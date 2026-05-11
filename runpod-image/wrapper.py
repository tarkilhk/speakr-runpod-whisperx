import hmac
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

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

app = FastAPI(title="RunPod WhisperX Auth Wrapper")


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

    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.request(
            request.method,
            f"{UPSTREAM}/{path}",
            params=request.query_params,
            headers=headers,
            content=request.stream(),
        )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=forwarded_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
