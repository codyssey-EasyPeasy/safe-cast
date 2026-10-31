#!/usr/bin/env bash
# 운영 서버(hk-ops) 기동: 127.0.0.1:8080 에만 바인드(외부 노출 없음, ssh 터널로만 접근).
# 접근 로그는 app 의 미들웨어가 메서드·경로·상태코드만 남긴다(uvicorn 접근 로그 끔).
set -euo pipefail
dir=$(dirname "$(readlink -f "$0")")/..
dir=$(readlink -f "$dir")
cd "$dir"
pidfile="$dir/app.pid"
log="$dir/app.log"
export GPU_URL="${GPU_URL:-http://10.1.2.33:8000}"
host="${APP_HOST:-127.0.0.1}"
port="${APP_PORT:-8080}"

if [[ -f "$pidfile" ]]; then
  pid=$(cat "$pidfile" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "app already running (pid $pid)"
    exit 0
  fi
fi

setsid nohup "$dir/.venv/bin/python" -m uvicorn app.main:app \
  --host "$host" --port "$port" --no-access-log --log-level warning \
  >> "$log" 2>&1 < /dev/null &
echo $! > "$pidfile"
echo "started (pid $(cat "$pidfile")) on $host:$port, GPU_URL=$GPU_URL -> $log"
