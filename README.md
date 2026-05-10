# Speakr RunPod WhisperX

On-demand GPU transcription for Speakr via RunPod Secure Pods. Starts a GPU
when a transcription request arrives, forwards it to WhisperX, and terminates
the Pod after an idle timeout.

## How It Works

```text
Speakr
  └─> adapter container  (local, always-on)
        ├─> RunPod GraphQL API  (deploy / inspect / terminate)
        └─> RunPod Secure Pod   (started on demand)
              └─> auth wrapper  :9000  (public TCP)
                    └─> WhisperX ASR  :9001  (local)
```

## Images

| Image | Runs on |
|---|---|
| `tarkilhk/speakr-adapter` | Your Docker host, beside Speakr |
| `tarkilhk/speakr-runpod-whisperx` | RunPod GPU Pod |

Both are published as `latest` and immutable `sha-<git-sha>` tags. Use `sha-*`
for stable deployments.

## Quick Start

**1. Create a RunPod template** using `tarkilhk/speakr-runpod-whisperx:latest`,
port `9000/tcp`, and set two RunPod secrets:

```
RUNPOD_SECRET_HF_TOKEN          your HuggingFace token
RUNPOD_SECRET_ADAPTER_WHISPERX_TOKEN  a shared secret you generate
```

See [docs/setup.md](docs/setup.md) for full template configuration.

**2. Run the adapter** beside your Speakr container:

```env
RUNPOD_API_KEY=your-runpod-api-key
RUNPOD_TEMPLATE_ID=your-template-id
RUNPOD_GPU_TYPE_IDS=NVIDIA GeForce RTX 4090,NVIDIA GeForce RTX 3090,NVIDIA RTX A5000
RUNPOD_CLOUD_TYPE=SECURE
RUNPOD_CONTAINER_DISK_GB=50
ADAPTER_WHISPERX_TOKEN=same-shared-secret-as-above
```

See [adapter/.env.example](adapter/.env.example) for all options.

**3. Point Speakr at the adapter:**

```env
ASR_BASE_URL=http://whisperx-adapter:9000
ASR_DIARIZE=true
```

## Response Codes

| Code | Meaning |
|------|---------|
| `500` | Missing required env vars (`RUNPOD_API_KEY`, `ADAPTER_WHISPERX_TOKEN`, `RUNPOD_TEMPLATE_ID`) |
| `503 + Retry-After` | RunPod capacity unavailable or Pod failed to become healthy in time |
| `504` | Readiness or transcription timeout |
| `502` | Unexpected response from RunPod or WhisperX |
| `413` | Upload exceeds `MAX_FILE_SIZE_MB` |

On a cold start the adapter holds the connection open while the Pod boots
(up to `RUNPOD_READINESS_TIMEOUT_SECONDS`, default 600 s). **Speakr's HTTP
client timeout must be set longer than this**, otherwise cold-start requests
will time out on the Speakr side before the adapter finishes. A `503` is only
returned when the deploy itself fails (e.g. no GPU capacity), not during normal
startup.

## Docs

- [docs/setup.md](docs/setup.md) — full configuration reference, HuggingFace setup, volume tradeoff
- [docs/local-development.md](docs/local-development.md) — mock testing and local builds
- [docs/cost-analysis.md](docs/cost-analysis.md) — volume vs cold-start breakeven
- [docs/decisions/0001-architecture.md](docs/decisions/0001-architecture.md) — architecture decision record
- [docs/runpod-graphql-api.md](docs/runpod-graphql-api.md) — RunPod GraphQL API notes

## License

MIT. See [LICENSE](LICENSE).
