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

# STUDENT_TTS_PRETRAINED_HOST=<local epochs_2nd_00020.pth> skips the 771 MB download in the CPU training smoke.
# STUDENT_TTS_HF_CACHE_HOST=<dir> keeps Hugging Face downloads (LibriTTS checkpoint, WavLM) between runs.
mount=()
if [[ -n "${STUDENT_TTS_PRETRAINED_HOST:-}" ]]; then
  mount+=(--volume "$STUDENT_TTS_PRETRAINED_HOST:/tmp/pretrained.pth:ro" --env STUDENT_TTS_PRETRAINED=/tmp/pretrained.pth)
fi
if [[ -n "${STUDENT_TTS_HF_CACHE_HOST:-}" ]]; then
  mkdir -p "$STUDENT_TTS_HF_CACHE_HOST"
  mount+=(--volume "$STUDENT_TTS_HF_CACHE_HOST:/hf" --env HF_HOME=/hf)
fi

docker run --rm --platform "$platform" \
  --env STUDENT_TTS_REPO=/workspace/repo \
  --env STUDENT_TTS_FIXTURE_DIR=/tmp/student-tts-smoke \
  --env STUDENT_TTS_SOURCE_ADAPTER=local-fixture \
  --env STUDENT_TTS_PUBLISH_ADAPTER=dry-run \
  "${mount[@]}" \
  "$image" \
  bash -lc '
    python -m pytest -q scripts/gpu_tts/student_tts/test_pipeline.py &&
    python scripts/gpu_tts/student_tts/local_smoke.py &&
    python scripts/gpu_tts/student_tts/cpu_train_smoke.py
  '
