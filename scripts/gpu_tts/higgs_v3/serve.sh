#!/usr/bin/env bash
# Start / stop the Higgs v3 server. Binds to 127.0.0.1 only: reach it via SSH tunnel.
#   bash higgs_v3/serve.sh          start in background, wait until healthy
#   bash higgs_v3/serve.sh stop     stop and free the GPU
set -euo pipefail

MODEL_DIR="/workspace/models/higgs-tts-3-4b"
PORT="${PORT:-8000}"
SGL="$(command -v sgl-omni || echo /opt/omni/bin/sgl-omni)"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HERE/../logs/higgs_v3.log"
PIDFILE="$HERE/../logs/higgs_v3.pid"
mkdir -p "$(dirname "$LOG")"

if [ "${1:-}" = "stop" ]; then
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  pkill -f "sgl-omni serve" 2>/dev/null || true
  rm -f "$PIDFILE"
  sleep 5
  nvidia-smi --query-gpu=memory.used --format=csv,noheader
  exit 0
fi

echo "starting Higgs v3 on 127.0.0.1:$PORT (log: $LOG)"
# This is the configuration used by the published Higgs H100 benchmark.
# Keeping admission and CUDA-graph capture aligned avoids queued requests
# silently falling outside the captured batch-size range.
nohup "$SGL" serve --model-path "$MODEL_DIR" --host 127.0.0.1 --port "$PORT" \
  --max-running-requests 16 --cuda-graph-max-bs 16 </dev/null >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

start=$(date +%s)
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "server exited during startup; last log lines:"; tail -40 "$LOG"; exit 1
  fi
  [ $(( $(date +%s) - start )) -gt 900 ] && { echo "not healthy after 15 min"; tail -40 "$LOG"; exit 1; }
  sleep 5
done
echo "healthy after $(( $(date +%s) - start ))s"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
