"""缓存某一天的全标的数据进研究库（cache_day.py）
==================================================
用法：
    python3 cache_day.py --day 2026-10-23                 # 全部自选
    python3 cache_day.py --day 2026-10-23 --symbols NASDAQ:NVDA,NYSE:TSM

流程：dump_trend_state.js（extended session，3 指标全套，锚定目标日 16:00 ET）
→ research_db.upsert_bars 幂等入库。重复执行安全（同 symbol+time 覆盖）。
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import research_db

ET = ZoneInfo("America/New_York")
DUMP_JS = Path(__file__).resolve().parent / "service" / "dump_trend_state.js"
RANGE = 400   # 400 根 × 5M ≈ 33h：覆盖目标日全天（含盘前盘后）+ 指标 warmup


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="美东交易日，如 2026-10-23")
    ap.add_argument("--symbols", default="", help="逗号分隔；缺省 = config.WATCHLIST 全部")
    ap.add_argument("--db", default=str(research_db.DEFAULT_DB))
    args = ap.parse_args()

    symbols = args.symbols.split(",") if args.symbols else list(config.WATCHLIST)
    anchor = int(datetime.strptime(args.day, "%Y-%m-%d")
                 .replace(hour=16, minute=0, tzinfo=ET).timestamp())
    print(f"缓存 {args.day}（美东）| {len(symbols)} 标的 | 锚点 {anchor}", flush=True)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name
    cmd = ["node", str(DUMP_JS), "--symbols", ",".join(symbols),
           "--to", str(anchor), "--range", str(RANGE), "--out", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        print("dump 失败:", r.stderr[-2000:], file=sys.stderr)
        sys.exit(1)

    payload = json.loads(Path(out_path).read_text(encoding="utf-8"))
    total = 0
    for sym, rows in payload.items():
        n = research_db.upsert_bars(sym, rows, Path(args.db))
        total += n
        print(f"  {sym}: {n} 行入库", flush=True)
    print(f"完成：{total} 行 / {len(payload)} 标的 → {args.db}", flush=True)


if __name__ == "__main__":
    main()
