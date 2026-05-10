# Speakr RunPod WhisperX

Reusable Docker images for running Speakr transcription through an on-demand
RunPod Secure Pod.

## What This Solves

Speakr can use a self-hosted ASR endpoint via `ASR_BASE_URL`. Local CPU
WhisperX works but is slow for work meetings. RunPod Serverless is awkward for
large audio files because of request size, timeout, and job-envelope behavior.

This project uses a simpler model:

```text
Speakr
  -> local adapter container
    -> deploys RunPod Secure Pod from a template when needed
    -> discovers current TCP mapping
    -> forwards /asr with bearer auth
      -> RunPod auth wrapper
        -> local WhisperX ASR service
```

## Images

This repo builds two images:

| Image | Purpose |
| --- | --- |
| `tarkilhk/speaker-adapter` | Local Speakr-side adapter. |
| `tarkilhk/speakr-runpod-whisperx` | RunPod GPU-side WhisperX image with auth wrapper. |

Both images should be published as:

- `latest`
- `sha-<git-sha>`

Use `latest` for low-maintenance testing. Use a `sha-*` tag for stable
deployments and rollback.

## Publishing Images

This repo uses two separate workflows: one for `adapter/` and one for
`runpod-image/`. The adapter workflow runs when adapter files change. The
RunPod workflow runs when RunPod image files change and also on the weekly
schedule so upstream `latest` can be refreshed.

The GitHub Actions workflows expect this repository secret:

```env
DOCKERHUB_TOKEN=your-dockerhub-access-token
```

The Docker Hub username is configured in each workflow; the token is the only
secret needed for publishing.

## RunPod Image

Directory: `runpod-image/`

The RunPod image is based on:

```dockerfile
FROM learnedmachine/whisperx-asr-service:latest
```

It runs two processes under `supervisord`:

- WhisperX ASR on `127.0.0.1:9001`
- FastAPI auth wrapper on `0.0.0.0:9000`

Only port `9000/tcp` should be exposed from RunPod. The wrapper requires:

```http
Authorization: Bearer <ADAPTER_WHISPERX_TOKEN>
```

Unauthenticated requests receive `401`.

## Adapter Image

Directory: `adapter/`

The adapter exposes Speakr-compatible:

```text
POST /asr
GET /health
```

It:

- accepts Speakr's multipart `/asr` request
- spools the body to temporary disk while the GPU starts
- deploys a RunPod Pod from a configured template if needed
- discovers `publicIp` and `portMappings["9000"]`
- waits for the RunPod wrapper `/health`
- forwards `/asr` with `ADAPTER_WHISPERX_TOKEN`
- returns the WhisperX JSON response to Speakr
- terminates the Pod after an idle timeout

## Required Environment

Adapter:

```env
RUNPOD_API_KEY=your-runpod-api-key
# Optional override for local mocks. Default: https://api.runpod.io/graphql
# RUNPOD_GRAPHQL_URL=http://127.0.0.1:19001/graphql
RUNPOD_TEMPLATE_ID=your-runpod-template-id
RUNPOD_GPU_TYPE_IDS=NVIDIA GeForce RTX 4090,NVIDIA RTX A5000,NVIDIA RTX A6000
RUNPOD_GPU_COUNT=1
RUNPOD_POD_NAME=speakr-whisperx
RUNPOD_IDLE_ACTION=terminate
ADAPTER_WHISPERX_TOKEN=replace-with-shared-secret
RUNPOD_WRAPPER_PORT=9000
RUNPOD_READINESS_TIMEOUT_SECONDS=600
RUNPOD_REQUEST_TIMEOUT_SECONDS=1800
RUNPOD_IDLE_STOP_SECONDS=900
RUNPOD_RETRY_AFTER_SECONDS=300
MAX_FILE_SIZE_MB=0
LOG_LEVEL=INFO
```

`RUNPOD_TEMPLATE_ID` is preferred because stopped Pods remain tied to their
original host. The adapter creates a fresh Pod from the template, then deletes it
after the idle timeout so the next request can land on any available GPU host.
For legacy fixed-Pod behavior, set `RUNPOD_POD_ID` instead and set
`RUNPOD_IDLE_ACTION=stop`.

`MAX_FILE_SIZE_MB=0` means unlimited. Set a value (e.g. `1000`) to reject oversized
uploads before spooling them to disk.

## Local Adapter Testing

You can test the adapter lifecycle without pulling the GPU image. The local mock
implements the subset of RunPod GraphQL used by the adapter, based on the
official GraphQL docs linked in `docs/runpod-graphql-api.md`, plus a fake
WhisperX `/health` + `/asr` endpoint.

