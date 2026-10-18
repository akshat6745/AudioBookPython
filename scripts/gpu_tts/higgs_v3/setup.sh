#!/usr/bin/env bash
# One-time setup for Higgs Audio v3 on a Vast instance running an sglang-omni:dev image.
# lmsysorg/sglang-omni:dev (Jun 2026) ships sglang-omni preinstalled in /opt/omni;
# newer images ship deps in /opt/sglang and need the package added.
set -euo pipefail

MODEL_ID="bosonai/higgs-tts-3-4b"
MODEL_DIR="/workspace/models/higgs-tts-3-4b"
if [ -x /opt/omni/bin/sgl-omni ]; then PY="/opt/omni/bin/python3"; else PY="/opt/sglang/bin/python3"; fi
log() { printf '\n[setup %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

log "GPU"
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv
cc=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d .)
if [ "$cc" -lt 89 ]; then
  echo "WARNING: compute capability $cc < 8.9 (4090). The image's FlashInfer cache is built for SM89/SM90;"
  echo "         first start will JIT-compile kernels and may be slow or unsupported."
fi

log "shared memory / disk (SGLang wants a large /dev/shm)"
df -h /dev/shm /workspace 2>/dev/null || df -h /dev/shm /
shm_gb=$(df -BG --output=size /dev/shm | tail -1 | tr -dc 0-9)
[ "$shm_gb" -lt 8 ] && echo "WARNING: /dev/shm is ${shm_gb}G; if serve crashes, relaunch with docker option --shm-size=32g"

current_version=$("$PY" -c "import sglang_omni; print(sglang_omni.__version__)" 2>/dev/null || true)
if [ "$current_version" != "0.1.7" ]; then
  log "upgrading sglang-omni ${current_version:-missing} -> 0.1.7 for configurable Higgs concurrency"
  uv pip install --python "$PY" --prerelease=allow --upgrade "sglang-omni==0.1.7"
fi
"$PY" -c "import sglang_omni; print('sglang_omni', sglang_omni.__version__)"
"$(dirname "$PY")/sgl-omni" check-gpu || true

log "downloading $MODEL_ID (~9.3 GB) to $MODEL_DIR"
mkdir -p "$MODEL_DIR"
"$PY" - <<EOF
from huggingface_hub import snapshot_download
snapshot_download("$MODEL_ID", local_dir="$MODEL_DIR", max_workers=8)
EOF
du -sh "$MODEL_DIR"

log "done. Next: bash higgs_v3/serve.sh"
