import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response


RUNPOD_API_BASE = os.getenv("RUNPOD_API_BASE", "https://rest.runpod.io/v1").rstrip("/")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_POD_ID = os.getenv("RUNPOD_POD_ID", "")
ADAPTER_WHISPERX_TOKEN = os.getenv("ADAPTER_WHISPERX_TOKEN", "")
RUNPOD_WRAPPER_PORT = int(os.getenv("RUNPOD_WRAPPER_PORT", "9000"))
RUNPOD_READINESS_TIMEOUT_SECONDS = int(os.getenv("RUNPOD_READINESS_TIMEOUT_SECONDS", "600"))
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "5"))
RUNPOD_REQUEST_TIMEOUT_SECONDS = int(os.getenv("RUNPOD_REQUEST_TIMEOUT_SECONDS", "1800"))
RUNPOD_IDLE_STOP_SECONDS = int(os.getenv("RUNPOD_IDLE_STOP_SECONDS", "900"))
RUNPOD_RETRY_AFTER_SECONDS = int(os.getenv("RUNPOD_RETRY_AFTER_SECONDS", "300"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "0"))

app = FastAPI(title="Speakr RunPod WhisperX Adapter")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whisperx-adapter")

_pod_lock = asyncio.Lock()
_idle_stop_task: asyncio.Task | None = None
_active_requests = 0


class TemporaryRunPodError(Exception):
    pass


class RunPodTimeoutError(Exception):
    pass


class BadUpstreamResponseError(Exception):
    pass


class ConfigurationError(Exception):
    pass


def _api_headers() -> dict[str, str]:
    if not RUNPOD_API_KEY:
        raise ConfigurationError("RUNPOD_API_KEY is not configured")
    return {"Authorization": f"Bearer {RUNPOD_API_KEY}"}


def _configured() -> bool:
    return bool(RUNPOD_API_KEY and RUNPOD_POD_ID and ADAPTER_WHISPERX_TOKEN)


async def _runpod_request(method: str, path: str) -> dict[str, Any]:
    timeout = httpx.Timeout(60, connect=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            f"{RUNPOD_API_BASE}{path}",
            headers=_api_headers(),
        )
    if response.status_code >= 500 or response.status_code in {408, 409, 423, 429}:
        raise TemporaryRunPodError(
            f"RunPod API {method} {path} returned {response.status_code}: {response.text[:500]}"
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"RunPod API {method} {path} returned {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise BadUpstreamResponseError("RunPod API returned non-JSON response") from exc
    return payload if isinstance(payload, dict) else {"data": payload}


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_dicts(item))
    return found


def _extract_tcp_mapping(pod: dict[str, Any]) -> tuple[str, int] | None:
    for item in _walk_dicts(pod):
        mapping = item.get("portMappings")
        if isinstance(mapping, dict):
            for key, value in mapping.items():
                if str(key).split("/")[0] != str(RUNPOD_WRAPPER_PORT):
                    continue
                if isinstance(value, int):
                    host = _extract_public_ip(pod)
                    return (host, value) if host else None
                if isinstance(value, str) and value.isdigit():
                    host = _extract_public_ip(pod)
                    return (host, int(value)) if host else None
                if isinstance(value, dict):
                    port = _extract_public_port(value)
                    host = _extract_public_ip(value) or _extract_public_ip(pod)
                    return (host, port) if host and port else None

        ports = item.get("ports")
        if isinstance(ports, list):
            for port_item in ports:
                if not isinstance(port_item, dict):
                    continue
                private_port = (
                    port_item.get("privatePort")
                    or port_item.get("containerPort")
                    or port_item.get("internalPort")
                    or port_item.get("port")
                )
                if str(private_port) != str(RUNPOD_WRAPPER_PORT):
                    continue
                public_port = _extract_public_port(port_item)
                host = _extract_public_ip(port_item) or _extract_public_ip(pod)
                if host and public_port:
                    return host, public_port

    return None


