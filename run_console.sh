#!/usr/bin/env bash
# 启动下单控制台（默认 dry-run，不发真实订单）
# 实盘：EXECUTION_ENABLED=1 ./run_console.sh
set -euo pipefail
cd "$(dirname "$0")"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "启动下单控制台 http://${HOST}:${PORT}  (EXECUTION_ENABLED=${EXECUTION_ENABLED:-0})"
exec env EXECUTION_ENABLED="${EXECUTION_ENABLED:-0}" \
  python3 -m uvicorn console.app:app --host "${HOST}" --port "${PORT}"
