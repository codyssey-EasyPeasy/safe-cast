#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log --log-level warning
