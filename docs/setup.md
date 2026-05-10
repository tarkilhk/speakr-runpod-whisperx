# Setup Guide

## Prerequisites

- RunPod account with billing configured
- HuggingFace account with access granted to:
  - `pyannote/speaker-diarization-community-1` — visit the model page and accept the user conditions

Without the HuggingFace model access, the container will fail with a 403 on
first start and transcription will succeed but **without speaker diarization**
(all speech returned without speaker labels).

## Step 1 — RunPod Template

Create a template at console.runpod.io with:

| Field | Value |
|-------|-------|
| Image | `tarkilhk/speakr-runpod-whisperx:latest` |
| Expose TCP ports | `9000` |
| Container disk | `50 GB` |
| Volume disk | `0 GB` (see [Volume vs Cold-Start](#volume-vs-cold-start)) |

Set these two **RunPod Secrets** (not plain env vars — they are injected as
`{{ RUNPOD_SECRET_* }}` at runtime):

```
RUNPOD_SECRET_HF_TOKEN                  your HuggingFace token
RUNPOD_SECRET_ADAPTER_WHISPERX_TOKEN    a shared secret (generate with: openssl rand -hex 48)
```

The template's plain env vars are pre-configured in the image. You only need
to override them if you want non-default behaviour — see
[RunPod Image Configuration](#runpod-image-configuration) below.

## Step 2 — Adapter Configuration

Copy `adapter/.env.example` and fill in the required values.

### Required

| Variable | Description |
|----------|-------------|
| `RUNPOD_API_KEY` | RunPod API key from console.runpod.io/user/settings |
| `RUNPOD_TEMPLATE_ID` | ID of the template created in Step 1 |
| `RUNPOD_GPU_TYPE_IDS` | Comma-separated GPU type IDs in priority order |
| `RUNPOD_CLOUD_TYPE` | Must be `SECURE` — community cloud has unreliable capacity |
| `RUNPOD_CONTAINER_DISK_GB` | Must match the template's container disk setting (e.g. `50`) |
| `ADAPTER_WHISPERX_TOKEN` | Same value as `RUNPOD_SECRET_ADAPTER_WHISPERX_TOKEN` |

### GPU Priority

Recommended order for WhisperX (fastest first, broadest availability fallback):

```
RUNPOD_GPU_TYPE_IDS=NVIDIA GeForce RTX 4090,NVIDIA GeForce RTX 3090,NVIDIA RTX A5000
```

The RTX 4090 is prioritised because it is faster per job despite the higher
hourly rate. The RTX 3090 is the best high-availability fallback (typically
"High" stock on Secure cloud). Use the GPU types query to check live
availability — see [docs/runpod-graphql-api.md](runpod-graphql-api.md).

### Important: `RUNPOD_CONTAINER_DISK_GB`

RunPod does **not** inherit `containerDiskInGb` from the template when deploying
via the API. If you omit this variable (or leave it at `0`), the deploy call
will fail with "This machine does not have the resources" regardless of
availability. Set it to the same value as the template's container disk.

### All Options

| Variable | Default | Description |
|----------|---------|-------------|
| `RUNPOD_GRAPHQL_URL` | `https://api.runpod.io/graphql` | Override for local mock testing |
| `RUNPOD_TEMPLATE_ID` | — | Deploy-from-template mode (preferred) |
| `RUNPOD_POD_ID` | — | Fixed-pod mode (legacy; resume/stop a specific pod) |
| `RUNPOD_GPU_TYPE_IDS` | — | Comma-separated GPU IDs |
| `RUNPOD_GPU_COUNT` | `1` | |
| `RUNPOD_CLOUD_TYPE` | `SECURE` | `SECURE` or `COMMUNITY` |
| `RUNPOD_CONTAINER_DISK_GB` | `0` (omit) | Must match template; `0` = omit from API call |
| `RUNPOD_NETWORK_VOLUME_ID` | — | Attach an existing persistent volume |
| `RUNPOD_POD_NAME` | `speakr-whisperx` | Display name in RunPod console |
| `RUNPOD_IDLE_ACTION` | `terminate` (template mode) / `stop` (pod mode) | What to do after idle timeout |
| `RUNPOD_IDLE_STOP_SECONDS` | `900` | Idle timeout before releasing the pod |
| `RUNPOD_RETRY_AFTER_SECONDS` | `300` | `Retry-After` header value sent on 503 |
| `RUNPOD_WRAPPER_PORT` | `9000` | Port the auth wrapper listens on inside the pod |
| `RUNPOD_READINESS_TIMEOUT_SECONDS` | `600` | Max wait for pod to become healthy |
| `RUNPOD_STUCK_INIT_TIMEOUT_SECONDS` | `120` | If a machine is assigned but the container hasn't started within this many seconds, terminate and redeploy. Set to `0` to disable. |
| `RUNPOD_REQUEST_TIMEOUT_SECONDS` | `1800` | Max time for a single transcription request |
| `ADAPTER_WHISPERX_TOKEN` | — | Bearer token; must match the RunPod secret |
| `MAX_FILE_SIZE_MB` | `0` (unlimited) | Reject uploads larger than this before spooling |
| `LOG_LEVEL` | `INFO` | |

### Template Mode vs Fixed-Pod Mode

**Template mode** (`RUNPOD_TEMPLATE_ID`) is preferred. The adapter deploys a
fresh pod for each active period and terminates it when idle. This avoids
RunPod's stopped-pod host-affinity problem where a resumed pod must return to
its original machine, which may no longer be available.

**Fixed-pod mode** (`RUNPOD_POD_ID`) resumes and stops a specific pre-existing
pod. Cheaper if the pod is reused frequently; slower to start if the original
host is busy. Default idle action is `stop` instead of `terminate`.

## Step 3 — Speakr Configuration

```env
ASR_BASE_URL=http://whisperx-adapter:9000
ASR_DIARIZE=true
```

Do not enable speaker embeddings until you have verified the response shape
matches what Speakr expects:

```env
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

## RunPod Image Configuration

The image ships with sensible defaults. These only need to change if you want
different model behaviour:

| Variable | Default | Description |
|----------|---------|-------------|
| `HF_TOKEN` | (from secret) | HuggingFace token for model download |
| `ADAPTER_WHISPERX_TOKEN` | (from secret) | Must match the adapter token |
| `DEVICE` | `cuda` | |
| `COMPUTE_TYPE` | `float16` | |
| `BATCH_SIZE` | `16` | Reduce if OOM on smaller GPUs |
| `PRELOAD_MODEL` | `large-v3` | WhisperX model to load at startup |
| `MAX_FILE_SIZE_MB` | `1000` | |
| `SERVE_MODE` | `simple` | |
| `MODEL_KEEP_ALIVE_SECONDS` | `0` | |
| `CACHE_DIR` | — | Set to `/workspace/cache` if using a volume |
| `HF_HOME` | — | Set to `/workspace/cache` if using a volume |

## Volume vs Cold-Start

By default the template uses `0 GB` volume. Each pod start re-downloads the
WhisperX models (~10 min extra cold-start on first boot after image pull, faster
on subsequent starts if the image layer is cached on the host).

See [cost-analysis.md](cost-analysis.md) for a breakeven calculation. With
default assumptions, a persistent `50 GB` volume only pays off above ~87 pod
starts per month.

If you add a volume, set in the RunPod template:

```env
CACHE_DIR=/workspace/cache
HF_HOME=/workspace/cache
```

## Idle Cost Control

The adapter terminates (or stops) the pod after `RUNPOD_IDLE_STOP_SECONDS` of
inactivity. This is in-process only: if the adapter container crashes while a
pod is running, that pod will continue billing until stopped manually.

An external watchdog that independently checks for orphaned pods is recommended.
Until one is wired, monitor RunPod billing manually.
