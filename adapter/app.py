import asyncio
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from adapter.config import AdapterConfig
from adapter.errors import (
    BadUpstreamResponseError,
    ConfigurationError,
    RunPodNotFoundError,
    RunPodTimeoutError,
    TemporaryRunPodError,
)
from adapter.proxy import forward_asr, spool_request_body
from adapter.runpod import RunPodManager


class _QuietUvicornAccessFilter(logging.Filter):
    """Swagger UI polls /docs and /openapi.json; omit those from access logs."""

    _SKIP = (' "GET /docs ', ' "GET /openapi.json ', ' "GET /redoc ')

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(fragment in msg for fragment in self._SKIP)


config = AdapterConfig.from_env()
logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").addFilter(_QuietUvicornAccessFilter())
logger = logging.getLogger("whisperx-adapter")
app = FastAPI(title="Speakr RunPod WhisperX Adapter")
runpod = RunPodManager(config)

_idle_stop_task: asyncio.Task | None = None
_active_requests = 0


def _schedule_idle_release() -> None:
    global _idle_stop_task
    replaced = bool(_idle_stop_task and not _idle_stop_task.done())
    if replaced:
        _idle_stop_task.cancel()
    delay = config.runpod_idle_stop_seconds
    logger.info(
        "Idle release timer %s; will run after %ss quiet time (action=%s)",
        "reset" if replaced else "started",
        delay,
        config.idle_action,
    )
    _idle_stop_task = asyncio.create_task(_release_after_idle_delay())


async def _release_after_idle_delay() -> None:
    try:
        delay = config.runpod_idle_stop_seconds
        if delay > 0:
            await asyncio.sleep(delay)
        if _active_requests != 0:
            logger.info(
                "Idle release skipped: %s ASR request(s) still in flight after wait",
                _active_requests,
            )
            return
        logger.info(
            "Idle threshold reached; releasing RunPod (action=%s, idle_stop_seconds=%s)",
            config.idle_action,
            delay,
        )
        await runpod.release_idle_pod()
    except asyncio.CancelledError:
        logger.debug("Idle release timer cancelled (superseded by newer timer or shutdown)")
        return


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "healthy", **runpod.health_status()}


@app.post("/asr")
async def asr(request: Request) -> Response:
    global _active_requests
    body_path = await spool_request_body(request, config.max_file_size_mb)
    _active_requests += 1
    if _idle_stop_task and not _idle_stop_task.done():
        _idle_stop_task.cancel()

    try:
        base_url = await runpod.ensure_ready()
        return await forward_asr(base_url, request, body_path, config)
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RunPodTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (TemporaryRunPodError, RunPodNotFoundError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": str(config.runpod_retry_after_seconds)},
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="RunPod transcription timed out") from exc
    except BadUpstreamResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        _active_requests -= 1
        _schedule_idle_release()
        body_path.unlink(missing_ok=True)
