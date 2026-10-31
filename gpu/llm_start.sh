#!/usr/bin/env bash
# 내부 LLM(vLLM OpenAI 호환 서버) 시작. SPEC §8.
# - 127.0.0.1:8001 만 바인드(외부 바인드 금지). gpu/server.py 가 프록시한다.
# - 요청·생성문 로그 끔(vLLM 0.32 는 기본 꺼짐이며 --no-enable-log-requests·--no-enable-log-outputs 로 명시),
#   uvicorn 접근 로그도 끔. 런타임 외부 호출 금지: HF_HUB_OFFLINE·통계 수집 끔.
# - GPU_MEM_UTIL: L4 23GB 중 다른 모델(bge-m3·reranker·GLiNER)이 ~6.5GB 를 쓰므로 0.6(≈13.8GB) → 합계 ~20.3GB.
set -euo pipefail

dir=$(dirname "$(readlink -f "$0")")
pidfile="$dir/llm.pid"
log="$HOME/sumeunjip/llm.log"
venv="${VLLM_VENV:-$HOME/vllm-venv}"
model="${LLM_MODEL_PATH:-$HOME/models/Qwen3-8B-AWQ}"
name="${LLM_SERVED_NAME:-qwen3-8b}"
util="${GPU_MEM_UTIL:-0.6}"
mkdir -p "$HOME/sumeunjip"

if [[ -f "$pidfile" ]]; then
  pid=$(cat "$pidfile" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "llm server already running (pid $pid)"
    exit 0
  fi
fi

# flashinfer/triton JIT 가 venv 의 ninja·컴파일러 도구를 PATH 에서 찾는다.
export PATH="$venv/bin:$PATH"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1

setsid nohup "$venv/bin/vllm" serve "$model" \
  --host 127.0.0.1 --port 8001 \
  --served-model-name "$name" \
  --max-model-len 8192 \
  --gpu-memory-utilization "$util" \
  --no-enable-log-requests --no-enable-log-outputs \
  --disable-uvicorn-access-log \
  >> "$log" 2>&1 &

echo $! > "$pidfile"
echo "started (pid $(cat "$pidfile")) model=$name util=$util -> $log"
