#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = stop ]; then
    exec ./start.sh stop
fi
API_V2_PORT="${API_V2_PORT:-5002}"
exec .venv/bin/python -m uvicorn src.api_v2.app:app --host 127.0.0.1 --port "$API_V2_PORT"
