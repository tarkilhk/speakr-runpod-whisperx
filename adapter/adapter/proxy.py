import asyncio
import logging
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

from adapter.config import AdapterConfig
from adapter.errors import BadUpstreamResponseError, TemporaryRunPodError
from adapter.result_transfer import fetch_result, read_manifest
from speakr_common.asr_result_protocol import PROTOCOL_HEADER, PROTOCOL_VERSION
from speakr_common.proxy_headers import forwarded_request_headers


logger = logging.getLogger("whisperx-adapter.proxy")
BODY_WAIT_LOG_INTERVAL_SECONDS = 30


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
    request_id = uuid4().hex[:12]
    headers = forwarded_request_headers(
        request.headers,
        authorization_token=config.adapter_whisperx_token,
        extra_excluded=("x-asr-trace-id", PROTOCOL_HEADER),
    )
    headers["X-ASR-Trace-ID"] = request_id
    headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
    timeout = httpx.Timeout(
        config.runpod_request_timeout_seconds,
        connect=60,
        read=config.runpod_request_timeout_seconds,
        write=config.runpod_request_timeout_seconds,
        pool=60,
    )
    started = time.monotonic()
    phase = "response_headers"
    status: int | None = None
    decoded_bytes = 0
    last_progress_bytes = 0
    last_progress_at = started
    logger.info("ASR upstream request started id=%s read_timeout_s=%s", request_id, timeout.read)
    # A fresh TCP connection per chunk prevents a stalled large-response socket from being reused.
    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_keepalive_connections=0)) as client:
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
                if status in (200, 202):
                    if status != 202 or upstream.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
                        raise TemporaryRunPodError("RunPod wrapper does not support bounded ASR result retrieval")
                    phase = "result_manifest"
                    manifest = await read_manifest(upstream)
                    phase = "result_chunks"
                    body = await fetch_result(
                        client, base_url, headers, manifest, request_id,
                        deadline=started + config.runpod_request_timeout_seconds,
                    )
                    decoded_bytes = len(body)
                    logger.info(
                        "ASR upstream body complete id=%s status=200 decoded_body_bytes=%d elapsed_s=%.3f",
                        request_id, decoded_bytes, time.monotonic() - started,
                    )
                    return Response(content=body, status_code=200, media_type="application/json")

                body = bytearray()
                last_chunk_at = time.monotonic()

                async def log_waiting() -> None:
                    while True:
                        await asyncio.sleep(BODY_WAIT_LOG_INTERVAL_SECONDS)
                        now = time.monotonic()
                        logger.info(
                            "ASR upstream body waiting id=%s status=%s decoded_body_bytes=%d "
                            "since_last_chunk_s=%.1f elapsed_s=%.3f",
                            request_id, status, decoded_bytes, now - last_chunk_at, now - started,
                        )

                heartbeat = asyncio.create_task(log_waiting())
                try:
                    async for chunk in upstream.aiter_bytes():
                        body.extend(chunk)
                        decoded_bytes += len(chunk)
                        now = time.monotonic()
                        last_chunk_at = now
                        if decoded_bytes and (
                            last_progress_bytes == 0
                            or decoded_bytes - last_progress_bytes >= 64 * 1024
                            or now - last_progress_at >= 15
                        ):
                            logger.info(
                                "ASR upstream body progress id=%s status=%s decoded_body_bytes=%d elapsed_s=%.3f",
                                request_id, status, decoded_bytes, now - started,
                            )
                            last_progress_bytes = decoded_bytes
                            last_progress_at = now
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
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
