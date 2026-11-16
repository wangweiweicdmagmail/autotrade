#!/bin/bash
# 一键实盘重启（restart_live.sh）
# ================================================
# 流程：TWS 预检 → 杀旧 console/daemon → EXECUTION_ENABLED=1 启动
#       → 轮询验证（API / TV 数据按股票池就绪 / IBKR 连接）→ 打印结论。
# 用法：./restart_live.sh
# 退出码：0 = 全部就绪；1 = TV 数据未就绪；2 = IBKR 未连接（可登录 TWS 后等自动重连）
# -----------------------------------------------
set -u
cd "$(dirname "$0")"

API="http://127.0.0.1:8000"
LOG="logs/console-live.log"

echo "== [1/4] TWS 预检 =="
if nc -z 127.0.0.1 7496 2>/dev/null; then
  echo "  TWS 7496 已开放"
  TWS_OK=1
else
  echo "  ⚠️  TWS 7496 未开放（TWS 未登录/未启动）。启动后控制台会每 5s 自动重连，"
  echo "     请尽快在 TWS 窗口完成登录。"
  TWS_OK=0
fi

echo "== [2/4] 停止旧进程 =="
pkill -f "uvicorn console.app" 2>/dev/null && echo "  console 已停止" || echo "  无运行中的 console"
sleep 3
pkill -f tv_cloud_daemon 2>/dev/null
sleep 1

echo "== [3/4] 实盘启动 =="
echo "=== restart $(date) LIVE (restart_live.sh) ===" >> "$LOG"
guard_fa_default=1
if [[ -n "${EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT:-}" ]]; then
  guard_fa_default=0
fi
EXECUTION_ENABLED=1 \
EXTERNAL_MANUAL_GUARD_ENABLED="${EXTERNAL_MANUAL_GUARD_ENABLED:-1}" \
EXTERNAL_MANUAL_GUARD_FA_SCOPE="${EXTERNAL_MANUAL_GUARD_FA_SCOPE:-$guard_fa_default}" \
nohup python3 -m uvicorn console.app:app --host 0.0.0.0 --port 8000 >> "$LOG" 2>&1 &
echo "  已启动 pid $!（日志 ${LOG}）"

echo "== [4/4] 就绪验证 =="
python3 - <<'EOF'
import json, sys, time, urllib.request

t0 = time.time()

# macOS 系统代理（getproxies 读系统设置）会把 127.0.0.1 也代理掉导致
# RemoteDisconnected——本脚本必须绕开代理直连本机（实测踩坑）。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def get(path):
    with _opener.open(f"http://127.0.0.1:8000{path}", timeout=3) as r:
        return json.load(r)

# 阶段 1：API 上线（最多 300s；TWS 不在线时启动握手会显著变慢）
while time.time() - t0 < 300:
    try:
        st = get("/api/status")
        break
    except Exception:
        time.sleep(3)
else:
    print("  ❌ API 300s 未上线，请查 logs/console-live.log")
    sys.exit(1)
print(f"  API 上线（{time.time()-t0:.0f}s）")

# 阶段 2：使用控制台的行情就绪口径。明确返回其他时段的标的单独报告。
ready = expected = 0
unavailable = {}
tv_ok = False
while time.time() - t0 < 300:
    try:
        startup = get("/api/startup")
    except Exception:
        time.sleep(3)
        continue
    progress = startup["market_progress"]
    ready, expected = progress["confirmed"], progress["total"]
    pending = startup["market_pending"]
    unavailable = startup.get("market_session", {}).get("unavailable", {})
    if expected and not pending and progress["subscribed"] >= expected:
        tv_ok = ready > 0
        break
    print(f"  数据准备中… 确认指标 {ready}/{expected}，等待 {len(pending)}，"
          f"时段未确认 {len(unavailable)}（{time.time()-t0:.0f}s）", flush=True)
    time.sleep(3)
print(f"  {'✅' if tv_ok else '❌'} TV 确认指标 {ready}/{expected}（{time.time()-t0:.0f}s）")
for symbol, info in unavailable.items():
    print(f"  ⚠️  {symbol} 24 小时行情不可用（服务端返回 {info.get('resolved') or '订阅错误'}）")

# 阶段 3：IBKR 连接状态（只报告，不等待——重连循环会持续尝试）
st = get("/api/status")
conn = st.get("connected")
print(f"  {'✅' if conn else '⚠️ '} IBKR {'已连接' if conn else '未连接'}"
      + (f"，净值 ${st.get('equity'):,.0f}" if conn and st.get('equity') else "（TWS 登录后自动重连）"))
print(f"  模式: {'LIVE 实盘' if st.get('execution_enabled') else 'DRY-RUN'}"
      f"  持仓标的 {st.get('open_symbols')}  风险敞口 ${st.get('total_risk_usd'):.0f}/{st.get('max_total_risk_usd'):.0f}")

if not tv_ok:
    sys.exit(1)
sys.exit(0 if conn else 2)
EOF
RC=$?
echo "== 完成（退出码 ${RC}：0=全部就绪，1=TV 异常，2=等 TWS 登录）=="
exit ${RC}
