#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
image="${STUDENT_TTS_SMOKE_IMAGE:-student-tts-local-smoke:py311-cuda128}"
platform="${STUDENT_TTS_SMOKE_PLATFORM:-linux/amd64}"

if [[ "${STUDENT_TTS_SMOKE_SKIP_BUILD:-0}" != "1" ]]; then
  docker build \
    --platform "$platform" \
    --file "$repo/scripts/gpu_tts/student_tts/Dockerfile.smoke" \
    --tag "$image" \
    "$repo"
fi

docker run --rm --platform "$platform" \
  --env STUDENT_TTS_REPO=/workspace/repo \
  --env STUDENT_TTS_FIXTURE_DIR=/tmp/student-tts-smoke \
  --env STUDENT_TTS_SOURCE_ADAPTER=local-fixture \
  --env STUDENT_TTS_PUBLISH_ADAPTER=dry-run \
  "$image" \
  bash -lc '
    python -m pytest -q scripts/gpu_tts/student_tts/test_pipeline.py &&
    python scripts/gpu_tts/student_tts/local_smoke.py
  '
