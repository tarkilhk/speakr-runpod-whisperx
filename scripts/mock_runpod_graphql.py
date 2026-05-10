import os
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

PUBLIC_IP = os.getenv("MOCK_RUNPOD_PUBLIC_IP", "127.0.0.1")
PUBLIC_PORT = int(os.getenv("MOCK_RUNPOD_PUBLIC_PORT", "19001"))
WRAPPER_PRIVATE_PORT = int(os.getenv("MOCK_RUNPOD_WRAPPER_PORT", "9000"))
ADAPTER_WHISPERX_TOKEN = os.getenv("ADAPTER_WHISPERX_TOKEN", "test-token")

app = FastAPI(title="Mock RunPod GraphQL + WhisperX")
pods: dict[str, dict[str, Any]] = {}


@app.post("/graphql")
async def graphql(request: Request) -> JSONResponse:
    payload = await request.json()
    query = payload.get("query", "")
    variables = payload.get("variables", {})
    input_payload = variables.get("input", {})

    if "podFindAndDeployOnDemand" in query:
        pod_id = f"mock-{uuid.uuid4().hex[:8]}"
        pods[pod_id] = _running_pod(
            pod_id=pod_id,
            name=input_payload.get("name", "mock-speakr-whisperx"),
            template_id=input_payload.get("templateId", "mock-template"),
        )
        # Real API returns a slim deploy receipt, not the full pod object.
        return JSONResponse({"data": {"podFindAndDeployOnDemand": {
            "id": pod_id,
            "imageName": "tarkilhk/speakr-runpod-whisperx:mock",
            "env": [],
            "machineId": "mock-machine",
            "machine": {"podHostId": "mock-host"},
        }}})

    if "podResume" in query:
        pod_id = input_payload["podId"]
        pod = pods.setdefault(pod_id, _running_pod(pod_id=pod_id))
        pod.update(_runtime_fields("RUNNING"))
        # Real API returns only id, desiredStatus, imageName on resume.
        return JSONResponse({"data": {"podResume": {
            "id": pod_id,
            "desiredStatus": "RUNNING",
            "imageName": pod["imageName"],
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
async def health() -> dict[str, Any]:
    return {"status": "healthy", "upstream": {"status": "mock"}}


@app.post("/asr")
async def asr(request: Request, authorization: str = Header(default="")) -> dict[str, Any]:
    expected = f"Bearer {ADAPTER_WHISPERX_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Drain the multipart body so this catches request streaming issues.
    async for _chunk in request.stream():
        pass

    return {
        "text": [{"start": 0.0, "end": 1.0, "text": " mock transcription", "speaker": "SPEAKER_00"}],
        "language": "en",
        "segments": [{"start": 0.0, "end": 1.0, "text": " mock transcription", "speaker": "SPEAKER_00"}],
        "word_segments": [],
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
