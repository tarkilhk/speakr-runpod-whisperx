import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import make_config

from adapter.errors import ConfigurationError, RunPodNotFoundError, RunPodTimeoutError
from adapter.runpod import RunPodManager


def running_pod(pod_id: str = "pod-1") -> dict:
    return {
        "id": pod_id,
        "desiredStatus": "RUNNING",
        "machineId": "machine-1",
        "runtime": {
            "ports": [
                {
                    "ip": "127.0.0.1",
                    "isIpPublic": True,
                    "privatePort": 9000,
                    "publicPort": 19000,
                    "type": "tcp",
                }
            ]
        },
    }


class FakeDeployLock:
    def __call__(self) -> "FakeDeployLock":
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.get_pod = AsyncMock()
        self.deploy_from_template = AsyncMock()
        self.start_pod = AsyncMock()
        self.stop_pod = AsyncMock()
        self.terminate_pod = AsyncMock()


class RunPodManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, **config_overrides) -> tuple[RunPodManager, FakeClient]:
        manager = RunPodManager(make_config(**config_overrides))
        client = FakeClient()
        manager.client = client
        manager._deploy_lock = FakeDeployLock()
        return manager, client

    async def test_ensure_ready_uses_existing_active_pod_mapping(self) -> None:
        manager, client = self.make_manager(runpod_pod_id="fixed-pod", runpod_template_id="")
        client.get_pod.return_value = running_pod("fixed-pod")

        with patch("adapter.runpod.wrapper_healthy_detail", AsyncMock(return_value=(True, "status=200"))):
            base_url = await manager.ensure_ready()

        self.assertEqual(base_url, "http://127.0.0.1:19000")
        client.deploy_from_template.assert_not_awaited()
        client.start_pod.assert_not_awaited()

    async def test_ensure_ready_deploys_from_template_when_no_active_pod_exists(self) -> None:
        manager, client = self.make_manager()
        client.deploy_from_template.return_value = "new-pod"
        client.get_pod.return_value = running_pod("new-pod")

        with patch("adapter.runpod.wrapper_healthy_detail", AsyncMock(return_value=(True, "status=200"))):
            base_url = await manager.ensure_ready()

        self.assertEqual(base_url, "http://127.0.0.1:19000")
        client.deploy_from_template.assert_awaited_once()
        self.assertEqual(manager.load_active_pod_id(), "new-pod")

    async def test_ensure_ready_redeploys_when_stored_template_pod_is_missing(self) -> None:
        manager, client = self.make_manager()
        manager._active_pod_store.store("missing-pod")
        client.deploy_from_template.return_value = "replacement-pod"
        client.get_pod.side_effect = [
            RunPodNotFoundError("missing"),
            running_pod("replacement-pod"),
        ]

        with patch("adapter.runpod.wrapper_healthy_detail", AsyncMock(return_value=(True, "status=200"))):
            base_url = await manager.ensure_ready()

        self.assertEqual(base_url, "http://127.0.0.1:19000")
        client.deploy_from_template.assert_awaited_once()
        self.assertEqual(manager.load_active_pod_id(), "replacement-pod")

    async def test_fixed_pod_without_mapping_starts_existing_pod(self) -> None:
        manager, client = self.make_manager(runpod_pod_id="fixed-pod", runpod_template_id="")

        pod_id = await manager._handle_pod_without_mapping(
            "fixed-pod",
            {"id": "fixed-pod", "desiredStatus": "EXITED", "runtime": None},
            just_deployed=False,
            deadline=999999.0,
        )

        self.assertEqual(pod_id, "fixed-pod")
        client.start_pod.assert_awaited_once_with("fixed-pod")

    async def test_template_mode_replaces_inactive_pod_without_mapping(self) -> None:
        manager, client = self.make_manager()
        manager._active_pod_store.store("inactive-pod")
        client.deploy_from_template.return_value = "replacement-pod"

        pod_id = await manager._handle_pod_without_mapping(
            "inactive-pod",
            {"id": "inactive-pod", "desiredStatus": "EXITED", "runtime": None},
            just_deployed=False,
            deadline=999999.0,
        )

        self.assertEqual(pod_id, "replacement-pod")
        client.terminate_pod.assert_awaited_once_with("inactive-pod")
        client.deploy_from_template.assert_awaited_once()

    async def test_release_idle_pod_terminates_and_clears_template_pod(self) -> None:
        manager, client = self.make_manager(adapter_drain_pod_logs_on_idle=False)
        manager._active_pod_store.store("idle-pod")

        await manager.release_idle_pod()

        client.terminate_pod.assert_awaited_once_with("idle-pod")
        self.assertEqual(manager.load_active_pod_id(), "")

    async def test_release_idle_pod_drains_logs_when_enabled(self) -> None:
        manager, client = self.make_manager()
        manager._active_pod_store.store("idle-pod")

        with patch("adapter.runpod.drain_cloud_pod_logs", AsyncMock()) as drain:
            await manager.release_idle_pod()

        drain.assert_awaited_once_with(manager.config, "idle-pod", client)
        client.terminate_pod.assert_awaited_once_with("idle-pod")

    async def test_release_idle_pod_stops_fixed_pod_by_default(self) -> None:
        manager, client = self.make_manager(
            runpod_pod_id="fixed-pod",
            runpod_template_id="",
            adapter_drain_pod_logs_on_idle=False,
        )

        await manager.release_idle_pod()

        client.stop_pod.assert_awaited_once_with("fixed-pod")
        self.assertEqual(manager.load_active_pod_id(), "fixed-pod")

    async def test_release_idle_pod_does_not_auto_drain_logs_when_stopping_fixed_pod(self) -> None:
        manager, client = self.make_manager(
            runpod_pod_id="fixed-pod",
            runpod_template_id="",
            adapter_drain_pod_logs_on_idle=True,
        )

        with patch("adapter.runpod.drain_cloud_pod_logs", AsyncMock()) as drain:
            await manager.release_idle_pod()

        drain.assert_not_awaited()
        client.stop_pod.assert_awaited_once_with("fixed-pod")

    # --- configured() ---

    def test_configured_returns_false_when_api_key_missing(self) -> None:
        manager, _ = self.make_manager(runpod_api_key="")
        self.assertFalse(manager.configured())

    def test_configured_returns_false_when_no_pod_source(self) -> None:
        manager, _ = self.make_manager(runpod_pod_id="", runpod_template_id="")
        self.assertFalse(manager.configured())

    async def test_ensure_ready_raises_configuration_error_when_not_configured(self) -> None:
        manager, _ = self.make_manager(runpod_api_key="")
        with self.assertRaises(ConfigurationError):
            await manager.ensure_ready()

    # --- _wait_until_ready polling loop ---

    async def test_wait_until_ready_polls_until_tcp_mapping_appears(self) -> None:
        manager, client = self.make_manager(runpod_pod_id="fixed-pod", runpod_template_id="")
        pod_no_mapping = {"id": "fixed-pod", "desiredStatus": "RUNNING", "machineId": "m1", "runtime": None}
        client.get_pod.side_effect = [pod_no_mapping, running_pod("fixed-pod")]

        deadline = asyncio.get_running_loop().time() + 30
        with patch("adapter.runpod.wrapper_healthy_detail", AsyncMock(return_value=(True, "status=200"))):
            base_url = await manager._wait_until_ready("fixed-pod", deadline)

        self.assertEqual(base_url, "http://127.0.0.1:19000")
        self.assertEqual(client.get_pod.await_count, 2)

    async def test_wait_until_ready_retries_wrapper_health_until_ok(self) -> None:
        manager, client = self.make_manager(runpod_pod_id="fixed-pod", runpod_template_id="")
        client.get_pod.return_value = running_pod("fixed-pod")

        deadline = asyncio.get_running_loop().time() + 30
        health_responses = [(False, "http_status=503"), (True, "status=200")]
        with patch("adapter.runpod.wrapper_healthy_detail", AsyncMock(side_effect=health_responses)):
            base_url = await manager._wait_until_ready("fixed-pod", deadline)

        self.assertEqual(base_url, "http://127.0.0.1:19000")
        self.assertEqual(client.get_pod.await_count, 2)

    async def test_wait_until_ready_raises_timeout_error_when_deadline_expired(self) -> None:
        manager, client = self.make_manager(runpod_pod_id="fixed-pod", runpod_template_id="")

        deadline = asyncio.get_running_loop().time() - 1  # already in the past
        with self.assertLogs("whisperx-adapter.runpod", level="WARNING"):
            with self.assertRaises(RunPodTimeoutError):
                await manager._wait_until_ready("fixed-pod", deadline)

        client.get_pod.assert_not_awaited()

    async def test_wait_until_ready_redeploys_when_template_pod_vanishes_mid_loop(self) -> None:
        manager, client = self.make_manager()  # template mode
        manager._active_pod_store.store("old-pod")
        client.deploy_from_template.return_value = "new-pod"
        client.get_pod.side_effect = [
            RunPodNotFoundError("old-pod gone"),
            running_pod("new-pod"),
        ]

        deadline = asyncio.get_running_loop().time() + 30
        with patch("adapter.runpod.wrapper_healthy_detail", AsyncMock(return_value=(True, "status=200"))):
            base_url = await manager._wait_until_ready("old-pod", deadline)

        self.assertEqual(base_url, "http://127.0.0.1:19000")
        client.deploy_from_template.assert_awaited_once()
        self.assertEqual(manager.load_active_pod_id(), "new-pod")

    async def test_wait_until_ready_raises_when_pod_vanishes_in_fixed_pod_mode(self) -> None:
        manager, client = self.make_manager(runpod_pod_id="fixed-pod", runpod_template_id="")
        client.get_pod.side_effect = RunPodNotFoundError("gone")

        deadline = asyncio.get_running_loop().time() + 30
        with self.assertRaises(RunPodNotFoundError):
            await manager._wait_until_ready("fixed-pod", deadline)


if __name__ == "__main__":
    unittest.main()
