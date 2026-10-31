#!/usr/bin/env bash
# PID 파일 + /proc/<pid>/cmdline 확인 뒤 종료(pkill 쓰지 않음).
set -euo pipefail
dir=$(readlink -f "$(dirname "$(readlink -f "$0")")/..")
pidfile="$dir/app.pid"
if [[ ! -f "$pidfile" ]]; then
  echo "no pid file"; exit 0
fi
pid=$(cat "$pidfile" 2>/dev/null || true)
if [[ -z "${pid:-}" ]] || [[ ! -r "/proc/$pid/cmdline" ]]; then
  echo "process not alive"; rm -f "$pidfile"; exit 0
fi
if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'uvicorn app.main:app'; then
  kill -TERM "$pid"
  echo "stopped (pid $pid)"
else
  echo "pid $pid is not the app, refusing"; exit 1
fi
rm -f "$pidfile"
