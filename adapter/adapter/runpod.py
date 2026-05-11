import asyncio
import logging
from typing import Any

import httpx

from adapter.config import AdapterConfig
from adapter.errors import ConfigurationError, RunPodNotFoundError, RunPodTimeoutError
from adapter.pod_mapping import (
    extract_tcp_mapping,
    pod_is_expected_running,
    startup_progress_fingerprint,
    warmup_digest,
    warmup_fingerprint_kv,
)
from adapter.pod_logs import drain_cloud_pod_logs
from adapter.pod_state import ActivePodStore, DeployLock
from adapter.runpod_client import RunPodClient
from adapter.wrapper_health import wrapper_healthy_detail

logger = logging.getLogger("whisperx-adapter.runpod")


class RunPodManager:
    """Keeps one GPU pod warm for WhisperX: deploy/resume from template or fixed pod id, poll
    GraphQL until public TCP mapping exists, probe wrapper /health, optionally redeploy on stuck
    init when lifecycle fingerprint stops moving (see RUNPOD_STUCK_INIT_TIMEOUT_SECONDS).
    """
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config
        self.client = RunPodClient(config)
        self._lock = asyncio.Lock()
        self._active_pod_store = ActivePodStore(config.runpod_pod_id, config.runpod_active_pod_id_path)
        self._deploy_lock = DeployLock(config.runpod_active_pod_id_path)

    def configured(self) -> bool:
        has_pod_source = bool(self.load_active_pod_id() or self.config.runpod_template_id)
        return bool(self.config.runpod_api_key and has_pod_source and self.config.adapter_whisperx_token)

    def health_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "template_mode_enabled": self.config.template_mode_enabled,
            "active_pod_id_configured": bool(self.load_active_pod_id()),
            "idle_action": self.config.idle_action,
            "pod_log_drain_enabled": self.config.adapter_drain_pod_logs_on_idle,
        }

    def load_active_pod_id(self) -> str:
        return self._active_pod_store.load()

    async def ensure_ready(self) -> str:
        """Return base URL for the authenticated WhisperX wrapper (may poll until timeout)."""
        if not self.configured():
            raise ConfigurationError(
                "RunPod adapter is not configured; set RUNPOD_API_KEY, ADAPTER_WHISPERX_TOKEN, "
                "and either RUNPOD_POD_ID or RUNPOD_TEMPLATE_ID"
            )

        deadline = asyncio.get_running_loop().time() + self.config.runpod_readiness_timeout_seconds
        async with self._lock:
            pod_id, pod, just_deployed = await self._get_or_deploy_pod(deadline)
            mapping = extract_tcp_mapping(pod, self.config.runpod_wrapper_port)
            skip_initial_tcp_log = False
            if mapping:
                base_url = f"http://{mapping[0]}:{mapping[1]}"
                logger.info("RunPod TCP mapping %s pod_id=%s (%s)", base_url, pod_id, warmup_digest(pod))
                healthy, detail = await wrapper_healthy_detail(base_url)
                if healthy:
                    logger.info("RunPod wrapper already healthy %s pod_id=%s", base_url, pod_id)
                    return base_url
                logger.info(
                    "RunPod wrapper not healthy yet pod_id=%s url=%s (%s); waiting",
                    pod_id,
                    base_url,
                    detail,
                )
                skip_initial_tcp_log = True  # avoid repeating TCP-ready INFO at poll loop start
            else:
                pod_id = await self._handle_pod_without_mapping(pod_id, pod, just_deployed, deadline)

            return await self._wait_until_ready(pod_id, deadline, skip_initial_tcp_log=skip_initial_tcp_log)

    async def release_idle_pod(self) -> None:
        pod_id = self.load_active_pod_id()
        # Silent failures here used to make idle shutdown opaque in logs; explain skips.
        if not self.config.runpod_api_key:
            logger.warning("Idle release skipped: RUNPOD_API_KEY is not set")
            return
        if not pod_id:
            logger.info(
                "Idle release skipped: no active RunPod pod id (memory or %s)",
                self._active_pod_store.path_label,
            )
            return

        action = self.config.idle_action
        logger.info("Idle release: action=%s pod_id=%s", action, pod_id)
        try:
            if self.config.adapter_drain_pod_logs_on_idle and action == "terminate":
                try:
                    await drain_cloud_pod_logs(self.config, pod_id, self.client)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Pod log drain raised unexpectedly pod_id=%s: %s", pod_id, exc)
            elif self.config.adapter_drain_pod_logs_on_idle:
                logger.info("Pod log drain skipped for non-terminal idle action action=%s pod_id=%s", action, pod_id)

            if action == "terminate":
                await self._terminate(pod_id)
                return
            if action != "stop":
                raise ConfigurationError("RUNPOD_IDLE_ACTION must be either 'stop' or 'terminate'")
            logger.info("Stopping RunPod pod %s", pod_id)
            await self.client.stop_pod(pod_id)
            logger.info("RunPod pod stop request completed")
        except Exception as exc:
            logger.warning("Failed to %s RunPod pod %s: %s", action, pod_id, exc)

    async def _get_or_deploy_pod(self, deadline: float) -> tuple[str, dict[str, Any], bool]:
        pod_id = self.load_active_pod_id()
        just_deployed = False
        if not pod_id:
            pod_id = await self._deploy_from_template_with_retry(deadline)
            just_deployed = True

        logger.info("Inspecting RunPod pod %s", pod_id)
        try:
            pod = await self.client.get_pod(pod_id)
        except RunPodNotFoundError:
            self._active_pod_store.clear(pod_id)
            if not self.config.template_mode_enabled:
                raise
            pod_id = await self._deploy_from_template_with_retry(deadline)
            just_deployed = True
            pod = await self.client.get_pod(pod_id)

        return pod_id, pod, just_deployed

    async def _handle_pod_without_mapping(self, pod_id: str, pod: dict[str, Any], just_deployed: bool, deadline: float) -> str:
        # Template mode can replace a dead/disabled pod; fixed-pod mode resumes the same pod id.
        if self.config.template_mode_enabled and not just_deployed and not pod_is_expected_running(pod):
            logger.info("No TCP mapping found for inactive pod %s; replacing it from template", pod_id)
            await self._terminate(pod_id)
            return await self._deploy_from_template_with_retry(deadline)
        if self.config.template_mode_enabled:
            logger.info("RunPod pod %s has no TCP mapping yet; waiting for assignment", pod_id)
            return pod_id

        logger.info("No RunPod TCP mapping found; starting pod %s", pod_id)
        await self.client.start_pod(pod_id)
        return pod_id

    async def _wait_until_ready(self, pod_id: str, deadline: float, skip_initial_tcp_log: bool = False) -> str:
        last_error = "Pod is not ready"
        stuck_init_since: float | None = None
        # Snapshot of startup_progress_fingerprint(); when RunPod mutates any tracked field
        # between polls, we assume warmup is still moving (e.g. image pull) and reset the
        # stuck timer instead of terminating after a fixed delay with no runtime yet.
        progress_prev: tuple[Any, ...] | None = None
        last_pod: dict[str, Any] | None = None
        last_slow_poll_log = -1e9  # first no-mapping wait logs immediately; then every ~30s
        # INFO once per pod_id when mapping appears (seeded if ensure_ready already logged that URL).
        tcp_logged_for_pod: str | None = pod_id if skip_initial_tcp_log else None

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.config.runpod_poll_interval_seconds)
            try:
                pod = await self.client.get_pod(pod_id)
                last_pod = pod
            except RunPodNotFoundError:
                self._active_pod_store.clear(pod_id)
                if not self.config.template_mode_enabled:
                    raise
                pod_id = await self._deploy_from_template_with_retry(deadline)
                stuck_init_since = None
                progress_prev = None
                last_error = "Replacement RunPod pod has not reported status yet"
                continue

            now = asyncio.get_running_loop().time()

            progress_fp = startup_progress_fingerprint(pod)
            # Any change ⇒ reset stuck_init_since below so RUNPOD_STUCK_INIT_TIMEOUT_SECONDS is a
            # "no API movement" threshold, not "no runtime yet" wall time alone.
            if progress_prev is not None and progress_fp != progress_prev:
                logger.info(
                    "pod_id=%s warmup still progressing; stuck-init timer reset (lifecycle fields changed)",
                    pod_id,
                )
                logger.debug(
                    "pod_id=%s fingerprint prev=[%s] now=[%s]",
                    pod_id,
                    warmup_fingerprint_kv(progress_prev),
                    warmup_fingerprint_kv(progress_fp),
                )
                stuck_init_since = None
            progress_prev = progress_fp

            # Stuck-init = no runtime yet + warmup fingerprint unchanged between polls. GraphQL does
            # not expose image pull vs deadlock; a generous timeout limits false redeploys.
            if (
                self.config.template_mode_enabled
                and self.config.runpod_stuck_init_timeout_seconds > 0
                and pod.get("machineId")
                and not pod.get("runtime")
            ):
                if stuck_init_since is None:
                    stuck_init_since = now
                    logger.info(
                        "pod_id=%s stuck-init armed machineId=%s threshold_seconds=%s (%s)",
                        pod_id,
                        pod["machineId"],
                        self.config.runpod_stuck_init_timeout_seconds,
                        warmup_digest(pod),
                    )
                elif now - stuck_init_since > self.config.runpod_stuck_init_timeout_seconds:
                    elapsed = now - stuck_init_since
                    logger.warning(
                        "pod_id=%s stuck-init exceeded elapsed_seconds=%.1f threshold=%s frozen=[%s] digest=%s",
                        pod_id,
                        elapsed,
                        self.config.runpod_stuck_init_timeout_seconds,
                        warmup_fingerprint_kv(progress_fp),
                        warmup_digest(pod),
                    )
                    await self._terminate(pod_id)
                    pod_id = await self._deploy_from_template_with_retry(deadline)
                    stuck_init_since = None
                    progress_prev = None
                    last_error = "Redeployed after stuck initialization"
                    continue
            else:
                # Runtime up, watchdog disabled, or no machine assignment yet — clear armed deadline.
                stuck_init_since = None

            mapping = extract_tcp_mapping(pod, self.config.runpod_wrapper_port)
            if not mapping:
                last_error = "RunPod has not assigned a public TCP mapping yet"
                loop_time = asyncio.get_running_loop().time()
                remaining = max(0.0, deadline - loop_time)
                if loop_time - last_slow_poll_log >= 30.0:
                    last_slow_poll_log = loop_time
                    logger.info(
                        "pod_id=%s awaiting TCP port=%s remaining_s=%.0f %s",
                        pod_id,
                        self.config.runpod_wrapper_port,
                        remaining,
                        warmup_digest(pod),
                    )
                continue

            base_url = f"http://{mapping[0]}:{mapping[1]}"
            if tcp_logged_for_pod != pod_id:
                tcp_logged_for_pod = pod_id
                logger.info(
                    "pod_id=%s RunPod TCP mapping ready; polling wrapper %s (%s)",
                    pod_id,
                    base_url,
                    warmup_digest(pod),
                )
            logger.debug("Polling wrapper health pod_id=%s url=%s", pod_id, base_url)
            healthy, detail = await wrapper_healthy_detail(base_url)
            if healthy:
                logger.info("RunPod wrapper healthy %s pod_id=%s", base_url, pod_id)
                return base_url
            last_error = f"Wrapper is not healthy at {base_url} ({detail})"
            logger.info(
                "RunPod wrapper health check not OK yet pod_id=%s url=%s (%s); will retry",
                pod_id,
                base_url,
                detail,
            )

        if last_pod is not None:
            logger.warning(
                "RunPod readiness timeout pod_id=%s last_error=%s readiness_timeout_s=%s wrapper_port=%s digest=%s",
                pod_id,
                last_error,
                self.config.runpod_readiness_timeout_seconds,
                self.config.runpod_wrapper_port,
                warmup_digest(last_pod),
            )
        else:
            logger.warning(
                "RunPod readiness timeout pod_id=%s last_error=%s readiness_timeout_seconds=%s "
                "(no pod snapshot yet)",
                pod_id,
                last_error,
                self.config.runpod_readiness_timeout_seconds,
            )
        raise RunPodTimeoutError(last_error)

    async def _deploy_from_template_with_retry(self, deadline: float) -> str:
        last_error = "RunPod deploy did not complete"
        while asyncio.get_running_loop().time() < deadline:
            existing_pod_id = self.load_active_pod_id()
            if existing_pod_id:
                logger.info("Using existing RunPod pod ID %s from shared state", existing_pod_id)
                return existing_pod_id

            try:
                async with self._deploy_lock():
                    existing_pod_id = self.load_active_pod_id()
                    if existing_pod_id:
                        logger.info("Detected active RunPod pod %s while waiting for deploy lock", existing_pod_id)
                        return existing_pod_id

                    pod_id = await self.client.deploy_from_template()
                    self._active_pod_store.store(pod_id)
                    return pod_id
            except (httpx.HTTPError, RunPodTimeoutError, ConfigurationError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning("RunPod deploy attempt failed: %s", exc)

            await asyncio.sleep(self.config.runpod_poll_interval_seconds)

        logger.warning(
            "RunPod deploy exhausted last_error=%s readiness_timeout_s=%s template_id=%s",
            last_error,
            self.config.runpod_readiness_timeout_seconds,
            self.config.runpod_template_id or "(none)",
        )
        raise RunPodTimeoutError(f"Timed out while deploying RunPod pod: {last_error}")

    async def _terminate(self, pod_id: str) -> None:
        try:
            logger.info("Terminating RunPod pod %s", pod_id)
            await self.client.terminate_pod(pod_id)
            logger.info("RunPod pod %s terminated", pod_id)
        except RunPodNotFoundError:
            logger.info("RunPod pod %s was already gone", pod_id)
        finally:
            self._active_pod_store.clear(pod_id)
