# Local Development

## Testing with the Mock

The mock server (`scripts/mock_runpod_graphql.py`) implements the RunPod
GraphQL operations used by the adapter and also serves a fake WhisperX
`/health` and `/asr` endpoint. This lets you exercise the full adapter
lifecycle without a real RunPod account.

**Install dependencies:**

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn[standard] httpx python-multipart
```

**Start the mock** (acts as both RunPod GraphQL API and the WhisperX wrapper):

```bash
ADAPTER_WHISPERX_TOKEN=test-token \
MOCK_RUNPOD_PUBLIC_IP=127.0.0.1 \
MOCK_RUNPOD_PUBLIC_PORT=19001 \
.venv/bin/uvicorn scripts.mock_runpod_graphql:app --port 19001
```

**Start the adapter** pointed at the mock:

```bash
RUNPOD_GRAPHQL_URL=http://127.0.0.1:19001/graphql \
RUNPOD_API_KEY=test-key \
RUNPOD_TEMPLATE_ID=mock-template \
RUNPOD_GPU_TYPE_IDS="NVIDIA GeForce RTX 4090" \
ADAPTER_WHISPERX_TOKEN=test-token \
RUNPOD_IDLE_STOP_SECONDS=0 \
PYTHONPATH=adapter \
.venv/bin/uvicorn app:app --app-dir adapter --port 19000
```

**Run the smoke test:**

```bash
bash scripts/smoke_mock_adapter.sh
```

This exercises: `podFindAndDeployOnDemand`, `pod` runtime port discovery,
bearer-auth `/asr` forwarding, and `podTerminate`.

## Building Images Locally

```bash
docker build -t speakr-adapter:test adapter/
docker build -t speakr-runpod-whisperx:test runpod-image/
```

The RunPod image is large — it inherits CUDA, PyTorch, and WhisperX. Allow
tens of GB of free Docker storage and expect a long first build.

## CI

Two GitHub Actions workflows publish images to Docker Hub:

- `.github/workflows/build-adapter-image.yaml` — triggers on changes to `adapter/`
- `.github/workflows/build-runpod-image.yaml` — triggers on changes to `runpod-image/` and weekly (to pick up upstream `latest`)

Required repository secret: `DOCKERHUB_TOKEN`
