import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from adapter.proxy import forward_asr
from tests.support import make_config


class PartialResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"partial":'
        raise httpx.ReadTimeout("synthetic incomplete response")

    async def aclose(self):
        pass


class DelayedPartialResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"partial":'
        await asyncio.sleep(0.05)
        raise httpx.ReadTimeout("synthetic delayed response")

    async def aclose(self):
        pass


class NeverEndingResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"partial":'
        await asyncio.sleep(3600)

    async def aclose(self):
        pass


class AdapterProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upload = tempfile.TemporaryDirectory()
        self.body_path = Path(self.upload.name) / "audio.request"
        self.body_path.write_bytes(b"private audio bytes")
        self.request = Request({
            "type": "http",
            "method": "POST",
            "path": "/asr",
            "query_string": b"diarize=true",
            "headers": [(b"content-type", b"audio/mpeg")],
        })

    async def asyncTearDown(self):
        self.upload.cleanup()

    async def test_complete_response_is_forwarded_and_logged_without_content(self):
        def respond(request):
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            self.assertRegex(request.headers["x-asr-trace-id"], r"^[0-9a-f]{12}$")
            self.assertEqual(request.url.params["diarize"], "true")
            return httpx.Response(200, json={"text": "private transcript"})

        transport = httpx.MockTransport(respond)
        client_class = httpx.AsyncClient
        with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=transport, **kw)):
            with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
                response = await forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'{"text":"private transcript"}')
        logs = "\n".join(captured.output)
        self.assertIn("ASR upstream headers received", logs)
        self.assertIn("ASR upstream body progress", logs)
        self.assertIn("ASR upstream body complete", logs)
        ids = re.findall(r"id=([0-9a-f]{12})", logs)
        self.assertEqual(len(set(ids)), 1)
        self.assertIn(f"decoded_body_bytes={len(response.body)}", logs)
        self.assertNotIn("private transcript", logs)
        self.assertNotIn("private audio bytes", logs)
        self.assertNotIn("test-token", logs)

    async def test_untrusted_trace_headers_are_replaced_with_one_generated_id(self):
        self.request = Request({
            "type": "http", "method": "POST", "path": "/asr", "query_string": b"",
            "headers": [(b"x-asr-trace-id", b"111111111111"),
                        (b"X-ASR-TRACE-ID", b"222222222222")],
        })

        def respond(request):
            trace_ids = request.headers.get_list("x-asr-trace-id")
            self.assertEqual(len(trace_ids), 1)
            self.assertRegex(trace_ids[0], r"^[0-9a-f]{12}$")
            self.assertNotIn(trace_ids[0], ("111111111111", "222222222222"))
            return httpx.Response(200, json={"text": "private transcript"})

        client_class = httpx.AsyncClient
        with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw)):
            with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
                await forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())

        logs = "\n".join(captured.output)
        self.assertNotIn("111111111111", logs)
        self.assertNotIn("222222222222", logs)

    async def test_partial_200_body_logs_phase_and_decoded_bytes_on_timeout(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "100"},
                stream=PartialResponse(),
            )
        )
        client_class = httpx.AsyncClient
        with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=transport, **kw)):
            with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
                with self.assertRaises(httpx.ReadTimeout):
                    await forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())

        logs = "\n".join(captured.output)
        self.assertIn("status=200 content_length=100", logs)
        self.assertIn("ASR upstream body progress", logs)
        self.assertIn("phase=response_body status=200 decoded_body_bytes=11", logs)
        self.assertIn("error=ReadTimeout", logs)
        self.assertNotIn("private audio bytes", logs)

    async def test_stalled_body_reports_byte_count_before_timeout(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "100"},
                stream=DelayedPartialResponse(),
            )
        )
        client_class = httpx.AsyncClient
        with patch("adapter.proxy.BODY_WAIT_LOG_INTERVAL_SECONDS", 0.01):
            with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=transport, **kw)):
                with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
                    with self.assertRaises(httpx.ReadTimeout):
                        await forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())

        logs = "\n".join(captured.output)
        self.assertIn("ASR upstream body waiting", logs)
        self.assertIn("decoded_body_bytes=11", logs)
        self.assertIn("since_last_chunk_s=", logs)
        self.assertNotIn("private audio bytes", logs)

    async def test_cancelled_body_logs_received_bytes(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "100"},
                stream=NeverEndingResponse(),
            )
        )
        client_class = httpx.AsyncClient
        with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=transport, **kw)):
            with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
                task = asyncio.create_task(
                    forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())
                )
                await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        logs = "\n".join(captured.output)
        self.assertIn("ASR upstream request cancelled", logs)
        self.assertIn("phase=response_body status=200 decoded_body_bytes=11", logs)

    async def test_413_preserves_error_detail(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(413, text="too large"))
        client_class = httpx.AsyncClient
        with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=transport, **kw)):
            with self.assertLogs("whisperx-adapter.proxy", level="INFO"):
                with self.assertRaises(HTTPException) as raised:
                    await forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())
        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail, "too large")


if __name__ == "__main__":
    unittest.main()
