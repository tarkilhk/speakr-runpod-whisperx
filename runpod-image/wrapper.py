import hmac
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from speakr_common.http_client_logging import configure_http_client_log_redaction
from speakr_common.proxy_headers import forwarded_request_headers, forwarded_response_headers
from speakr_common.uvicorn_access import QuietUvicornAccessFilter


UPSTREAM = os.getenv("WHISPERX_UPSTREAM_URL", "http://127.0.0.1:9001").rstrip("/")
ADAPTER_WHISPERX_TOKEN = os.getenv("ADAPTER_WHISPERX_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("WRAPPER_REQUEST_TIMEOUT_SECONDS", "3600"))

if not logging.root.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
configure_http_client_log_redaction()
logging.getLogger("uvicorn.access").addFilter(QuietUvicornAccessFilter())

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
