#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:19000}"
TMP_AUDIO="$(mktemp --suffix=.wav)"
trap 'rm -f "$TMP_AUDIO"' EXIT

# The mock drains the body but does not decode audio, so a tiny placeholder is enough.
printf 'mock audio bytes\n' > "$TMP_AUDIO"

echo "Checking adapter health..."
curl --fail --silent "$BASE_URL/health"
echo

echo "Posting mock ASR request..."
curl --fail --silent \
  -F "audio_file=@${TMP_AUDIO};type=audio/wav" \
  "$BASE_URL/asr?diarize=true&output=json"
echo

echo "Mock adapter smoke test passed."
