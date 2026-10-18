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
WIDTHFILE="$HERE/../logs/higgs_v3.max_concurrency"
mkdir -p "$(dirname "$LOG")"

if [ "${1:-}" = "stop" ]; then
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  pkill -f "sgl-omni serve" 2>/dev/null || true
  rm -f "$PIDFILE"
  sleep 5
  nvidia-smi --query-gpu=memory.used --format=csv,noheader
  exit 0
fi

rm -f "$WIDTHFILE"
for width in 128 64 32 16; do
  echo "starting Higgs v3 at concurrency $width on 127.0.0.1:$PORT (log: $LOG)"
  nohup "$SGL" serve --model-path "$MODEL_DIR" --host 127.0.0.1 --port "$PORT" \
    --max-running-requests "$width" --cuda-graph-max-bs "$width" </dev/null >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"

  start=$(date +%s)
  failed=0
  until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "server exited at concurrency $width; retrying lower"
      tail -40 "$LOG"
      failed=1
      break
    fi
    [ $(( $(date +%s) - start )) -gt 900 ] && {
      echo "not healthy after 15 min at concurrency $width"
      tail -40 "$LOG"
      exit 1
    }
    sleep 5
  done
  if [ "$failed" -eq 0 ]; then
    echo "$width" >"$WIDTHFILE"
    echo "healthy at concurrency $width after $(( $(date +%s) - start ))s"
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
    exit 0
  fi
done
echo "Higgs failed to start at every concurrency" >&2
exit 1
