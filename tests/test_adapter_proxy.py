import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from adapter.errors import TemporaryRunPodError
from adapter.proxy import forward_asr
from speakr_common.asr_result_protocol import CHUNK_SIZE, PROTOCOL_HEADER, PROTOCOL_VERSION
from tests.support import make_config

RESULT_ID = "a" * 32
BODY = b'{"text":"' + b'x' * (477323 - 11) + b'"}'


def manifest(body=BODY):
    return httpx.Response(202, json={
        "id": RESULT_ID, "length": len(body), "sha256": hashlib.sha256(body).hexdigest(),
    }, headers={PROTOCOL_HEADER: PROTOCOL_VERSION})


class PartialResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'partial'
        raise httpx.ReadTimeout("synthetic incomplete response")

    async def aclose(self):
        pass


class NeverEndingResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b'never'

    async def aclose(self):
        pass


class AdapterProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upload = tempfile.TemporaryDirectory()
        self.body_path = Path(self.upload.name) / "audio.request"
        self.body_path.write_bytes(b"private audio bytes")
        self.request = Request({
            "type": "http", "method": "POST", "path": "/asr",
            "query_string": b"diarize=true", "headers": [(b"content-type", b"audio/mpeg")],
        })

    async def asyncTearDown(self):
        self.upload.cleanup()

    async def _forward(self, handler):
        original = httpx.AsyncClient
        with patch("adapter.proxy.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            return await forward_asr("http://mock-wrapper", self.request, self.body_path, make_config())

    async def test_complete_large_response_is_reassembled_and_logged_without_content(self):
        offsets = []
        trace_ids = set()

        def respond(request):
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            trace_ids.add(request.headers["x-asr-trace-id"])
            if request.method == "POST":
                self.assertEqual(request.headers[PROTOCOL_HEADER], PROTOCOL_VERSION)
                self.assertEqual(request.url.params["diarize"], "true")
                return manifest()
            if request.method == "DELETE":
                return httpx.Response(204)
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            self.assertLessEqual(limit, CHUNK_SIZE)
            offsets.append(offset)
            return httpx.Response(200, content=BODY[offset:offset + limit], headers={"X-ASR-Chunk-Offset": str(offset)})

        with self.assertLogs(level="INFO") as captured:
            response = await self._forward(respond)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, BODY)
        self.assertEqual(len(offsets), 59)
        self.assertEqual(len(trace_ids), 1)
        self.assertRegex(next(iter(trace_ids)), r"^[0-9a-f]{12}$")
        logs = "\n".join(captured.output)
        self.assertIn("ASR result verified", logs)
        self.assertIn(f"decoded_body_bytes={len(BODY)}", logs)
        self.assertNotIn("private audio bytes", logs)
        self.assertNotIn("test-token", logs)
        self.assertNotIn('"text"', logs)

    async def test_untrusted_trace_headers_are_replaced_with_one_generated_id(self):
        self.request = Request({
            "type": "http", "method": "POST", "path": "/asr", "query_string": b"",
            "headers": [(b"x-asr-trace-id", b"111111111111"), (b"X-ASR-TRACE-ID", b"222222222222"),
                        (b"x-asr-result-protocol", b"untrusted")],
        })

        def respond(request):
            self.assertEqual(len(request.headers.get_list("x-asr-trace-id")), 1)
            self.assertNotIn(request.headers["x-asr-trace-id"], ("111111111111", "222222222222"))
            if request.method == "POST":
                self.assertEqual(request.headers[PROTOCOL_HEADER], PROTOCOL_VERSION)
                return manifest(b"{}")
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, content=b"{}", headers={"X-ASR-Chunk-Offset": "0"})

        with self.assertLogs(level="INFO") as captured:
            await self._forward(respond)
        logs = "\n".join(captured.output)
        self.assertNotIn("111111111111", logs)
        self.assertNotIn("222222222222", logs)

    async def test_old_wrapper_fails_fast_without_reading_large_body(self):
        def respond(request):
            return httpx.Response(200, headers={"content-length": "477323"}, stream=NeverEndingResponse())
        with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
            with self.assertRaisesRegex(TemporaryRunPodError, "does not support"):
                await self._forward(respond)
        self.assertIn("status=200 content_length=477323", "\n".join(captured.output))

    async def test_one_partial_chunk_retries_same_offset_without_rerunning_gpu(self):
        attempts = 0
        def respond(request):
            nonlocal attempts
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                return httpx.Response(204)
            attempts += 1
            if attempts == 1:
                return httpx.Response(200, headers={"X-ASR-Chunk-Offset": "0"}, stream=PartialResponse())
            return httpx.Response(200, content=b"safe result", headers={"X-ASR-Chunk-Offset": "0"})
        with self.assertLogs("whisperx-adapter.result_transfer", level="INFO") as captured:
            response = await self._forward(respond)
        self.assertEqual(response.body, b"safe result")
        self.assertEqual(attempts, 2)
        self.assertIn("ASR result chunk retry", "\n".join(captured.output))

    async def test_persistent_partial_chunk_exhausts_retries(self):
        attempts = 0
        def respond(request):
            nonlocal attempts
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                return httpx.Response(204)
            attempts += 1
            return httpx.Response(200, headers={"X-ASR-Chunk-Offset": "0"}, stream=PartialResponse())
        with self.assertLogs("whisperx-adapter.result_transfer", level="WARNING"):
            with self.assertRaisesRegex(TemporaryRunPodError, "after retries"):
                await self._forward(respond)
        self.assertEqual(attempts, 3)

    async def test_oversized_streamed_chunk_is_rejected_without_buffering_all_of_it(self):
        attempts = 0
        class Oversized(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * (CHUNK_SIZE * 16)
            async def aclose(self):
                pass
        def respond(request):
            nonlocal attempts
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                return httpx.Response(204)
            attempts += 1
            return httpx.Response(200, headers={"X-ASR-Chunk-Offset": "0"}, stream=Oversized())
        with self.assertLogs("whisperx-adapter.result_transfer", level="WARNING") as captured:
            with self.assertRaises(TemporaryRunPodError):
                await self._forward(respond)
        self.assertEqual(attempts, 3)
        self.assertIn("oversize_body", "\n".join(captured.output))

    async def test_stalled_cleanup_cannot_hold_verified_reply(self):
        async def respond(request):
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                await asyncio.sleep(3600)
                return httpx.Response(204)
            return httpx.Response(200, content=b"safe result", headers={"X-ASR-Chunk-Offset": "0"})
        started = time.monotonic()
        with patch("adapter.result_transfer.CLEANUP_DEADLINE_SECONDS", 0.03):
            with self.assertLogs("whisperx-adapter.result_transfer", level="WARNING") as captured:
                response = await self._forward(respond)
        self.assertEqual(response.body, b"safe result")
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("cleanup deferred to TTL", "\n".join(captured.output))

    async def test_cleanup_retries_transient_http_failure_without_losing_result(self):
        deletes = 0
        def respond(request):
            nonlocal deletes
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                deletes += 1
                return httpx.Response(503 if deletes == 1 else 204)
            return httpx.Response(200, content=b"safe result", headers={"X-ASR-Chunk-Offset": "0"})
        response = await self._forward(respond)
        self.assertEqual(response.body, b"safe result")
        self.assertEqual(deletes, 2)

    async def test_cleanup_logs_persistent_http_failure_without_losing_result(self):
        def respond(request):
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                return httpx.Response(503)
            return httpx.Response(200, content=b"safe result", headers={"X-ASR-Chunk-Offset": "0"})
        with self.assertLogs("whisperx-adapter.result_transfer", level="WARNING") as captured:
            response = await self._forward(respond)
        self.assertEqual(response.body, b"safe result")
        self.assertIn("reason=status=503", "\n".join(captured.output))

    async def test_checksum_mismatch_rejected(self):
        def respond(request):
            if request.method == "POST":
                return manifest(b"expected")
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, content=b"modified", headers={"X-ASR-Chunk-Offset": "0"})
        with self.assertRaisesRegex(TemporaryRunPodError, "checksum mismatch"):
            await self._forward(respond)

    async def test_cancelled_chunk_fetch_logs_phase(self):
        def respond(request):
            if request.method == "POST":
                return manifest(b"safe result")
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, stream=NeverEndingResponse())
        with self.assertLogs("whisperx-adapter.proxy", level="INFO") as captured:
            task = asyncio.create_task(self._forward(respond))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn("phase=result_chunks", "\n".join(captured.output))

    async def test_413_preserves_error_detail(self):
        with self.assertLogs("whisperx-adapter.proxy", level="INFO"):
            with self.assertRaises(HTTPException) as raised:
                await self._forward(lambda request: httpx.Response(413, text="too large"))
        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail, "too large")


if __name__ == "__main__":
    unittest.main()
