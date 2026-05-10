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
from speakr_common.http_client_logging import configure_http_client_log_redaction
from speakr_common.uvicorn_access import QuietUvicornAccessFilter


config = AdapterConfig.from_env()
logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
configure_http_client_log_redaction()
logging.getLogger("uvicorn.access").addFilter(QuietUvicornAccessFilter())
logger = logging.getLogger("whisperx-adapter")
app = FastAPI(title="Speakr RunPod WhisperX Adapter")
runpod = RunPodManager(config)

# Idle shutdown is tied only to /asr (not /health): count concurrent ASR handlers and arm a
# timer when the last one finishes so RunPod can terminate/stop after RUNPOD_IDLE_STOP_SECONDS.
_idle_stop_task: asyncio.Task | None = None
_active_requests = 0


def _schedule_idle_release() -> None:
    global _idle_stop_task
    # Each completed /asr schedules a fresh timer; cancel the previous so idle is measured
    # from the last request end, not the first.
    replaced = bool(_idle_stop_task and not _idle_stop_task.done())
    if replaced:
        _idle_stop_task.cancel()
    delay = config.runpod_idle_stop_seconds
    logger.info(
        "Idle timer %s delay=%ss action=%s in_flight=%s pod=%s",
        "reset" if replaced else "started",
        delay,
        config.idle_action,
        _active_requests,
        runpod.load_active_pod_id() or "(none)",
    )
    _idle_stop_task = asyncio.create_task(_release_after_idle_delay())


async def _release_after_idle_delay() -> None:
    try:
        delay = config.runpod_idle_stop_seconds
        if delay > 0:
            await asyncio.sleep(delay)
        # Another /asr may have started during the sleep; only release when truly quiet.
        if _active_requests != 0:
            logger.info(
                "Idle skipped: in_flight=%s pod=%s",
                _active_requests,
                runpod.load_active_pod_id() or "(none)",
            )
            return
        logger.info(
            "Idle release pod=%s action=%s delay=%ss template_mode=%s",
            runpod.load_active_pod_id() or "(none)",
            config.idle_action,
            delay,
            config.template_mode_enabled,
        )
        await runpod.release_idle_pod()
    except asyncio.CancelledError:
        # Expected when a new /asr finishes and replaces this task, or on process shutdown.
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
    # Work in progress: do not let a sleeping idle timer fire mid-request.
    # Idle timer reset/start stays at INFO below; per-request cancel is DEBUG to avoid doubling noise.
    if _idle_stop_task and not _idle_stop_task.done():
        _idle_stop_task.cancel()
        logger.debug(
            "Idle timer cancelled (ASR started) in_flight=%s pod=%s",
            _active_requests,
            runpod.load_active_pod_id() or "(none)",
        )

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
        _schedule_idle_release()  # (re)arm idle shutdown from this completion time
        body_path.unlink(missing_ok=True)
