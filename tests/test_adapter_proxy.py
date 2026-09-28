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
        self.assertIn("ASR upstream body complete", logs)
        self.assertIn(f"decoded_body_bytes={len(response.body)}", logs)
        self.assertNotIn("private transcript", logs)
        self.assertNotIn("private audio bytes", logs)
        self.assertNotIn("test-token", logs)

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
        self.assertIn("phase=response_body status=200 decoded_body_bytes=11", logs)
        self.assertIn("error=ReadTimeout", logs)
        self.assertNotIn("private audio bytes", logs)

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
