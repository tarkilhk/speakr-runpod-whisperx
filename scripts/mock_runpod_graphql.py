"""
Mock RunPod GraphQL API + WhisperX wrapper for local adapter testing.

Environment variables:
  ADAPTER_WHISPERX_TOKEN     Bearer token the adapter must send (default: test-token)
  MOCK_RUNPOD_PUBLIC_IP      IP returned in pod port mappings (default: 127.0.0.1)
  MOCK_RUNPOD_PUBLIC_PORT    Public port returned in pod port mappings (default: 19001)
  MOCK_RUNPOD_WRAPPER_PORT   Private port the adapter looks for (default: 9000)
  MOCK_STUCK_INIT_PODS       First N pods will be stuck (machineId set, runtime null forever).
                             Tests stuck-init redeploy once warmup fingerprint stops changing.
"""

import hashlib
import os
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from speakr_common.asr_result_protocol import CHUNK_SIZE, PROTOCOL_HEADER, PROTOCOL_VERSION

PUBLIC_IP = os.getenv("MOCK_RUNPOD_PUBLIC_IP", "127.0.0.1")
PUBLIC_PORT = int(os.getenv("MOCK_RUNPOD_PUBLIC_PORT", "19001"))
WRAPPER_PRIVATE_PORT = int(os.getenv("MOCK_RUNPOD_WRAPPER_PORT", "9000"))
ADAPTER_WHISPERX_TOKEN = os.getenv("ADAPTER_WHISPERX_TOKEN", "test-token")
MOCK_STUCK_INIT_PODS = int(os.getenv("MOCK_STUCK_INIT_PODS", "0"))

app = FastAPI(title="Mock RunPod GraphQL + WhisperX")

# Pod store: pod_id -> pod dict
pods: dict[str, dict[str, Any]] = {}
# How many stuck pods have been deployed so far
_stuck_pods_deployed = 0
results: dict[str, bytes] = {}


@app.post("/graphql")
async def graphql(request: Request) -> JSONResponse:
    global _stuck_pods_deployed

    payload = await request.json()
    query = payload.get("query", "")
    variables = payload.get("variables", {})
    input_payload = variables.get("input", {})

    if "podFindAndDeployOnDemand" in query:
        pod_id = f"mock-{uuid.uuid4().hex[:8]}"
        stuck = _stuck_pods_deployed < MOCK_STUCK_INIT_PODS
        if stuck:
            _stuck_pods_deployed += 1
            pods[pod_id] = _initializing_pod(
                pod_id=pod_id,
                name=input_payload.get("name", "mock-speakr-whisperx"),
                template_id=input_payload.get("templateId", "mock-template"),
            )
        else:
            pods[pod_id] = _running_pod(
                pod_id=pod_id,
                name=input_payload.get("name", "mock-speakr-whisperx"),
                template_id=input_payload.get("templateId", "mock-template"),
            )
        # Real API returns a slim deploy receipt matching podFindAndDeployOnDemand response shape.
        return JSONResponse({"data": {"podFindAndDeployOnDemand": {
            "id": pod_id,
            "imageName": "tarkilhk/speakr-runpod-whisperx:mock",
            "machineId": "mock-machine",
        }}})

    if "podResume" in query:
        pod_id = input_payload["podId"]
        pod = pods.setdefault(pod_id, _running_pod(pod_id=pod_id))
        pod.update(_runtime_fields("RUNNING"))
        # Real API returns only id, desiredStatus on resume.
        return JSONResponse({"data": {"podResume": {
            "id": pod_id,
            "desiredStatus": "RUNNING",
        }}})

    if "podStop" in query:
        pod_id = input_payload["podId"]
        pod = pods.setdefault(pod_id, _stopped_pod(pod_id=pod_id))
        pod.update(_stopped_fields())
        return JSONResponse({"data": {"podStop": {"id": pod_id, "desiredStatus": "EXITED"}}})

    if "podTerminate" in query:
        pods.pop(input_payload["podId"], None)
        return JSONResponse({"data": {"podTerminate": None}})

    if "pod(input" in query:
        pod_id = input_payload["podId"]
        return JSONResponse({"data": {"pod": pods.get(pod_id)}})

    return JSONResponse(
        status_code=400,
        content={"errors": [{"message": "Unsupported mock GraphQL operation"}]},
    )


