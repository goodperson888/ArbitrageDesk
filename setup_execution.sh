#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv-execution
.venv-execution/bin/python -m pip install --proxy http://127.0.0.1:7890 -r requirements-runtime.txt -r requirements-execution.txt
.venv-execution/bin/python -B scripts/execution_cli.py
echo "独立执行依赖已准备；只检查状态，不启动实盘。"
