import hmac
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from speakr_common.http_client_logging import configure_http_client_log_redaction
from speakr_common.proxy_headers import forwarded_request_headers, forwarded_response_headers
from speakr_common.uvicorn_access import QuietUvicornAccessFilter


UPSTREAM = os.getenv("WHISPERX_UPSTREAM_URL", "http://127.0.0.1:9001").rstrip("/")
ADAPTER_WHISPERX_TOKEN = os.getenv("ADAPTER_WHISPERX_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("WRAPPER_REQUEST_TIMEOUT_SECONDS", "3600"))
WRAPPER_POD_LOGS_DIR = Path(os.getenv("WRAPPER_POD_LOGS_DIR", "/var/log/whisperx-pod")).resolve()
WRAPPER_POD_LOGS_MAX_BYTES = max(0, int(os.getenv("WRAPPER_POD_LOGS_MAX_BYTES", str(4 * 1024 * 1024))))
ALLOWED_LOG_BASENAMES = frozenset(
    {
        "whisperx-stdout.log",
        "whisperx-stderr.log",
        "wrapper-stdout.log",
        "wrapper-stderr.log",
    },
)

if not logging.root.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
configure_http_client_log_redaction()
logging.getLogger("uvicorn.access").addFilter(QuietUvicornAccessFilter())
_wr_logger = logging.getLogger("whisperx-wrapper")


def _trace_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() == b"x-asr-trace-id":
            candidate = value.decode("ascii", errors="ignore")
            return candidate if re.fullmatch(r"[0-9a-f]{12}", candidate) else "untracked"
    return "untracked"


class AsrResponseTraceMiddleware:
    """Measure ASGI handoff, not delivery to the client over TCP."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/asr" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        trace_id = _trace_id(scope)
        started = time.monotonic()
        status: int | None = None
        attempted_bytes = 0
        handed_off_bytes = 0

        async def traced_send(message: Message) -> None:
            nonlocal status, attempted_bytes, handed_off_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
                content_length = next(
                    (value.decode("ascii", errors="replace") for name, value in message.get("headers", [])
                     if name.lower() == b"content-length"),
                    None,
                )
                _wr_logger.info(
                    "ASR response headers handoff started id=%s status=%s content_length=%s elapsed_s=%.3f",
                    trace_id, status, content_length, time.monotonic() - started,
                )
            elif message["type"] == "http.response.body":
                chunk_bytes = len(message.get("body", b""))
                attempted_bytes += chunk_bytes
                _wr_logger.info(
                    "ASR response body handoff started id=%s status=%s chunk_bytes=%d "
                    "attempted_bytes=%d elapsed_s=%.3f",
                    trace_id, status, chunk_bytes, attempted_bytes, time.monotonic() - started,
                )
            await send(message)
            if message["type"] == "http.response.start":
                _wr_logger.info(
                    "ASR response headers handoff returned id=%s status=%s elapsed_s=%.3f",
                    trace_id, status, time.monotonic() - started,
                )
            elif message["type"] == "http.response.body":
                handed_off_bytes += chunk_bytes
                _wr_logger.info(
                    "ASR response body handoff returned id=%s status=%s handed_off_bytes=%d "
                    "final=%s elapsed_s=%.3f",
                    trace_id, status, handed_off_bytes, not message.get("more_body", False),
                    time.monotonic() - started,
                )

        try:
            await self.app(scope, receive, traced_send)
        except BaseException as exc:
            _wr_logger.warning(
                "ASR response handoff failed id=%s status=%s attempted_bytes=%d "
                "handed_off_bytes=%d elapsed_s=%.3f error=%s",
                trace_id, status, attempted_bytes, handed_off_bytes,
                time.monotonic() - started, type(exc).__name__,
            )
            raise


app = FastAPI(title="RunPod WhisperX Auth Wrapper")
app.add_middleware(AsrResponseTraceMiddleware)


def _authorized(request: Request) -> bool:
    expected = f"Bearer {ADAPTER_WHISPERX_TOKEN}"
    provided = request.headers.get("authorization", "")
    return bool(ADAPTER_WHISPERX_TOKEN) and hmac.compare_digest(provided, expected)


def _resolved_log_file(name: str) -> Path | None:
    if name not in ALLOWED_LOG_BASENAMES:
        return None
    candidate = (WRAPPER_POD_LOGS_DIR / name).resolve()
    try:
        candidate.relative_to(WRAPPER_POD_LOGS_DIR)
    except ValueError:
        return None
    return candidate


def _tail_utf8_text(path: Path, max_bytes: int) -> str:
    if max_bytes <= 0 or not path.is_file():
        return ""
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read()
    return raw.decode("utf-8", errors="replace")


@app.get("/internal/pod-logs")
async def internal_pod_logs(request: Request) -> dict[str, Any]:
    """Return bounded tails of supervisor-managed log files for adapter drain (e.g. Alloy → Loki)."""
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    files_out: list[dict[str, Any]] = []
    for name in sorted(ALLOWED_LOG_BASENAMES):
        path = _resolved_log_file(name)
        if path is None or not path.is_file():
            files_out.append({"name": name, "content": ""})
            continue
        try:
            text = _tail_utf8_text(path, WRAPPER_POD_LOGS_MAX_BYTES)
        except OSError as exc:
            _wr_logger.warning("read log %s: %s", name, exc)
            text = ""
        files_out.append({"name": name, "content": text})

    return {"files": files_out}


@app.get("/health")
async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            upstream = await client.get(f"{UPSTREAM}/health")
            upstream.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="WhisperX not ready") from exc

    return {"status": "healthy"}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST"],
)
async def proxy(path: str, request: Request) -> Response:
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    headers = forwarded_request_headers(request.headers)
    timeout = httpx.Timeout(
        REQUEST_TIMEOUT_SECONDS,
        connect=60,
        read=REQUEST_TIMEOUT_SECONDS,
        write=REQUEST_TIMEOUT_SECONDS,
        pool=60,
    )

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.request(
            request.method,
            f"{UPSTREAM}/{path}",
            params=request.query_params,
            headers=headers,
            content=request.stream(),
        )

    if path == "asr":
        _wr_logger.info(
            "ASR upstream buffered id=%s status=%s body_bytes=%d elapsed_s=%.3f",
            _trace_id(request.scope), upstream.status_code, len(upstream.content),
            time.monotonic() - started,
        )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=forwarded_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
