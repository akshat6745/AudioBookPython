#!/usr/bin/env bash
# Create a Python 3.11 virtualenv and install the inference stack from public PyPI.
# Windows: run this inside Ubuntu on WSL2.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v python3.11 >/dev/null 2>&1; then
  echo "Python 3.11 is required. On macOS: brew install python@3.11" >&2
  echo "On Ubuntu/WSL2: sudo apt-get install python3.11 python3.11-venv" >&2
  exit 1
fi

missing=()
command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
command -v espeak-ng >/dev/null 2>&1 || missing+=(espeak-ng)
if ((${#missing[@]})); then
  echo "Missing: ${missing[*]}" >&2
  echo "macOS: brew install ${missing[*]}" >&2
  echo "Ubuntu/Debian/WSL2: sudo apt-get install ffmpeg libopus0 espeak-ng" >&2
  echo "Windows native is not supported. Use WSL2 Ubuntu and rerun this script." >&2
  exit 1
fi

python3.11 -m venv .venv-r2worker
PIP_CONFIG_FILE=/dev/null .venv-r2worker/bin/python -m pip install --upgrade pip
PIP_CONFIG_FILE=/dev/null .venv-r2worker/bin/python -m pip install -r scripts/gpu_tts/student_tts/distributed/requirements-inference.txt
exec .venv-r2worker/bin/python -m scripts.gpu_tts.student_tts.distributed setup "$@"
