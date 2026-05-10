import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

from adapter.config import AdapterConfig
from adapter.errors import BadUpstreamResponseError, TemporaryRunPodError


async def spool_request_body(request: Request, max_file_size_mb: int) -> Path:
    max_bytes = max_file_size_mb * 1024 * 1024 if max_file_size_mb > 0 else 0
    handle = tempfile.NamedTemporaryFile(prefix="speakr-asr-", suffix=".request", delete=False)
    path = Path(handle.name)
    written = 0
    try:
        async for chunk in request.stream():
            written += len(chunk)
            if max_bytes and written > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body exceeds {max_file_size_mb} MB limit",
                )
            handle.write(chunk)
    except BaseException:
        handle.close()
        path.unlink(missing_ok=True)
        raise
    handle.close()
    return path


async def forward_asr(base_url: str, request: Request, body_path: Path, config: AdapterConfig) -> Response:
    headers = _forward_headers(request, config.adapter_whisperx_token)
    timeout = httpx.Timeout(
        config.runpod_request_timeout_seconds,
        connect=60,
        read=config.runpod_request_timeout_seconds,
        write=config.runpod_request_timeout_seconds,
        pool=60,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.post(
            f"{base_url}/asr",
            params=request.query_params,
            headers=headers,
            content=_file_chunks(body_path),
        )

    return _response_from_upstream(upstream)


def _forward_headers(request: Request, token: str) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {
            "host",
            "connection",
            "content-length",
            "transfer-encoding",
            "authorization",
        }
    }
    headers["Authorization"] = f"Bearer {token}"
    return headers


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            yield chunk


def _response_from_upstream(upstream: httpx.Response) -> Response:
    if upstream.status_code >= 500:
        raise TemporaryRunPodError(f"WhisperX returned {upstream.status_code}")
    if upstream.status_code == 413:
        raise HTTPException(status_code=413, detail=upstream.text)
    if upstream.status_code >= 400:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    if "application/json" not in upstream.headers.get("content-type", ""):
        raise BadUpstreamResponseError("WhisperX returned non-JSON response")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
