# Decision 0001: RunPod Secure Pod With Local Adapter

Date: 2026-05-10

## Context

The goal is to replace Fireflies-style transcription for work meetings while
keeping Speakr as the meeting archive and UI.

Requirements:

- high transcription quality
- speaker diarization for more than four speakers
- eventual speaker identification/voice profile support
- support for normal meeting audio files above 30 MB
- low ongoing cost by running GPU only on demand
- no unauthenticated public GPU endpoint
- no homelab/private information in reusable Docker image builds

## Options Considered

### OpenAI `gpt-4o-transcribe-diarize`

Rejected for this use case because the diarization product limit does not fit
meetings with more than four speakers.

### Hosted Transcription APIs

AssemblyAI, Gladia, and Deepgram were considered. They are simpler than running
GPU infrastructure and handle larger files, but they do not match Speakr's
WhisperX voice-profile path as closely.

### RunPod Serverless

Rejected as the primary architecture.

Reasons:

- Serverless request payload limits are a poor fit for large meeting audio.
- Queue job format does not match Speakr's simple `/asr` HTTP contract.
- Load-balanced HTTP paths can hit long-request timeout limits.
- A correct design would require object storage, polling, and custom handler
  logic.

### RunPod Secure Pod

Accepted.

Reasons:

- behaves like a normal GPU container/VM service
- supports large multipart uploads over TCP mapping
- can be started and stopped on demand through RunPod API
- avoids Serverless job-envelope complexity
- works with the upstream Speakr-compatible WhisperX ASR service

## Chosen Architecture

```text
Speakr
  -> local adapter image
    -> RunPod REST API start/inspect/stop
    -> public TCP mapping with bearer token
      -> RunPod auth wrapper
        -> local WhisperX service
```

The RunPod image contains both the auth wrapper and WhisperX in one container
because RunPod Pods do not support Docker Compose.

## Why The Auth Wrapper Exists

RunPod TCP mapping is public. If WhisperX were exposed directly, anyone who
found the IP and port could consume the GPU.

The wrapper:

- listens on public port `9000`
- checks `Authorization: Bearer <ADAPTER_WHISPERX_TOKEN>`
- forwards valid requests to `127.0.0.1:9001`
- returns `401` for invalid requests

Tailscale/WireGuard was considered, but it adds container networking complexity.
The auth wrapper is simpler and reliable for v1.

## Why The Local Adapter Exists

Speakr only knows how to call an ASR base URL. It should not know RunPod API
tokens, Pod IDs, or changing TCP ports.

The adapter:

- provides a stable local `ASR_BASE_URL`
- starts the Pod when needed
- discovers the current TCP mapping
- waits for readiness
- forwards the `/asr` request
- stops the Pod after idle timeout

## Storage Decision

Start with 0 GB RunPod volume disk for cheapest testing.

This means model cache may be wiped when the Pod stops. If cold starts are too
slow or model downloads repeat too often, add a small `/workspace` volume and
set:

```env
CACHE_DIR=/workspace/cache
HF_HOME=/workspace/cache
```

## Update Strategy

The RunPod image intentionally uses upstream `latest`:

```dockerfile
FROM learnedmachine/whisperx-asr-service:latest
```

This matches the preference for low-maintenance auto-updates. To reduce risk,
CI publishes immutable `sha-*` tags. Production/stable deployments can use a
specific `sha-*` tag while testing newer `latest` builds separately.

## Watchdog Decision

The adapter stops the Pod after idle timeout, but an external watchdog is still
valuable in case the adapter crashes after starting the GPU.

Raw cron on the Docker VM was deferred. A draft script is kept in the consuming
homelab repo until a scheduler approach is chosen, such as:

- Home Assistant automation
- containerized scheduler
- managed service/timer
- Ansible-managed timer, if later accepted

## Consequences

Positive:

- large audio files are not blocked by Serverless payload limits
- GPU cost is controlled by start/stop lifecycle
- public TCP endpoint is protected
- Speakr cutover and rollback are one env edit
- reusable images can be open sourced independently

Tradeoffs:

- one more repo and two images to maintain
- first transcription after Pod stop includes startup/model warm-up latency
- without an external watchdog, failed stop calls can leave GPU running
- upstream `latest` can introduce breaking changes unless stable deployments use
  immutable tags

