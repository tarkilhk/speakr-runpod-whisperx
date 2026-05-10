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
    -> starts RunPod Secure Pod when needed
    -> discovers current TCP mapping
    -> forwards /asr with bearer auth
      -> RunPod auth wrapper
        -> local WhisperX ASR service
```

## Images

This repo builds two images:

| Image | Purpose |
| --- | --- |
| `tarkilhk/speakr-whisperx-adapter` | Local Speakr-side adapter. |
| `tarkilhk/speakr-whisperx-runpod` | RunPod GPU-side WhisperX image with auth wrapper. |

Both images should be published as:

- `latest`
- `sha-<git-sha>`

Use `latest` for low-maintenance testing. Use a `sha-*` tag for stable
deployments and rollback.

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
Authorization: Bearer <RUNPOD_WRAPPER_TOKEN>
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
- starts the configured RunPod Pod if needed
- discovers `publicIp` and `portMappings["9000"]`
- waits for the RunPod wrapper `/health`
- forwards `/asr` with `RUNPOD_WRAPPER_TOKEN`
- returns the WhisperX JSON response to Speakr
- stops the Pod after an idle timeout

## Required Environment

Adapter:

```env
RUNPOD_API_KEY=...
RUNPOD_POD_ID=...
RUNPOD_WRAPPER_TOKEN=...
RUNPOD_WRAPPER_PORT=9000
RUNPOD_READINESS_TIMEOUT_SECONDS=600
RUNPOD_REQUEST_TIMEOUT_SECONDS=1800
RUNPOD_IDLE_STOP_SECONDS=900
RUNPOD_RETRY_AFTER_SECONDS=300
LOG_LEVEL=INFO
```

RunPod image:

```env
HF_TOKEN=...
RUNPOD_WRAPPER_TOKEN=...
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

## HuggingFace Access

The `HF_TOKEN` account must accept the model terms required by upstream
`learnedmachine/whisperx-asr-service`:

- `pyannote/speaker-diarization-community-1`
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

- `503 Service Unavailable` plus `Retry-After` for temporary RunPod capacity,
  startup, or preemption failures.
- `504 Gateway Timeout` if readiness or transcription exceeds the configured
  timeout.
- `502 Bad Gateway` if RunPod or WhisperX returns malformed/unexpected data.

## Security

RunPod TCP mapping is internet-facing. Do not expose WhisperX directly.

The RunPod image exposes only the auth wrapper. The wrapper checks
`RUNPOD_WRAPPER_TOKEN` and forwards valid requests to local WhisperX.

## Local Build

```bash
docker build -t speakr-whisperx-adapter:test adapter
docker build -t speakr-whisperx-runpod:test runpod-image
```

The RunPod image is large because it inherits CUDA/PyTorch/WhisperX. Local
builds may need tens of GB of free Docker storage.

## Decision Log

See [docs/decisions/0001-architecture.md](docs/decisions/0001-architecture.md).

## License

MIT. See [LICENSE](LICENSE).
