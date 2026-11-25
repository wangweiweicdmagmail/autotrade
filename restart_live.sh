#!/bin/bash
# 一键实盘重启（restart_live.sh）
# ================================================
# 流程：TWS 预检 → 等待旧进程退出和订单写锁释放 → EXECUTION_ENABLED=1 启动
#       → 轮询验证（API / TV 数据按股票池就绪 / IBKR 连接）→ 打印结论。
# 用法：./restart_live.sh
# 仅检查 Python（不重启）：./restart_live.sh --check-python
# AUTOTRADE_SHUTDOWN_TIMEOUT：停止旧进程和等待写锁的总时限，默认 120 秒。
# 退出码：0 = 全部就绪；1 = 重启失败或 TV 数据未就绪；2 = IBKR 未连接（可登录 TWS 后等自动重连）
# -----------------------------------------------
set -u
cd "$(dirname "$0")"

API="http://127.0.0.1:8000"
LOG="logs/console-live.log"

# Resolve and validate the interpreter before stopping a working console.
# GUI/tool shells may resolve python3 to Homebrew while this project uses Conda.
PYTHON_BIN="${AUTOTRADE_PYTHON:-}"
if [[ -n "$PYTHON_BIN" ]]; then
  if ! "$PYTHON_BIN" -c 'import uvicorn, fastapi, ib_async, websockets' >/dev/null 2>&1; then
    echo "错误：AUTOTRADE_PYTHON 缺少项目依赖；未停止控制台。"
    exit 1
  fi
else
  for candidate in "$PWD/.venv/bin/python" python3.11 /opt/anaconda3/bin/python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import uvicorn, fastapi, ib_async, websockets' >/dev/null 2>&1; then
      PYTHON_BIN="$("$candidate" -c 'import sys; print(sys.executable)')"
      break
    fi
  done
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "错误：未找到已安装项目依赖的 Python；请设置 AUTOTRADE_PYTHON。未停止控制台。"
    exit 1
  fi
fi
if [[ "${1:-}" == "--check-python" ]]; then
  echo "$PYTHON_BIN"
  exit 0
fi
echo "使用 Python：$PYTHON_BIN"

echo "== [1/4] TWS 预检 =="
if nc -z 127.0.0.1 7496 2>/dev/null; then
  echo "  TWS 7496 已开放"
  TWS_OK=1
else
  echo "  ⚠️  TWS 7496 未开放（TWS 未登录/未启动）。启动后控制台会每 5s 自动重连，"
  echo "     请尽快在 TWS 窗口完成登录。"
  TWS_OK=0
fi

echo "== [2/4] 停止旧进程并等待订单写锁释放 =="
guard_fa_default=1
if [[ -n "${EXTERNAL_MANUAL_GUARD_TEST_ACCOUNT:-}" ]]; then
  guard_fa_default=0
fi
if ! EXECUTION_ENABLED=1 \
EXTERNAL_MANUAL_GUARD_ENABLED="${EXTERNAL_MANUAL_GUARD_ENABLED:-1}" \
EXTERNAL_MANUAL_GUARD_FA_SCOPE="${EXTERNAL_MANUAL_GUARD_FA_SCOPE:-$guard_fa_default}" \
"$PYTHON_BIN" -u - "$LOG" <<'PY_LAUNCH'
import fcntl
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from execution.writer_lock import acquire_writer_lock, release_writer_lock


def process_table():
    rows = subprocess.run(
        ['ps', '-axww', '-o', 'pid=', '-o', 'stat=', '-o', 'command='],
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout.splitlines()
    processes = {}
    for row in rows:
        fields = row.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and not fields[1].startswith('Z'):
            processes[int(fields[0])] = fields[2]
    return processes


def wait_until_ready(check, deadline, blocked_message):
    while not check():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(blocked_message)
        time.sleep(min(0.25, remaining))


def stop_processes(pattern, label, deadline):
    old_pids = {pid for pid, command in process_table().items() if pattern.search(command)}
    if not old_pids:
        return
    for pid in old_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    names = ', '.join(map(str, sorted(old_pids)))
    print(f'  等待{label}退出（PID {names}）')
    wait_until_ready(
        lambda: not old_pids.intersection(process_table()), deadline,
        f'{label}未在时限内退出（PID {names}）',
    )


def writer_available(path):
    if not acquire_writer_lock(str(path)):
        return False
    release_writer_lock()
    return True


def launch(log_path, timeout):
    console = re.compile(r'(?:^|[\s/])uvicorn\s+console\.app:app(?:\s|$)')
    daemon_path = re.escape(str(Path('service/tv_cloud_daemon.js').resolve()))
    daemon = re.compile(r'(?:^|\s)' + daemon_path + r'(?:\s|$)')
    writer_path = log_path.with_name('autotrade_writer.lock')
    with log_path.with_name('autotrade_restart.lock').open('a') as restart_lock:
        try:
            fcntl.flock(restart_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('另一个重启脚本正在停止或启动控制台') from error
        with log_path.open('ab', buffering=0) as log:
            deadline = time.monotonic() + timeout
            # Let the console finish cleanup before stopping any orphaned TV daemon.
            stop_processes(console, '旧控制台', deadline)
            stop_processes(daemon, '旧 TV 行情进程', deadline)
            wait_until_ready(
                lambda: writer_available(writer_path), deadline,
                '订单写锁仍被占用',
            )
            if any(console.search(command) or daemon.search(command)
                   for command in process_table().values()):
                raise RuntimeError('等待期间出现了新的控制台或 TV 行情进程')
            print('  已确认旧进程退出、订单写锁释放')
            print('== [3/4] 实盘启动 ==')
            stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
            log.write(f'=== restart {stamp} LIVE (restart_live.sh) ===\n'.encode())
            # The application itself acquires the writer lock again during startup.
            process = subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'console.app:app', '--host', '0.0.0.0', '--port', '8000'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
    print(f'  已启动 pid {process.pid}（日志 {log_path}）')


try:
    timeout = float(os.environ.get('AUTOTRADE_SHUTDOWN_TIMEOUT', '120'))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('AUTOTRADE_SHUTDOWN_TIMEOUT 必须为正数')
    launch(Path(sys.argv[1]), timeout)
except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
    sys.exit(f'错误：{error}。未启动新控制台，请检查日志。')
PY_LAUNCH
then
  exit 1
fi

echo "== [4/4] 就绪验证 =="
"$PYTHON_BIN" - <<'EOF'
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
tv_deadline = time.time() + 300
last_login_state = None
while time.time() < tv_deadline:
    try:
        startup = get("/api/startup")
    except Exception:
        time.sleep(3)
        continue
    login = startup.get("tv_login", {})
    if login.get("required"):
        state = (login.get("state"), login.get("detail"))
        if state != last_login_state:
            print("  等待 TradingView 登录：" + (login.get("detail") or "请在专用 Chrome 登录自己的账户，登录后自动继续。"), flush=True)
            last_login_state = state
        # The user completing login is not a stalled market subscription.
        tv_deadline = time.time() + 300
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
try:
    st = get("/api/status")
except Exception as error:
    print(f"  ⚠️  最终状态读取未确认：GET /api/status：{type(error).__name__}: {error}；控制台继续运行，请重新检查。")
    sys.exit(2)
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
