# AGENTS.md

Agent operating guide for `speakr-runpod-whisperx`.

## Scope

This file applies to the whole repository.

## Project Purpose

This repo contains reusable Docker images that let Speakr use an on-demand
RunPod Secure Pod for WhisperX transcription:

- `runpod-image/`: GPU-side image based on
  `learnedmachine/whisperx-asr-service:latest`, with a FastAPI auth wrapper in
  front of the public TCP port.
- `adapter/`: local Speakr-side adapter that accepts Speakr's `/asr` requests,
  starts/stops the RunPod Pod, discovers the current TCP mapping, and forwards
  requests to the authenticated wrapper.

The repo must stay generic and open-sourceable. Do not add homelab-specific
hostnames, private URLs, inventory paths, tokens, secrets, or deployment
assumptions.

## Secrets

Never commit real values for:

- `RUNPOD_API_KEY`
- `ADAPTER_WHISPERX_TOKEN`
- `HF_TOKEN`
- any Speakr, Docker Hub, or homelab secrets

Use examples with obvious placeholders.

## Docker Images

Expected published images:

- `tarkilhk/speakr-runpod-whisperx:latest`
- `tarkilhk/speaker-adapter:latest`

Also publish immutable SHA tags from CI for rollback.

The RunPod image intentionally uses:

```dockerfile
FROM learnedmachine/whisperx-asr-service:latest
```

This follows the project decision to prefer low-maintenance upstream updates.
Document any breakage and recommend using immutable SHA tags for stable
deployments.

## Validation

Run before finalizing code changes:

```bash
python3 -m py_compile adapter/app.py runpod-image/wrapper.py
docker build -t speaker-adapter:test adapter
docker build -t speakr-runpod-whisperx:test runpod-image
```

If Docker image builds are too large for the local machine, at minimum run the
Python compile check and rely on CI for image builds.

