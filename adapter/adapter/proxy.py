import asyncio
import logging
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

from adapter.config import AdapterConfig
from adapter.errors import BadUpstreamResponseError, TemporaryRunPodError
from speakr_common.proxy_headers import forwarded_request_headers


logger = logging.getLogger("whisperx-adapter.proxy")


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
    headers = forwarded_request_headers(request.headers, authorization_token=config.adapter_whisperx_token)
    timeout = httpx.Timeout(
        config.runpod_request_timeout_seconds,
        connect=60,
        read=config.runpod_request_timeout_seconds,
        write=config.runpod_request_timeout_seconds,
        pool=60,
    )
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    phase = "response_headers"
    status: int | None = None
    decoded_bytes = 0
    logger.info("ASR upstream request started id=%s read_timeout_s=%s", request_id, timeout.read)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream(
                "POST",
                f"{base_url}/asr",
                params=request.query_params,
                headers=headers,
                content=_file_chunks(body_path),
            ) as upstream:
                status = upstream.status_code
                phase = "response_body"
                logger.info(
                    "ASR upstream headers received id=%s status=%s content_length=%s "
                    "transfer_encoding=%s content_encoding=%s elapsed_s=%.3f",
                    request_id, status, upstream.headers.get("content-length"),
                    upstream.headers.get("transfer-encoding"), upstream.headers.get("content-encoding"),
                    time.monotonic() - started,
                )
                body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    body.extend(chunk)
                    decoded_bytes += len(chunk)
                logger.info(
                    "ASR upstream body complete id=%s status=%s decoded_body_bytes=%d elapsed_s=%.3f",
                    request_id, status, decoded_bytes, time.monotonic() - started,
                )
                return _response_from_upstream(upstream, bytes(body))
        except httpx.HTTPError as exc:
            logger.warning(
                "ASR upstream request failed id=%s phase=%s status=%s decoded_body_bytes=%d "
                "elapsed_s=%.3f error=%s",
                request_id, phase, status, decoded_bytes, time.monotonic() - started,
                type(exc).__name__,
            )
            raise
        except asyncio.CancelledError:
            logger.warning(
                "ASR upstream request cancelled id=%s phase=%s status=%s decoded_body_bytes=%d "
                "elapsed_s=%.3f",
                request_id, phase, status, decoded_bytes, time.monotonic() - started,
            )
            raise


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            yield chunk


def _response_from_upstream(upstream: httpx.Response, body: bytes) -> Response:
    if upstream.status_code >= 500:
        raise TemporaryRunPodError(f"WhisperX returned {upstream.status_code}")
    if upstream.status_code == 413:
        raise HTTPException(status_code=413, detail=body.decode(upstream.encoding or "utf-8", errors="replace"))
    if upstream.status_code >= 400:
        return Response(
            content=body,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    if "application/json" not in upstream.headers.get("content-type", ""):
        raise BadUpstreamResponseError("WhisperX returned non-JSON response")

    return Response(
        content=body,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
