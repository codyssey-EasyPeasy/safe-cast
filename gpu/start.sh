#!/usr/bin/env bash
set -euo pipefail

dir=$(dirname "$(readlink -f "$0")")
export HF_HOME="${MODELS_DIR:-$HOME/models}"

pidfile="$dir/gpu.pid"
log="$HOME/sumeunjip/gpu.log"
mkdir -p "$HOME/sumeunjip"

if [[ -f "$pidfile" ]]; then
  pid=$(cat "$pidfile" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "gpu server already running (pid $pid)"
    exit 0
  fi
fi

setsid nohup "$dir/.venv/bin/python" -m uvicorn server:app \
  --app-dir "$dir" --host 0.0.0.0 --port 8000 --access-log \
  >> "$log" 2>&1 &

echo $! > "$pidfile"
echo "started (pid $(cat "$pidfile")) -> $log"
