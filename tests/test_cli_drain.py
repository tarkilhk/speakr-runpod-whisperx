import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.support import make_config


def _make_config(**overrides):
    defaults = {"runpod_active_pod_id_path": ""}
    defaults.update(overrides)
    return make_config(**defaults)


class CliDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_exits_1_when_api_key_missing(self) -> None:
        from adapter.cli_drain import _run

        cfg = _make_config(runpod_api_key="", runpod_pod_id="p1")
        self.assertEqual(await _run(cfg), 1)

    async def test_exits_1_when_token_missing(self) -> None:
        from adapter.cli_drain import _run

        cfg = _make_config(adapter_whisperx_token="", runpod_pod_id="p1")
        self.assertEqual(await _run(cfg), 1)

    async def test_exits_1_when_no_active_pod_id(self) -> None:
        from adapter.cli_drain import _run

        cfg = _make_config(runpod_pod_id="", runpod_active_pod_id_path="")
        self.assertEqual(await _run(cfg), 1)

    async def test_exits_0_when_drain_succeeds(self) -> None:
        from adapter.cli_drain import _run

        cfg = _make_config(runpod_pod_id="p1")
        with patch("adapter.cli_drain.drain_cloud_pod_logs", new_callable=AsyncMock, return_value=True) as mock_drain:
            result = await _run(cfg)
        self.assertEqual(result, 0)
        mock_drain.assert_awaited_once()
        _, call_pod_id, _ = mock_drain.call_args.args
        self.assertEqual(call_pod_id, "p1")

    async def test_exits_1_when_drain_fails(self) -> None:
        from adapter.cli_drain import _run

        cfg = _make_config(runpod_pod_id="p1")
        with patch("adapter.cli_drain.drain_cloud_pod_logs", new_callable=AsyncMock, return_value=False):
            result = await _run(cfg)
        self.assertEqual(result, 1)

    def test_main_calls_sys_exit(self) -> None:
        from adapter.cli_drain import main

        with patch("adapter.cli_drain.AdapterConfig.from_env", return_value=_make_config(runpod_pod_id="")):
            with self.assertRaises(SystemExit) as cm:
                main()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
