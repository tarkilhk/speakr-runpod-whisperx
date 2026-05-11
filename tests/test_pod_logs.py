import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.support import make_config

from adapter.pod_logs import _emit_capture_lines, _lines_from_bundle, drain_cloud_pod_logs


def _pod_with_mapping(pod_id: str = "p1") -> dict:
    return {
        "id": pod_id,
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


class PodLogsHelpersTests(unittest.TestCase):
    def test_lines_from_bundle_returns_tuples(self) -> None:
        entries = _lines_from_bundle(
            {"files": [{"name": "whisperx-stdout.log", "content": "a\nb"}]},
        )
        self.assertEqual(entries, [("whisperx-stdout.log", "a"), ("whisperx-stdout.log", "b")])

    def test_lines_from_bundle_skips_bad_entries(self) -> None:
        self.assertEqual(_lines_from_bundle({}), [])
        self.assertEqual(_lines_from_bundle({"files": "nope"}), [])

    def test_emit_capture_truncates_long_utf8_line(self) -> None:
        with self.assertLogs("whisperx-adapter.runpod_logs", level="INFO") as cm:
            _emit_capture_lines("pid", [("whisperx-stdout.log", "x" * 400)], max_line_bytes=50)
        self.assertEqual(len(cm.records), 1)
        msg = cm.records[0].getMessage()
        self.assertIn("truncated", msg)
        self.assertIn("log_file=whisperx-stdout.log", msg)

    def test_emit_capture_structured_format(self) -> None:
        with self.assertLogs("whisperx-adapter.runpod_logs", level="INFO") as cm:
            _emit_capture_lines("mypod", [("whisperx-stderr.log", "oops")])
        msg = cm.records[0].getMessage()
        self.assertIn("pod_id=mypod", msg)
        self.assertIn("log_file=whisperx-stderr.log", msg)
        self.assertIn("oops", msg)


class PodLogsDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain_emits_capture_logger(self) -> None:
        cfg = make_config(adapter_whisperx_token="secret")
        client = MagicMock()
        client.get_pod = AsyncMock(return_value=_pod_with_mapping("p1"))
        with patch("adapter.pod_logs._fetch_bundle", new_callable=AsyncMock) as fetch:
            fetch.return_value = {"files": [{"name": "whisperx-stdout.log", "content": "hello\n"}]}
            with self.assertLogs("whisperx-adapter.runpod_logs", level="INFO") as cm:
                await drain_cloud_pod_logs(cfg, "p1", client)
        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(
            any("pod_id=p1" in m and "log_file=whisperx-stdout.log" in m and "hello" in m for m in messages)
        )


if __name__ == "__main__":
    unittest.main()