@app.get("/health")
async def health(response: Response) -> dict[str, Any]:
    response.headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
    return {"status": "healthy", "upstream": {"status": "mock"}}


@app.get("/internal/pod-logs")
async def internal_pod_logs(authorization: str = Header(default="")) -> dict[str, Any]:
    expected = f"Bearer {ADAPTER_WHISPERX_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"files": [{"name": "whisperx-stdout.log", "content": "mock whisperx log line\n"}]}


@app.post("/asr")
async def asr(request: Request, authorization: str = Header(default="")) -> Response:
    expected = f"Bearer {ADAPTER_WHISPERX_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Drain the multipart body so this catches request streaming issues.
    async for _chunk in request.stream():
        pass

    payload = {
        "text": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": " mock transcription",
                "speaker": "SPEAKER_00",
            }
        ],
        "language": "en",
        "segments": [{"start": 0.0, "end": 1.0, "text": " mock transcription", "speaker": "SPEAKER_00"}],
        "word_segments": [],
    }
    if request.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
        return JSONResponse(payload)
    body = JSONResponse(payload).body
    result_id = uuid.uuid4().hex
    results[result_id] = body
    return JSONResponse(
        {"id": result_id, "length": len(body), "sha256": hashlib.sha256(body).hexdigest()},
        status_code=202, headers={PROTOCOL_HEADER: PROTOCOL_VERSION},
    )


@app.get("/internal/asr-results/{result_id}")
async def result_chunk(result_id: str, offset: int, limit: int, authorization: str = Header(default="")) -> Response:
    if authorization != f"Bearer {ADAPTER_WHISPERX_TOKEN}":
        raise HTTPException(status_code=401)
    body = results.get(result_id)
    if body is None:
        raise HTTPException(status_code=404)
    if offset < 0 or offset >= len(body) or limit < 1 or limit > CHUNK_SIZE:
        raise HTTPException(status_code=416)
    return Response(body[offset:offset + limit], headers={"X-ASR-Chunk-Offset": str(offset)})


@app.delete("/internal/asr-results/{result_id}")
async def delete_result(result_id: str, authorization: str = Header(default="")) -> Response:
    if authorization != f"Bearer {ADAPTER_WHISPERX_TOKEN}":
        raise HTTPException(status_code=401)
    results.pop(result_id, None)
    return Response(status_code=204)


def _initializing_pod(pod_id: str, name: str = "mock-speakr-whisperx", template_id: str = "mock-template") -> dict[str, Any]:
    """Scheduled on a machine but runtime never appears — stuck-init watchdog can fire if fingerprint is flat."""
    return {
        "id": pod_id,
        "name": name,
        "desiredStatus": "RUNNING",
        "imageName": "tarkilhk/speakr-runpod-whisperx:mock",
        "machineId": "mock-machine",
        "machine": {"podHostId": f"{pod_id}-mock-host"},
        "templateId": template_id,
        "runtime": None,
        "version": 0,
        "uptimeSeconds": 0,
    }


def _running_pod(pod_id: str, name: str = "mock-speakr-whisperx", template_id: str = "mock-template") -> dict[str, Any]:
    return {
        "id": pod_id,
        "name": name,
        "desiredStatus": "RUNNING",
        "imageName": "tarkilhk/speakr-runpod-whisperx:mock",
        "machineId": "mock-machine",
        "templateId": template_id,
        **_runtime_fields("RUNNING"),
    }


def _stopped_pod(pod_id: str) -> dict[str, Any]:
    return {
        "id": pod_id,
        "name": "mock-speakr-whisperx",
        "desiredStatus": "EXITED",
        "imageName": "tarkilhk/speakr-runpod-whisperx:mock",
        "machineId": None,
        **_stopped_fields(),
    }


def _runtime_fields(status: str) -> dict[str, Any]:
    return {
        "desiredStatus": status,
        "runtime": {
            "uptimeInSeconds": 1,
            "ports": [
                {
                    "ip": PUBLIC_IP,
                    "isIpPublic": True,
                    "privatePort": WRAPPER_PRIVATE_PORT,
                    "publicPort": PUBLIC_PORT,
                    "type": "tcp",
                }
            ],
        },
    }


def _stopped_fields() -> dict[str, Any]:
    return {"desiredStatus": "EXITED", "runtime": None}