def _extract_public_ip(value: dict[str, Any]) -> str | None:
    for key in ("publicIp", "publicIP", "ip", "host"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    runtime = value.get("runtime")
    if isinstance(runtime, dict):
        return _extract_public_ip(runtime)
    return None


def _extract_public_port(value: dict[str, Any]) -> int | None:
    for key in ("publicPort", "hostPort", "externalPort"):
        candidate = value.get(key)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


async def _wrapper_healthy(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{base_url}/health")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def _ensure_pod_ready() -> str:
    if not _configured():
        raise ConfigurationError(
            "RunPod adapter is not configured; set RUNPOD_API_KEY, RUNPOD_POD_ID, and ADAPTER_WHISPERX_TOKEN"
        )

    async with _pod_lock:
        logger.info("Inspecting RunPod pod %s", RUNPOD_POD_ID)
        pod = await _runpod_request("GET", f"/pods/{RUNPOD_POD_ID}")
        mapping = _extract_tcp_mapping(pod)
        if mapping:
            base_url = f"http://{mapping[0]}:{mapping[1]}"
            logger.info("Discovered RunPod TCP mapping: %s", base_url)
            if await _wrapper_healthy(base_url):
                logger.info("RunPod wrapper is already healthy")
                return base_url
            logger.info("RunPod wrapper not healthy yet; waiting without calling start")
        else:
            logger.info("No RunPod TCP mapping found; starting pod %s", RUNPOD_POD_ID)
            await _runpod_request("POST", f"/pods/{RUNPOD_POD_ID}/start")

        deadline = asyncio.get_running_loop().time() + RUNPOD_READINESS_TIMEOUT_SECONDS
        last_error = "Pod is not ready"

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
            pod = await _runpod_request("GET", f"/pods/{RUNPOD_POD_ID}")
            mapping = _extract_tcp_mapping(pod)
            if not mapping:
                last_error = "RunPod has not assigned a public TCP mapping yet"
                logger.info(last_error)
                continue

            base_url = f"http://{mapping[0]}:{mapping[1]}"
            logger.info("Polling RunPod wrapper health at %s", base_url)
            if await _wrapper_healthy(base_url):
                logger.info("RunPod wrapper is healthy")
                return base_url
            last_error = f"Wrapper is not healthy at {base_url}"

        raise RunPodTimeoutError(last_error)


async def _stop_pod() -> None:
    if not (RUNPOD_API_KEY and RUNPOD_POD_ID):
        return
    try:
        logger.info("Stopping RunPod pod %s", RUNPOD_POD_ID)
        await _runpod_request("POST", f"/pods/{RUNPOD_POD_ID}/stop")
        logger.info("RunPod pod stop request completed")
    except Exception as exc:
        # The external watchdog is the backstop; do not crash the adapter.
        logger.warning("Failed to stop RunPod pod %s: %s", RUNPOD_POD_ID, exc)
        return


def _schedule_idle_stop() -> None:
    global _idle_stop_task
    if RUNPOD_IDLE_STOP_SECONDS <= 0:
        return
    if _idle_stop_task and not _idle_stop_task.done():
        _idle_stop_task.cancel()
    _idle_stop_task = asyncio.create_task(_idle_stop_after_delay())


async def _idle_stop_after_delay() -> None:
    try:
        await asyncio.sleep(RUNPOD_IDLE_STOP_SECONDS)
        if _active_requests == 0:
            logger.info(
                "Idle timer fired after %s seconds; stopping RunPod pod",
                RUNPOD_IDLE_STOP_SECONDS,
            )
            await _stop_pod()
    except asyncio.CancelledError:
        return


async def _write_request_body(request: Request) -> Path:
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024 if MAX_FILE_SIZE_MB > 0 else 0
    handle = tempfile.NamedTemporaryFile(prefix="speakr-asr-", suffix=".request", delete=False)
    path = Path(handle.name)
    written = 0
    try:
        async for chunk in request.stream():
            written += len(chunk)
            if max_bytes and written > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body exceeds {MAX_FILE_SIZE_MB} MB limit",
                )
            handle.write(chunk)
    except BaseException:
        handle.close()
        path.unlink(missing_ok=True)
        raise
    handle.close()
    return path


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            yield chunk


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "configured": _configured(),
        "pod_id_configured": bool(RUNPOD_POD_ID),
    }


@app.post("/asr")
async def asr(request: Request) -> Response:
    global _active_requests
    body_path = await _write_request_body(request)
    _active_requests += 1
    if _idle_stop_task and not _idle_stop_task.done():
        _idle_stop_task.cancel()

    try:
        base_url = await _ensure_pod_ready()
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
        headers["Authorization"] = f"Bearer {ADAPTER_WHISPERX_TOKEN}"
        timeout = httpx.Timeout(
            RUNPOD_REQUEST_TIMEOUT_SECONDS,
            connect=60,
            read=RUNPOD_REQUEST_TIMEOUT_SECONDS,
            write=RUNPOD_REQUEST_TIMEOUT_SECONDS,
            pool=60,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(
                f"{base_url}/asr",
                params=request.query_params,
                headers=headers,
                content=_file_chunks(body_path),
            )

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
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RunPodTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (TemporaryRunPodError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": str(RUNPOD_RETRY_AFTER_SECONDS)},
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="RunPod transcription timed out") from exc
    except BadUpstreamResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        _active_requests -= 1
        _schedule_idle_stop()
        body_path.unlink(missing_ok=True)
