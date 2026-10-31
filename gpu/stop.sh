#!/usr/bin/env bash
set -euo pipefail

dir=$(dirname "$(readlink -f "$0")")
pidfile="$dir/gpu.pid"

if [[ ! -f "$pidfile" ]]; then
  echo "no pid file"
  exit 0
fi

pid=$(cat "$pidfile" 2>/dev/null || true)
if [[ -z "${pid:-}" ]] || [[ ! -r "/proc/$pid/cmdline" ]]; then
  echo "process not alive"
  rm -f "$pidfile"
  exit 0
fi

if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q uvicorn; then
  kill -TERM "$pid"
  echo "stopped (pid $pid)"
else
  echo "pid $pid is not uvicorn, refusing"
fi
rm -f "$pidfile"
