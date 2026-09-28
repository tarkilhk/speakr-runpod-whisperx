"""Response-boundary diagnostics in the RunPod wrapper (no pod or audio)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

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
