"""TradingView OHLCV and PineTS output cache for the EMA7 historical replay."""

import asyncio
from contextlib import closing
from datetime import datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


EMA7_MODEL = "ema7_autotrade_v2"
ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
SOURCE = ROOT / "indicators" / "ema7-autotrade-v2.pine"
# Reproduce the TradingView v3.0 settings used in the historical parity check.
INPUTS = {"offsetMultiplier": 0}
ET = ZoneInfo("America/New_York")
BAR_SECONDS = 300
WARMUP_BARS = 400
OHLCV = ("open", "high", "low", "close", "volume")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tv_5m_extended_splits (
    symbol TEXT NOT NULL,
    cutoff INTEGER NOT NULL,
    bar_limit INTEGER NOT NULL,
    dataset TEXT NOT NULL,
    PRIMARY KEY (symbol, cutoff, bar_limit)
);
CREATE TABLE IF NOT EXISTS pine_calculations (
    revision TEXT PRIMARY KEY,
    outputs TEXT NOT NULL
);
"""


class LocalPineHistoryError(RuntimeError):
    pass


def _encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


async def _node(script: str, *args: str, payload=None):
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", str(SERVICE / script), *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise LocalPineHistoryError("找不到 Node.js，无法运行本地回测") from error
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(_encode(payload).encode() if payload is not None else b""),
            timeout=40,
        )
    except (TimeoutError, asyncio.CancelledError) as error:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise LocalPineHistoryError("本地历史数据进程超时") from error
    if proc.returncode:
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise LocalPineHistoryError(detail[-1] if detail else "本地历史数据进程失败")
    try:
        return json.loads(stdout)
    except ValueError as error:
        raise LocalPineHistoryError("本地历史数据进程返回了无效结果") from error


def _validate_dataset(dataset: dict, cutoff: int, limit: int) -> dict:
    metadata = dataset["metadata"]
    if metadata.get("session") != "extended":
        raise LocalPineHistoryError("TradingView 未确认扩展交易时段")
    ZoneInfo(metadata["timezone"])
    if not all(math.isfinite(metadata[k]) and metadata[k] > 0 for k in ("minmove", "pricescale")):
        raise LocalPineHistoryError("TradingView 未返回有效的最小价格跳动")
    rows = []
    previous = 0
    for bar in dataset["bars"]:
        stamp = bar["time"]
        if type(stamp) is not int or stamp <= previous or stamp % BAR_SECONDS:
            raise LocalPineHistoryError("TradingView K 线时间重复、乱序或未对齐")
        previous = stamp
        if stamp + BAR_SECONDS > cutoff:
            continue
        if any(not isinstance(bar[k], (int, float)) or not math.isfinite(bar[k]) for k in OHLCV):
            raise LocalPineHistoryError("TradingView K 线含缺失或无效的 OHLCV")
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            raise LocalPineHistoryError("TradingView K 线的最高价或最低价无效")
        rows.append({key: bar[key] for key in ("time", *OHLCV)})
    if len(rows) < limit:
        raise LocalPineHistoryError(f"TradingView 只返回了 {len(rows)} 根已收盘 K 线，需 {limit} 根")
    return {"metadata": metadata, "bars": rows[-limit:]}


def _target_day(symbol: str, day: str, rows: list) -> str:
    regular = {}
    for index, row in enumerate(rows):
        bar = datetime.fromtimestamp(row["time"], ET)
        minute = bar.hour * 60 + bar.minute
        if 570 <= minute < 960:
            regular.setdefault(bar.date().isoformat(), {})[minute] = index
    resolved = day or max(regular, default="")
    target = regular.get(resolved, {})
    if target.keys() != set(range(570, 960, 5)):
        raise LocalPineHistoryError(
            f"{symbol} {resolved or '最近交易日'} 缺少完整常规盘 5 分钟 K 线，本地回测需要 78 根"
        )
    if target[570] < WARMUP_BARS:
        raise LocalPineHistoryError(f"{symbol} {resolved} 的策略预热数据少于 {WARMUP_BARS} 根")
    return resolved


class LocalPineHistory:
    def __init__(self, path: Path):
        self.path = path

    def _sql(self, statement: str, params: tuple, *, write=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(_SCHEMA)
            cursor = conn.execute(statement, params)
            return None if write else cursor.fetchone()

    async def load(self, symbol: str, day: str, bars: int, now: datetime):
        if not re.fullmatch(r"[A-Z0-9_]+:[A-Z0-9_.!\-]+", symbol):
            raise LocalPineHistoryError("请输入带交易所前缀的回测标的")
        target = datetime.fromisoformat(day).date() if day else now.date()
        anchor = datetime.combine(target, time(16), ET)
        if now < anchor:
            if day:
                raise LocalPineHistoryError("EMA7 本地回测只使用已结束的常规交易日")
            anchor -= timedelta(days=1)
        cutoff = int(anchor.timestamp())
        limit = max(600, bars)
        window = (symbol, cutoff, limit)
        stored = await asyncio.to_thread(
            self._sql, "SELECT dataset FROM tv_5m_extended_splits WHERE symbol=? AND cutoff=? AND bar_limit=?", window,
        )
        if stored is None:
            requested = min(5000, limit + 1)
            data = await _node("tv_backtest_bars.js", symbol, str(requested), str(cutoff))
            try:
                dataset = _validate_dataset(data, cutoff, limit)
            except (KeyError, TypeError, ValueError) as error:
                raise LocalPineHistoryError("TradingView 返回的 K 线或元数据格式无效") from error
        else:
            dataset = json.loads(stored[0])
        resolved = _target_day(symbol, day, dataset["bars"])
        if stored is None:
            await asyncio.to_thread(
                self._sql, "INSERT OR REPLACE INTO tv_5m_extended_splits VALUES (?, ?, ?, ?)",
                (*window, _encode(dataset)), write=True,
            )

        source = SOURCE.read_text(encoding="utf-8")
        lock_bytes = (ROOT / "package-lock.json").read_bytes()
        lock = json.loads(lock_bytes)
        version = lock["packages"]["node_modules/pinets"]["version"]
        context = {
            "source": "pinets",
            "pine_source_sha256": _digest(source.encode()),
            "pine_inputs": INPUTS,
            "pinets_version": version,
            "candle_revision": _digest(_encode({"symbol": symbol, **dataset}).encode()),
            "session": "extended",
            "adjustment": "splits",
            "bar_interval": "5m",
            "warmup_bars": WARMUP_BARS,
        }
        revision = _digest(_encode({
            **context,
            "lock": _digest(lock_bytes),
            "adapter": _digest(b"\n".join((SERVICE / name).read_bytes() for name in
                                         ("pinets_backtest.js", "pinets_runtime.js", "pine_models.js", "pine_strategies.json"))),
        }).encode())
        cached = await asyncio.to_thread(
            self._sql, "SELECT outputs FROM pine_calculations WHERE revision=?", (revision,),
        )
        if cached is None:
            response = await _node(
                "pinets_backtest.js", payload={"source": source, "inputs": INPUTS,
                                              "version": version,
                                              "datasets": [{"symbol": symbol, **dataset}]},
            )
            outputs = response.get(symbol, [])
            if [out["time"] for out in outputs] != [row["time"] for row in dataset["bars"]]:
                raise LocalPineHistoryError("PineTS 输出与 K 线时间未对齐")
            await asyncio.to_thread(
                self._sql, "INSERT OR REPLACE INTO pine_calculations VALUES (?, ?)",
                (revision, _encode(outputs)), write=True,
            )
        else:
            outputs = json.loads(cached[0])
        rows = [{**bar, "e7v2": output} for bar, output in zip(dataset["bars"], outputs, strict=True)]
        return resolved, rows, {
            **context, "calculation_revision": revision, "candles_reused": stored is not None,
            "calculation_reused": cached is not None, "candle_count": len(rows),
        }
