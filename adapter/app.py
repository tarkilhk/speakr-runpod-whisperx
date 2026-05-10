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
from adapter.idle import IdleReleaseController
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
app = FastAPI(title="Speakr RunPod WhisperX Adapter")
runpod = RunPodManager(config)
idle_release = IdleReleaseController(config, runpod)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "healthy", **runpod.health_status()}


@app.post("/asr")
async def asr(request: Request) -> Response:
    body_path = await spool_request_body(request, config.max_file_size_mb)
    idle_release.request_started()
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
        idle_release.request_finished()
        body_path.unlink(missing_ok=True)
