import asyncio
import fcntl
import logging
from pathlib import Path
from typing import Any

import httpx

from adapter.config import AdapterConfig
from adapter.errors import ConfigurationError, RunPodNotFoundError, RunPodTimeoutError
from adapter.pod_mapping import (
    extract_tcp_mapping,
    pod_is_expected_running,
    startup_progress_fingerprint,
    warmup_fingerprint_kv,
    warmup_status_kv,
)
from adapter.runpod_client import RunPodClient

logger = logging.getLogger("whisperx-adapter.runpod")


class RunPodManager:
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config
        self.client = RunPodClient(config)
        self._lock = asyncio.Lock()
        self._active_pod_id = config.runpod_pod_id
        self._deploy_lock_path = Path(
            f"{config.runpod_active_pod_id_path}.deploy.lock"
            if config.runpod_active_pod_id_path
            else "/tmp/speakr-runpod-deploy.lock"
        )

    def configured(self) -> bool:
        has_pod_source = bool(self.load_active_pod_id() or self.config.runpod_template_id)
        return bool(self.config.runpod_api_key and has_pod_source and self.config.adapter_whisperx_token)

    def health_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "template_mode_enabled": self.config.template_mode_enabled,
            "active_pod_id_configured": bool(self.load_active_pod_id()),
            "idle_action": self.config.idle_action,
        }

    def load_active_pod_id(self) -> str:
        if self._active_pod_id:
            return self._active_pod_id
        if not self.config.runpod_active_pod_id_path:
            return ""
        try:
            return Path(self.config.runpod_active_pod_id_path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    async def ensure_ready(self) -> str:
        if not self.configured():
            raise ConfigurationError(
                "RunPod adapter is not configured; set RUNPOD_API_KEY, ADAPTER_WHISPERX_TOKEN, "
                "and either RUNPOD_POD_ID or RUNPOD_TEMPLATE_ID"
            )

        deadline = asyncio.get_running_loop().time() + self.config.runpod_readiness_timeout_seconds
        async with self._lock:
            pod_id, pod, just_deployed = await self._get_or_deploy_pod(deadline)
            mapping = extract_tcp_mapping(pod, self.config.runpod_wrapper_port)
            if mapping:
                base_url = f"http://{mapping[0]}:{mapping[1]}"
                logger.info(
                    "Discovered RunPod TCP mapping url=%s pod_id=%s status=%s",
                    base_url,
                    pod_id,
                    warmup_status_kv(pod, wrapper_port=self.config.runpod_wrapper_port),
                )
                healthy, detail = await self._wrapper_healthy_detail(base_url)
                if healthy:
                    logger.info("RunPod wrapper is already healthy url=%s pod_id=%s", base_url, pod_id)
                    return base_url
                logger.info(
                    "RunPod wrapper not healthy yet pod_id=%s url=%s detail=%s; waiting without calling start",
                    pod_id,
                    base_url,
                    detail,
                )
            else:
                pod_id = await self._handle_pod_without_mapping(pod_id, pod, just_deployed, deadline)

            return await self._wait_until_ready(pod_id, deadline)

    async def release_idle_pod(self) -> None:
        pod_id = self.load_active_pod_id()
        # Silent failures here used to make idle shutdown opaque in logs; explain skips.
        if not self.config.runpod_api_key:
            logger.warning("Idle release skipped: RUNPOD_API_KEY is not set")
            return
        if not pod_id:
            logger.info(
                "Idle release skipped: no active RunPod pod id (memory or %s)",
                self.config.runpod_active_pod_id_path or "(no path)",
            )
            return

        action = self.config.idle_action
        logger.info(
            "Applying idle action=%s to RunPod pod_id=%s template_mode=%s idle_stop_seconds=%s "
            "active_pod_path=%s",
            action,
            pod_id,
            self.config.template_mode_enabled,
            self.config.runpod_idle_stop_seconds,
            self.config.runpod_active_pod_id_path or "(none)",
        )
        try:
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
            self._clear_active_pod_id(pod_id)
            if not self.config.template_mode_enabled:
                raise
            pod_id = await self._deploy_from_template_with_retry(deadline)
            just_deployed = True
            pod = await self.client.get_pod(pod_id)

        return pod_id, pod, just_deployed

    async def _handle_pod_without_mapping(self, pod_id: str, pod: dict[str, Any], just_deployed: bool, deadline: float) -> str:
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

    async def _wait_until_ready(self, pod_id: str, deadline: float) -> str:
        last_error = "Pod is not ready"
        stuck_init_since: float | None = None
        # Snapshot of startup_progress_fingerprint(); when RunPod mutates any tracked field
        # between polls, we assume warmup is still moving (e.g. image pull) and reset the
        # stuck timer instead of terminating after a fixed delay with no runtime yet.
        progress_prev: tuple[Any, ...] | None = None
        last_pod: dict[str, Any] | None = None

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.config.runpod_poll_interval_seconds)
            try:
                pod = await self.client.get_pod(pod_id)
                last_pod = pod
            except RunPodNotFoundError:
                self._clear_active_pod_id(pod_id)
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
                    "RunPod pod_id=%s warmup fingerprint changed; resetting stuck-init timer "
                    "(poll_interval_seconds=%s stuck_init_timeout_seconds=%s) prev=[%s] now=[%s]",
                    pod_id,
                    self.config.runpod_poll_interval_seconds,
                    self.config.runpod_stuck_init_timeout_seconds,
                    warmup_fingerprint_kv(progress_prev),
                    warmup_fingerprint_kv(progress_fp),
                )
                stuck_init_since = None
            progress_prev = progress_fp

            # Machine assigned but runtime not visible yet — normal during image pull / boot.
            # Without movement in startup_progress_fingerprint(), assume the placement is stuck.
            if (
                self.config.template_mode_enabled
                and self.config.runpod_stuck_init_timeout_seconds > 0
                and pod.get("machineId")
                and not pod.get("runtime")
            ):
                if stuck_init_since is None:
                    stuck_init_since = now
                    logger.info(
                        "RunPod pod_id=%s machineId=%s assigned; waiting for runtime "
                        "(poll_interval_seconds=%s stuck_init_timeout_seconds=%s). "
                        "Timer resets when warmup fingerprint moves. status=%s",
                        pod_id,
                        pod["machineId"],
                        self.config.runpod_poll_interval_seconds,
                        self.config.runpod_stuck_init_timeout_seconds,
                        warmup_status_kv(pod, wrapper_port=self.config.runpod_wrapper_port),
                    )
                elif now - stuck_init_since > self.config.runpod_stuck_init_timeout_seconds:
                    elapsed = now - stuck_init_since
                    logger.warning(
                        "pod_id=%s no warmup fingerprint change for elapsed_seconds=%.1f "
                        "(stuck_init_timeout_seconds=%s machineId=%s template_mode=%s); "
                        "terminating and redeploying frozen=[%s] status=%s",
                        pod_id,
                        elapsed,
                        self.config.runpod_stuck_init_timeout_seconds,
                        pod["machineId"],
                        self.config.template_mode_enabled,
                        warmup_fingerprint_kv(progress_fp),
                        warmup_status_kv(pod, wrapper_port=self.config.runpod_wrapper_port),
                    )
                    await self._terminate(pod_id)
                    pod_id = await self._deploy_from_template_with_retry(deadline)
                    stuck_init_since = None
                    progress_prev = None
                    last_error = "Redeployed after stuck initialization"
                    continue
            else:
                # Either runtime exists (normal path) or stuck-init watchdog disabled / no machine:
                # do not carry over a stale stuck-init deadline into the next state.
                stuck_init_since = None

            mapping = extract_tcp_mapping(pod, self.config.runpod_wrapper_port)
            if not mapping:
                last_error = "RunPod has not assigned a public TCP mapping yet"
                loop_time = asyncio.get_running_loop().time()
                remaining = max(0.0, deadline - loop_time)
                logger.info(
                    "pod_id=%s still no TCP mapping for wrapper_port=%s "
                    "(readiness_remaining_seconds=%.1f poll_interval_seconds=%s): %s",
                    pod_id,
                    self.config.runpod_wrapper_port,
                    remaining,
                    self.config.runpod_poll_interval_seconds,
                    warmup_status_kv(pod, wrapper_port=self.config.runpod_wrapper_port),
                )
                continue

            base_url = f"http://{mapping[0]}:{mapping[1]}"
            logger.info(
                "Polling RunPod wrapper health url=%s pod_id=%s status=%s",
                base_url,
                pod_id,
                warmup_status_kv(pod, wrapper_port=self.config.runpod_wrapper_port),
            )
            healthy, detail = await self._wrapper_healthy_detail(base_url)
            if healthy:
                logger.info("RunPod wrapper is healthy url=%s pod_id=%s", base_url, pod_id)
                return base_url
            last_error = f"Wrapper is not healthy at {base_url} ({detail})"
            logger.info(
                "RunPod wrapper not healthy yet pod_id=%s url=%s detail=%s status=%s",
                pod_id,
                base_url,
                detail,
                warmup_status_kv(pod, wrapper_port=self.config.runpod_wrapper_port),
            )

        if last_pod is not None:
            logger.warning(
                "RunPod readiness timeout pod_id=%s last_error=%s readiness_timeout_seconds=%s "
                "poll_interval_seconds=%s wrapper_port=%s status=%s",
                pod_id,
                last_error,
                self.config.runpod_readiness_timeout_seconds,
                self.config.runpod_poll_interval_seconds,
                self.config.runpod_wrapper_port,
                warmup_status_kv(last_pod, wrapper_port=self.config.runpod_wrapper_port),
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

            lock_file = await asyncio.to_thread(self._acquire_deploy_lock)
            try:
                existing_pod_id = self.load_active_pod_id()
                if existing_pod_id:
                    logger.info("Detected active RunPod pod %s while waiting for deploy lock", existing_pod_id)
                    return existing_pod_id

                pod_id = await self.client.deploy_from_template()
                self._store_active_pod_id(pod_id)
                return pod_id
            except (httpx.HTTPError, RunPodTimeoutError, ConfigurationError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning("RunPod deploy attempt failed: %s", exc)
            finally:
                await asyncio.to_thread(self._release_deploy_lock, lock_file)

            await asyncio.sleep(self.config.runpod_poll_interval_seconds)

        logger.warning(
            "RunPod deploy retries exhausted before readiness deadline last_error=%s "
            "readiness_timeout_seconds=%s poll_interval_seconds=%s template_id=%s",
            last_error,
            self.config.runpod_readiness_timeout_seconds,
            self.config.runpod_poll_interval_seconds,
            self.config.runpod_template_id or "(none)",
        )
        raise RunPodTimeoutError(f"Timed out while deploying RunPod pod: {last_error}")

    def _acquire_deploy_lock(self) -> Any:
        self._deploy_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._deploy_lock_path.open("a+", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return lock_file

    def _release_deploy_lock(self, lock_file: Any) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    async def _terminate(self, pod_id: str) -> None:
        try:
            logger.info("Terminating RunPod pod %s", pod_id)
            await self.client.terminate_pod(pod_id)
            logger.info("RunPod pod %s terminated", pod_id)
        except RunPodNotFoundError:
            logger.info("RunPod pod %s was already gone", pod_id)
        finally:
            self._clear_active_pod_id(pod_id)

    def _store_active_pod_id(self, pod_id: str) -> None:
        self._active_pod_id = pod_id
        if not self.config.runpod_active_pod_id_path:
            return
        path = Path(self.config.runpod_active_pod_id_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pod_id, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to write active RunPod pod ID to %s: %s", path, exc)

    def _clear_active_pod_id(self, pod_id: str | None = None) -> None:
        if pod_id and self._active_pod_id and pod_id != self._active_pod_id:
            return
        self._active_pod_id = ""
        if not self.config.runpod_active_pod_id_path:
            return
        try:
            Path(self.config.runpod_active_pod_id_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove active RunPod pod ID file: %s", exc)

    async def _wrapper_healthy_detail(self, base_url: str) -> tuple[bool, str]:
        """Return (healthy, detail) where detail is safe for logs (status code or error kind)."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base_url}/health")
            if response.status_code == 200:
                return True, "status=200"
            return False, f"http_status={response.status_code}"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
