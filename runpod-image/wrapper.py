import hmac
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


UPSTREAM = os.getenv("WHISPERX_UPSTREAM_URL", "http://127.0.0.1:9001").rstrip("/")
ADAPTER_WHISPERX_TOKEN = os.getenv("ADAPTER_WHISPERX_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("WRAPPER_REQUEST_TIMEOUT_SECONDS", "3600"))


class _QuietUvicornAccessFilter(logging.Filter):
    """Swagger UI polls /docs and /openapi.json; omit those from access logs."""

    _SKIP = (' "GET /docs ', ' "GET /openapi.json ', ' "GET /redoc ')

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(fragment in msg for fragment in self._SKIP)


logging.getLogger("uvicorn.access").addFilter(_QuietUvicornAccessFilter())

app = FastAPI(title="RunPod WhisperX Auth Wrapper")


def _authorized(request: Request) -> bool:
    expected = f"Bearer {ADAPTER_WHISPERX_TOKEN}"
    provided = request.headers.get("authorization", "")
    return bool(ADAPTER_WHISPERX_TOKEN) and hmac.compare_digest(provided, expected)


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

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {
            "authorization",
            "host",
            "connection",
            "content-length",
            "transfer-encoding",
        }
    }
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
        headers={
            key: value
            for key, value in upstream.headers.items()
            if key.lower()
            not in {"content-encoding", "content-length", "transfer-encoding", "connection", "content-type"}
        },
        media_type=upstream.headers.get("content-type"),
    )
