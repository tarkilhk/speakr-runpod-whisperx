"""Retrieve a buffered WhisperX JSON result over small, independently retryable requests."""

import asyncio
import hashlib
import json
import logging
import re
import time

import httpx

from adapter.errors import TemporaryRunPodError
from speakr_common.asr_result_protocol import (
    CHUNK_SIZE, MAX_MANIFEST_BYTES, MAX_RESULT_BYTES,
)

logger = logging.getLogger("whisperx-adapter.result_transfer")
CLEANUP_DEADLINE_SECONDS = 2


async def read_manifest(response: httpx.Response) -> dict[str, object]:
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        if not declared_length.isdecimal() or int(declared_length) > MAX_MANIFEST_BYTES:
            raise TemporaryRunPodError("RunPod ASR manifest exceeds limit")
    data = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > MAX_MANIFEST_BYTES:
                    raise TemporaryRunPodError("RunPod ASR manifest exceeds limit")
    except TimeoutError as exc:
        raise TemporaryRunPodError("RunPod ASR manifest stalled") from exc
    try:
        manifest = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise TemporaryRunPodError("RunPod ASR manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise TemporaryRunPodError("RunPod ASR manifest is invalid")
    result_id = manifest.get("id")
    length = manifest.get("length")
    digest = manifest.get("sha256")
    if (not isinstance(result_id, str) or re.fullmatch(r"[0-9a-f]{32}", result_id) is None
            or type(length) is not int or not 0 < length <= MAX_RESULT_BYTES
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise TemporaryRunPodError("RunPod ASR manifest is invalid")
    return manifest


async def fetch_result(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str],
    manifest: dict[str, object], request_id: str, *, deadline: float,
) -> bytes:
    result_id = str(manifest["id"])
    length = int(manifest["length"])
    expected_digest = str(manifest["sha256"])
    url = f"{base_url}/internal/asr-results/{result_id}"
    auth_headers = {"Authorization": headers["Authorization"], "X-ASR-Trace-ID": request_id}
    body = bytearray()
    try:
        for offset in range(0, length, CHUNK_SIZE):
            expected = min(CHUNK_SIZE, length - offset)
            for attempt in range(1, 4):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TemporaryRunPodError("RunPod ASR result retrieval deadline exceeded")
                received: bytes | None = None
                error = "invalid_chunk"
                try:
                    async with asyncio.timeout(min(15, remaining)):
                        async with client.stream(
                            "GET", url, params={"offset": offset, "limit": expected},
                            headers=auth_headers, timeout=httpx.Timeout(min(10, remaining)),
                        ) as chunk:
                            if chunk.status_code == 404:
                                raise TemporaryRunPodError("RunPod ASR result expired before retrieval")
                            if chunk.status_code in (401, 403):
                                raise TemporaryRunPodError("RunPod ASR result authorization failed")
                            if chunk.status_code != 200 or chunk.headers.get("x-asr-chunk-offset") != str(offset):
                                error = f"status={chunk.status_code} offset_mismatch={chunk.headers.get('x-asr-chunk-offset') != str(offset)}"
                            else:
                                declared = chunk.headers.get("content-length")
                                if declared is not None and (not declared.isdecimal() or int(declared) > expected):
                                    error = "oversize_header"
                                else:
                                    data = bytearray()
                                    async for part in chunk.aiter_bytes(chunk_size=CHUNK_SIZE):
                                        data.extend(part)
                                        if len(data) > expected:
                                            error = "oversize_body"
                                            break
                                    if len(data) == expected:
                                        received = bytes(data)
                                    elif error != "oversize_body":
                                        error = f"short_body_bytes={len(data)}"
                except (httpx.RequestError, TimeoutError) as exc:
                    error = type(exc).__name__
                if received is not None:
                    body.extend(received)
                    break
                logger.warning(
                    "ASR result chunk retry id=%s offset=%d attempt=%d error=%s",
                    request_id, offset, attempt, error,
                )
                if attempt == 3:
                    raise TemporaryRunPodError("RunPod ASR result chunk failed after retries")
                await asyncio.sleep(min(0.5 * attempt, max(0, deadline - time.monotonic())))
        if hashlib.sha256(body).hexdigest() != expected_digest:
            raise TemporaryRunPodError("RunPod ASR result checksum mismatch")
        logger.info("ASR result verified id=%s bytes=%d chunks=%d", request_id, length, (length + CHUNK_SIZE - 1) // CHUNK_SIZE)
        return bytes(body)
    finally:
        # Best effort with an absolute deadline; never hold up a verified result.
        cleaned = False
        reason = "unknown"
        try:
            async with asyncio.timeout(CLEANUP_DEADLINE_SECONDS):
                for attempt in range(2):
                    try:
                        async with client.stream(
                            "DELETE", url, headers=auth_headers, timeout=CLEANUP_DEADLINE_SECONDS,
                        ) as cleanup:
                            status = cleanup.status_code
                        if status in (204, 404):
                            cleaned = True
                            break
                        reason = f"status={status}"
                        if status in (401, 403):
                            break
                    except httpx.RequestError as exc:
                        reason = type(exc).__name__
                    if attempt == 0:
                        await asyncio.sleep(0.1)
        except TimeoutError:
            reason = "TimeoutError"
        if not cleaned:
            logger.warning("ASR result cleanup deferred to TTL id=%s reason=%s", request_id, reason)
