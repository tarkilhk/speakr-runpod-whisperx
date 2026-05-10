#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:19000}"
TMP_AUDIO="$(mktemp --suffix=.wav)"
trap 'rm -f "$TMP_AUDIO"' EXIT

# The mock drains the body but does not decode audio, so a tiny placeholder is enough.
printf 'mock audio bytes\n' > "$TMP_AUDIO"

echo "Checking adapter health..."
health_body=""
for ((attempt = 1; attempt <= 45; attempt++)); do
  if health_body=$(curl --fail --silent --max-time 3 "$BASE_URL/health" 2>/dev/null); then
    echo "$health_body"
    echo
    break
  fi
  if [[ "$attempt" -eq 45 ]]; then
    echo "Adapter health check failed after ${attempt} attempts (${BASE_URL}/health)." >&2
    exit 1
  fi
  sleep 1
done

echo "Posting mock ASR request..."
curl --fail --silent \
  -F "audio_file=@${TMP_AUDIO};type=audio/wav" \
  "$BASE_URL/asr?diarize=true&output=json"
echo

echo "Mock adapter smoke test passed."
