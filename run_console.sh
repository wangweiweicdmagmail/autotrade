#!/usr/bin/env bash
# 启动下单控制台（默认 dry-run，不发真实订单）
# 实盘：EXECUTION_ENABLED=1 ./run_console.sh
set -euo pipefail
cd "$(dirname "$0")"
HOST="${HOST:-0.0.0.0}"   # 默认绑全网卡：局域网多客户端交易（非本机需 master 审批）
PORT="${PORT:-8000}"
echo "启动下单控制台 http://${HOST}:${PORT}  (EXECUTION_ENABLED=${EXECUTION_ENABLED:-0})"
exec env EXECUTION_ENABLED="${EXECUTION_ENABLED:-0}" \
  python3 -m uvicorn console.app:app --host "${HOST}" --port "${PORT}"