Run both services:

```bash
docker compose -f docker-compose.mock.yml up --build
```

In another terminal, run:

```bash
scripts/smoke_mock_adapter.sh
```

This exercises the real adapter flow: `podFindAndDeployOnDemand`, `pod` runtime
port discovery, bearer-auth `/asr` forwarding, and `podTerminate`.

If you prefer to run only the mock directly:

```bash
ADAPTER_WHISPERX_TOKEN=test-token \
MOCK_RUNPOD_PUBLIC_PORT=19001 \
uvicorn scripts.mock_runpod_graphql:app --host 127.0.0.1 --port 19001
```

Point the adapter at the mock:

```env
RUNPOD_API_KEY=test-key
RUNPOD_GRAPHQL_URL=http://127.0.0.1:19001/graphql
RUNPOD_TEMPLATE_ID=mock-template
RUNPOD_GPU_TYPE_IDS=NVIDIA GeForce RTX 4090
ADAPTER_WHISPERX_TOKEN=test-token
RUNPOD_WRAPPER_PORT=9000
RUNPOD_IDLE_ACTION=terminate
```

RunPod image:

```env
HF_TOKEN=your-huggingface-token
ADAPTER_WHISPERX_TOKEN=replace-with-shared-secret
DEVICE=cuda
COMPUTE_TYPE=float16
BATCH_SIZE=16
PRELOAD_MODEL=large-v3
MAX_FILE_SIZE_MB=1000
SERVE_MODE=simple
MODEL_KEEP_ALIVE_SECONDS=0
```

If using a RunPod volume mounted at `/workspace`, also set:

```env
CACHE_DIR=/workspace/cache
HF_HOME=/workspace/cache
```

For cheapest first testing, use 0 GB volume disk and accept slower cold starts.
Add a small volume later if model downloads repeat too often.
See `docs/cost-analysis.md` for a worked breakeven example comparing `0 GB`
volume versus a `50 GB` persistent volume with RunPod pricing assumptions.

## HuggingFace Access

The `HF_TOKEN` account must accept the model terms required by upstream
`learnedmachine/whisperx-asr-service`:

- `pyannote/segmentation-3.0`
- `pyannote/speaker-diarization-3.1`

Without this, model download will fail with a 403.

## Speakr Configuration

After deploying the adapter image beside Speakr:

```env
ASR_BASE_URL=http://whisperx-adapter:9000
ASR_DIARIZE=true
```

Do not enable speaker embeddings until you have tested the contract:

```env
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

Only enable that after confirming the returned `speaker_embeddings` shape
matches what Speakr expects.

## Failure Codes

The adapter returns:

- `500 Internal Server Error` if required environment variables are missing
  (`RUNPOD_API_KEY`, `ADAPTER_WHISPERX_TOKEN`, and either `RUNPOD_TEMPLATE_ID`
  or `RUNPOD_POD_ID`).
- `503 Service Unavailable` plus `Retry-After` for temporary RunPod capacity,
  startup, or preemption failures.
- `504 Gateway Timeout` if readiness or transcription exceeds the configured
  timeout.
- `502 Bad Gateway` if RunPod or WhisperX returns malformed/unexpected data.

## Cost Controls

The adapter releases the RunPod Pod after `RUNPOD_IDLE_STOP_SECONDS` of inactivity.
When `RUNPOD_TEMPLATE_ID` is set, the default idle action is `terminate`; this
avoids RunPod's stopped-Pod host affinity problem by creating a fresh Pod from
the template on the next request. When using legacy `RUNPOD_POD_ID` mode, the
default idle action is `stop`.

This idle-stop is in-memory only: if the adapter container crashes or the Docker
host reboots while the Pod is running, the Pod will continue billing until stopped
manually.

An external watchdog that independently polls and stops the Pod is recommended.
Until that is wired, monitor RunPod billing manually.

## Security

RunPod TCP mapping is internet-facing. Do not expose WhisperX directly.

The RunPod image exposes only the auth wrapper. The wrapper checks
`ADAPTER_WHISPERX_TOKEN` and forwards valid requests to local WhisperX.

## Local Build

```bash
docker build -t speaker-adapter:test adapter
docker build -t speakr-runpod-whisperx:test runpod-image
```

The RunPod image is large because it inherits CUDA/PyTorch/WhisperX. Local
builds may need tens of GB of free Docker storage.

## Decision Log

See [docs/decisions/0001-architecture.md](docs/decisions/0001-architecture.md).

## License

MIT. See [LICENSE](LICENSE).
