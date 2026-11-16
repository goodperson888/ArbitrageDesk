#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-runtime.txt
mkdir -p logs data
if [ ! -f config/config.yaml ]; then
    cp config/config.example.yaml config/config.yaml
fi
echo "安装完成。运行 ./start.sh；公开行情无需 .env 或钱包。"
echo "旧 venv/ 中的浏览器和抓包工具保持独立。"
