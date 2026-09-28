"""Response-boundary diagnostics in the RunPod wrapper (no pod or audio)."""

import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request
from speakr_common.asr_result_protocol import CHUNK_SIZE, PROTOCOL_HEADER, PROTOCOL_VERSION

sys.path.insert(0, str(Path(__file__).parent.parent / "runpod-image"))

import wrapper  # noqa: E402


class WrapperResponseTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_asgi_handoff_logs_counts_without_body_or_token(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"18")]})
            await send({"type": "http.response.body", "body": b"private transcript", "more_body": False})

        messages = []

        async def sink(message):
            messages.append(message)

        scope = {
            "type": "http", "method": "POST", "path": "/asr",
            "headers": [(b"x-asr-trace-id", b"abc123def456"), (b"authorization", b"Bearer private-token")],
        }
        with self.assertLogs("whisperx-wrapper", level="INFO") as captured:
            await wrapper.AsrResponseTraceMiddleware(app)(scope, AsyncMock(), sink)

        self.assertEqual(len(messages), 2)
        logs = "\n".join(captured.output)
        self.assertIn("ASR response headers handoff started id=abc123def456 status=200 content_length=18", logs)
        self.assertIn("ASR response headers handoff returned id=abc123def456 status=200", logs)
        self.assertIn("ASR response body handoff started id=abc123def456 status=200 chunk_bytes=18", logs)
        self.assertIn("ASR response body handoff returned id=abc123def456 status=200 handed_off_bytes=18 final=True", logs)
        self.assertNotIn("private transcript", logs)
        self.assertNotIn("private-token", logs)

    async def test_handoff_failure_logs_attempt_without_claiming_return(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"secret", "more_body": False})

        async def fail_body(message):
            if message["type"] == "http.response.body":
                raise OSError("secret transport detail")

        scope = {"type": "http", "method": "POST", "path": "/asr",
                 "headers": [(b"x-asr-trace-id", b"invalid\ntrace")]}
        with self.assertLogs("whisperx-wrapper", level="INFO") as captured:
            with self.assertRaises(OSError):
                await wrapper.AsrResponseTraceMiddleware(app)(scope, AsyncMock(), fail_body)

        logs = "\n".join(captured.output)
        self.assertIn("ASR response handoff failed id=untracked status=200 attempted_bytes=6 handed_off_bytes=0", logs)
        self.assertNotIn("ASR response body handoff returned", logs)
        self.assertNotIn("secret", logs)
        self.assertNotIn("invalid", logs)

    async def test_header_send_failure_does_not_claim_handoff_returned(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def fail_headers(message):
            raise OSError("private transport detail")

        scope = {"type": "http", "method": "POST", "path": "/asr",
                 "headers": [(b"x-asr-trace-id", b"abc123def456")]}
        with self.assertLogs("whisperx-wrapper", level="INFO") as captured:
            with self.assertRaises(OSError):
                await wrapper.AsrResponseTraceMiddleware(app)(scope, AsyncMock(), fail_headers)

        logs = "\n".join(captured.output)
        self.assertIn("ASR response headers handoff started id=abc123def456", logs)
        self.assertIn("ASR response handoff failed id=abc123def456 status=200 attempted_bytes=0", logs)
        self.assertNotIn("ASR response headers handoff returned", logs)
        self.assertNotIn("private transport detail", logs)

    async def test_proxy_logs_buffered_bytes_before_returning_response(self):
        upstream = httpx.Response(200, json={"text": "private transcript"})
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.request.return_value = upstream
        scope = {"type": "http", "method": "POST", "path": "/asr", "query_string": b"",
                 "headers": [(b"authorization", b"Bearer test-token"),
                             (b"x-asr-trace-id", b"abc123def456")]}
        request = Request(scope)

        with patch.object(wrapper, "ADAPTER_WHISPERX_TOKEN", "test-token"):
            with patch.object(wrapper.httpx, "AsyncClient", return_value=fake_client):
                with self.assertLogs("whisperx-wrapper", level="INFO") as captured:
                    response = await wrapper.proxy("asr", request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, upstream.content)
        logs = "\n".join(captured.output)
        self.assertIn(f"ASR upstream buffered id=abc123def456 status=200 body_bytes={len(upstream.content)}", logs)
        self.assertNotIn("private transcript", logs)
        self.assertNotIn("test-token", logs)

    async def test_authenticated_bounded_chunks_reassemble_and_delete_result(self):
        body = b'{"text":"' + b"x" * (477323 - 11) + b'"}'
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.request.return_value = httpx.Response(200, content=body, headers={"content-type": "application/json"})
        wrapper._results.clear()
        transport = httpx.ASGITransport(app=wrapper.app)
        auth = {"Authorization": "Bearer test-token", "X-ASR-Trace-ID": "abc123def456"}
        async with httpx.AsyncClient(transport=transport, base_url="http://local-wrapper") as client:
            with patch.object(wrapper, "ADAPTER_WHISPERX_TOKEN", "test-token"):
                with patch.object(wrapper.httpx, "AsyncClient", return_value=fake_client):
                    with self.assertLogs("whisperx-wrapper", level="INFO") as captured:
                        response = await client.post("/asr", content=b"synthetic audio", headers={**auth, PROTOCOL_HEADER: PROTOCOL_VERSION})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.headers[PROTOCOL_HEADER], PROTOCOL_VERSION)
                info = response.json()
                self.assertEqual(info["length"], len(body))
                self.assertEqual(info["sha256"], hashlib.sha256(body).hexdigest())
                url = f'/internal/asr-results/{info["id"]}'
                unauthorized = await client.get(url, params={"offset": 0, "limit": CHUNK_SIZE})
                self.assertEqual(unauthorized.status_code, 401)
                assembled = bytearray()
                for offset in range(0, len(body), CHUNK_SIZE):
                    part = await client.get(url, params={"offset": offset, "limit": CHUNK_SIZE}, headers=auth)
                    self.assertEqual(part.status_code, 200)
                    self.assertEqual(part.headers["x-asr-chunk-offset"], str(offset))
                    self.assertLessEqual(len(part.content), CHUNK_SIZE)
                    assembled.extend(part.content)
                self.assertEqual(bytes(assembled), body)
                out_of_range = await client.get(url, params={"offset": len(body), "limit": 1}, headers=auth)
                self.assertEqual(out_of_range.status_code, 416)
                self.assertEqual((await client.delete(url, headers=auth)).status_code, 204)
                self.assertEqual((await client.get(url, params={"offset": 0, "limit": 1}, headers=auth)).status_code, 404)
        logs = "\n".join(captured.output)
        self.assertIn(f"body_bytes={len(body)}", logs)
        self.assertNotIn("synthetic audio", logs)
        self.assertNotIn("Bearer test-token", logs)
        self.assertNotIn('"text"', logs)
        wrapper._results.clear()

    async def test_result_store_is_bounded_and_expiring(self):
        wrapper._results.clear()
        too_large = b"x" * (wrapper.MAX_RESULT_BYTES + 1)
        with self.assertRaises(Exception) as raised:
            await wrapper._store_result(too_large, "abc123def456")
        self.assertEqual(raised.exception.status_code, 503)
        with self.assertLogs("whisperx-wrapper", level="INFO"):
            info = await wrapper._store_result(b"safe", "abc123def456")
        with patch.object(wrapper.time, "monotonic", return_value=float("inf")):
            with self.assertRaises(Exception) as missing:
                await wrapper._get_result(info["id"])
        self.assertEqual(missing.exception.status_code, 404)
        wrapper._results.clear()

    async def test_actual_wrapper_app_correlates_large_buffer_and_asgi_handoff(self):
        body = b'{"text":"' + b"x" * (477323 - 11) + b'"}'
        upstream = httpx.Response(200, content=body, headers={"content-type": "application/json"})
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.request.return_value = upstream
        transport = httpx.ASGITransport(app=wrapper.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local-wrapper") as client:
            with patch.object(wrapper, "ADAPTER_WHISPERX_TOKEN", "test-token"):
                with patch.object(wrapper.httpx, "AsyncClient", return_value=fake_client):
                    with self.assertLogs("whisperx-wrapper", level="INFO") as captured:
                        response = await client.post(
                            "/asr", content=b"synthetic audio",
                            headers={"Authorization": "Bearer test-token", "X-ASR-Trace-ID": "abc123def456"},
                        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 477323)
        logs = "\n".join(captured.output)
        self.assertIn("ASR upstream buffered id=abc123def456 status=200 body_bytes=477323", logs)
        self.assertIn("ASR response headers handoff started id=abc123def456 status=200 content_length=477323", logs)
        self.assertIn("ASR response body handoff returned id=abc123def456 status=200 handed_off_bytes=477323 final=True", logs)
        self.assertNotIn("synthetic audio", logs)
        self.assertNotIn("Bearer test-token", logs)


if __name__ == "__main__":
    unittest.main()
