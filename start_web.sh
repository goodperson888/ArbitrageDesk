#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = stop ]; then
    exec ./start.sh stop
fi
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -B -c 'import flask, requests, yaml, dotenv, websocket' >/dev/null 2>&1; then
    echo "正在准备独立运行环境…"
    bash setup.sh
fi
mkdir -p logs data
if .venv/bin/python -B - <<'PY'
import json, urllib.request
try:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:5001/api/health',timeout=1) as r:
        data=json.load(r)
    raise SystemExit(0 if data.get('service')=='hedge-monitor' else 1)
except Exception:
    raise SystemExit(1)
PY
then
    echo "行情面板已经运行：http://127.0.0.1:5001"
    echo "如刚修改代码，请先在原终端 Ctrl+C 停止，再重新运行本脚本。"
    exit 0
fi
echo "行情面板：http://127.0.0.1:5001"
echo "代理读取 config/config.yaml → common.network.proxy_url；当前不会发送订单。"
echo "首次连接约需几秒，Ctrl+C 停止服务。"
exec .venv/bin/python -B src/web/app.py
