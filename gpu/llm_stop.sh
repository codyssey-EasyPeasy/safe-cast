#!/usr/bin/env bash
# 내부 LLM 종료. PID 파일의 프로세스가 vllm serve 인지 cmdline 으로 확인한 뒤에만 종료한다.
set -euo pipefail

dir=$(dirname "$(readlink -f "$0")")
pidfile="$dir/llm.pid"

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

if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'vllm serve'; then
  kill -TERM "$pid"
  for _ in $(seq 1 30); do
    [[ -r "/proc/$pid/cmdline" ]] || break
    sleep 1
  done
  if [[ -r "/proc/$pid/cmdline" ]]; then
    echo "pid $pid still alive after 30s (left running; check manually)"
    exit 1
  fi
  echo "stopped (pid $pid)"
else
  echo "pid $pid is not vllm serve, refusing"
fi
rm -f "$pidfile"
