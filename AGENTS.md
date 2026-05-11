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
- `speakr_common/`: tiny shared Python helpers used by the adapter and RunPod
  wrapper images (e.g. logging hygiene).

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
- `tarkilhk/speakr-adapter:latest`

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
python3 -m py_compile adapter/app.py adapter/adapter/pod_logs.py adapter/adapter/cli_drain.py \
  runpod-image/tee_process.py runpod-image/wrapper.py \
  speakr_common/http_client_logging.py speakr_common/uvicorn_access.py
docker compose -f docker-compose.mock.yml up -d --build
bash scripts/smoke_mock_adapter.sh
docker compose -f docker-compose.mock.yml down
```

The smoke test exercises the full adapter lifecycle against the mock: deploy,
TCP mapping discovery, bearer-auth `/asr` forwarding, and terminate. Run it
once when a feature or fix is complete — not after every small change. CI does
not run the smoke test; it is a manual pre-commit gate.

If Docker image builds are too large for the local machine, at minimum run the
Python compile check and rely on CI for image builds.
