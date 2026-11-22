"""Read-only minute perception cache. No market subscriptions or execution access."""

import json
import math
from pathlib import Path
import threading
import time


def _validate(frame):
    if not isinstance(frame, dict) or frame.get("version") != 1:
        raise ValueError("联动行情缓存版本不匹配")
    if not isinstance(frame.get("series"), dict) or not isinstance(frame.get("beta"), list):
        raise ValueError("联动行情缓存结构不完整")
    if not isinstance(frame.get("as_of"), (int, float)) or not math.isfinite(frame["as_of"]):
        raise ValueError("联动行情确认时间无效")
    if not isinstance(frame.get("columns"), list) or not isinstance(frame.get("ranks"), list):
        raise ValueError("联动行情缓存字段不完整")
    for row in frame["series"].values():
        if not isinstance(row, dict) or not isinstance(row.get("points"), list):
            raise ValueError("联动行情序列无效")
        for p in row["points"]:
            if not isinstance(p, list) or len(p) != 11 or not isinstance(p[0], (int, float)):
                raise ValueError("联动行情K线字段无效")
        if not isinstance(row.get("events", []), list) or any(
            not isinstance(e, dict) or not isinstance(e.get("time"), (int, float))
            for e in row.get("events", [])
        ):
            raise ValueError("联动行情事件字段无效")
    if any(not isinstance(r, dict) or r.get("symbol") not in frame["series"] for r in frame["ranks"]):
        raise ValueError("联动行情候选字段无效")


class MarketRhythmReader:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp = None
        self._frame = None

    def read(self, symbol: str = "", *, watch: str = "", now=None):
        now = time.time() if now is None else now
        with self._lock:
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self._stamp:
                    if stat.st_size > 16 * 1024 * 1024:
                        raise ValueError("联动行情缓存超出大小限制")
                    frame = json.loads(self.path.read_text(encoding="utf-8"))
                    _validate(frame)
                    self._frame, self._stamp = frame, stamp
            except FileNotFoundError:
                return {"status": "waiting", "message": "等待一分钟收盘数据；首次加载需完成行情订阅", "tracks": [], "ranks": []}
            except (OSError, ValueError, TypeError) as error:
                return {"status": "error", "message": f"联动行情缓存读取失败：{error}", "tracks": [], "ranks": []}
            frame = self._frame
            as_of = frame.get("as_of") or 0
            if not as_of:
                return {"status": "waiting", "message": "正在订阅一分钟行情，等待首批收盘数据", "tracks": [], "ranks": []}
            if as_of > now:
                return {"status": "error", "message": "联动行情确认时间晚于当前时间", "tracks": [], "ranks": []}
            scoring = frame.get("scoring")
            if (not isinstance(scoring, dict) or scoring.get("model") != "ema9_15m_v3"
                    or type(scoring.get("version")) is not int or scoring["version"] != 3):
                return {"status": "waiting", "message": "等待重建EMA9／15分钟分类评分缓存", "tracks": [], "ranks": []}
            pending = scoring.get("pending")
            if not isinstance(pending, list):
                return {"status": "error", "message": "候选评分就绪字段无效", "tracks": [], "ranks": []}
            if pending:
                names = ", ".join(str(r.get("symbol", "未知")) for r in pending[:4] if isinstance(r, dict))
                return {"status": "waiting", "message": f"评分数据 {scoring.get('ready', 0)}/{scoring.get('eligible', 0)}，等待 {names}",
                        "scoring": scoring, "tracks": [], "ranks": []}
            available = [s for s in frame["series"] if s not in frame["beta"]]
            ranks = (frame.get("ranks") or [])[:10]
            ranked_symbols = [r["symbol"] for r in ranks]
            watched = list(dict.fromkeys(s for s in watch.split(",")[:10] if s in available))
            selected = symbol if symbol in [*frame["beta"], *ranked_symbols, *watched] else next(iter(ranked_symbols), "")
            tracks = []
            slots = [("beta", ticker, None) for ticker in frame["beta"]]
            slots.extend(("candidate", ranks[i]["symbol"] if i < len(ranks) else "", i + 1) for i in range(10))
            base_count = len(slots)
            slots.extend(("watch", ticker, None) for ticker in watched if ticker not in ranked_symbols)
            for role, ticker, rank in slots:
                row = frame["series"].get(ticker) or {"symbol": ticker, "points": [], "events": [], "issue": "missing_data"}
                metrics = next((r for r in ranks if r["symbol"] == ticker), {})
                td = row.get("td")
                if isinstance(td, dict) and isinstance(td.get("counts"), list):
                    td = {**td, "counts": [p for p in td["counts"] if isinstance(p, list) and len(p) == 2
                        and isinstance(p[0], (int, float)) and math.isfinite(p[0]) and p[0] + 60 <= as_of]}
                else:
                    td = None
                tracks.append({**row, "points": [p for p in row["points"] if p[0] + 60 <= as_of],
                    "td": td,
                    "events": [e for e in row.get("events", []) if e["time"] + 60 <= as_of],
                    "role": role, "rank": rank, "score": metrics.get("score"),
                    "direction_sign": metrics.get("direction_sign", next((p[10] for p in reversed(row["points"])
                        if p[0] + 60 <= as_of), 0)),
                    "is_beta": role == "beta", "aligned": (row.get("last_closed") or 0) + 60 == as_of})
            return {"status": "ready", "day": frame.get("day"), "as_of": as_of,
                "ranking_batch_id": frame.get('ranking_batch_id'),
                "scoring": scoring,
                "generated_at": frame.get("generated_at"), "age_seconds": max(0, now - as_of),
                "columns": frame["columns"], "coverage": frame.get("coverage"),
                "selected": selected, "symbols": sorted(available), "ranks": ranks,
                "tracks": tracks[:base_count], "watch_tracks": tracks[base_count:], "revision": self._stamp[0]}
