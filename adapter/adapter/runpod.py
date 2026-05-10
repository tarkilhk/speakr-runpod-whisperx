import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from adapter.config import AdapterConfig
from adapter.errors import ConfigurationError, RunPodNotFoundError, RunPodTimeoutError
from adapter.pod_mapping import extract_tcp_mapping, pod_is_expected_running
from adapter.runpod_client import RunPodClient

logger = logging.getLogger("whisperx-adapter.runpod")


class RunPodManager:
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config
        self.client = RunPodClient(config)
        self._lock = asyncio.Lock()
        self._active_pod_id = config.runpod_pod_id

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

        async with self._lock:
            pod_id, pod, just_deployed = await self._get_or_deploy_pod()
            mapping = extract_tcp_mapping(pod, self.config.runpod_wrapper_port)
            if mapping:
                base_url = f"http://{mapping[0]}:{mapping[1]}"
                logger.info("Discovered RunPod TCP mapping: %s", base_url)
                if await self._wrapper_healthy(base_url):
                    logger.info("RunPod wrapper is already healthy")
                    return base_url
                logger.info("RunPod wrapper not healthy yet; waiting without calling start")
            else:
                pod_id = await self._handle_pod_without_mapping(pod_id, pod, just_deployed)

            return await self._wait_until_ready(pod_id)

    async def release_idle_pod(self) -> None:
        pod_id = self.load_active_pod_id()
        if not (self.config.runpod_api_key and pod_id):
            return

        action = self.config.idle_action
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

    async def _get_or_deploy_pod(self) -> tuple[str, dict[str, Any], bool]:
        pod_id = self.load_active_pod_id()
        just_deployed = False
        if not pod_id:
            pod_id = await self._deploy_from_template()
            just_deployed = True

        logger.info("Inspecting RunPod pod %s", pod_id)
        try:
            pod = await self.client.get_pod(pod_id)
        except RunPodNotFoundError:
            self._clear_active_pod_id(pod_id)
            if not self.config.template_mode_enabled:
                raise
            pod_id = await self._deploy_from_template()
            just_deployed = True
            pod = await self.client.get_pod(pod_id)

        return pod_id, pod, just_deployed

    async def _handle_pod_without_mapping(self, pod_id: str, pod: dict[str, Any], just_deployed: bool) -> str:
        if self.config.template_mode_enabled and not just_deployed and not pod_is_expected_running(pod):
            logger.info("No TCP mapping found for inactive pod %s; replacing it from template", pod_id)
            await self._terminate(pod_id)
            return await self._deploy_from_template()
        if self.config.template_mode_enabled:
            logger.info("RunPod pod %s has no TCP mapping yet; waiting for assignment", pod_id)
            return pod_id

        logger.info("No RunPod TCP mapping found; starting pod %s", pod_id)
        await self.client.start_pod(pod_id)
        return pod_id

    async def _wait_until_ready(self, pod_id: str) -> str:
        deadline = asyncio.get_running_loop().time() + self.config.runpod_readiness_timeout_seconds
        last_error = "Pod is not ready"

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.config.runpod_poll_interval_seconds)
            try:
                pod = await self.client.get_pod(pod_id)
            except RunPodNotFoundError:
                self._clear_active_pod_id(pod_id)
                if not self.config.template_mode_enabled:
                    raise
                pod_id = await self._deploy_from_template()
                last_error = "Replacement RunPod pod has not reported status yet"
                continue

            mapping = extract_tcp_mapping(pod, self.config.runpod_wrapper_port)
            if not mapping:
                last_error = "RunPod has not assigned a public TCP mapping yet"
                logger.info(last_error)
                continue

            base_url = f"http://{mapping[0]}:{mapping[1]}"
            logger.info("Polling RunPod wrapper health at %s", base_url)
            if await self._wrapper_healthy(base_url):
                logger.info("RunPod wrapper is healthy")
                return base_url
            last_error = f"Wrapper is not healthy at {base_url}"

        raise RunPodTimeoutError(last_error)

    async def _deploy_from_template(self) -> str:
        pod_id = await self.client.deploy_from_template()
        self._store_active_pod_id(pod_id)
        return pod_id

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

    async def _wrapper_healthy(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base_url}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
