"""Accept complete minute snapshots for both display and admission. No broker access."""

import json
import math
from pathlib import Path
import threading
import time
import hashlib
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from execution.state_store import atomic_write


def _encoded(frame):
    return json.dumps(frame, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _premarket_reason(frame, expected_symbols, now):
    stamp = frame['as_of']
    at = datetime.fromtimestamp(stamp-60, ZoneInfo('America/New_York'))
    session = frame['ranking_session']
    release = session.get('ready_after')
    generated = frame.get('generated_at')
    if (session.get('version') != 1 or session.get('cohort') != 'available'
            or at.weekday() >= 5 or not 240 <= at.hour*60+at.minute < 570
            or release != stamp+10 or not isinstance(generated, (int, float))
            or not math.isfinite(generated) or generated < release
            or now is not None and now < release):
        return '盘前本分钟正在收集，保留上一批榜单'
    universe = frame.get('universe')
    if (not isinstance(universe, list) or not universe or any(not isinstance(s, str) for s in universe)
            or len(set(universe)) != len(universe)
            or expected_symbols is not None and set(universe) != expected_symbols):
        return '等待当前盘前股票池声明'
    coverage, series = frame.get('coverage') or {}, frame['series']
    if (type(coverage.get('total')) is not int or coverage['total'] != len(universe)
            or coverage.get('received') != len(series) or not set(series) <= set(universe)):
        return '盘前股票池覆盖信息不一致'
    aligned = set()
    for symbol, row in series.items():
        if row.get('last_closed') == stamp-60 and sum(p[0] == stamp-60 for p in row['points']) == 1:
            aligned.add(symbol)
    missing = coverage.get('missing')
    if (not aligned or type(coverage.get('aligned')) is not int or coverage['aligned'] != len(aligned)
            or not isinstance(missing, list) or len(missing) != len(set(missing))
            or set(missing) != set(universe)-aligned):
        return '等待盘前有效收盘分钟及准确缺数清单'
    pending = {r.get('symbol') for r in frame['scoring']['pending'] if isinstance(r, dict)}
    ranked = frame.get('ranks') or []
    if len({r.get('symbol') for r in ranked}) != len(ranked):
        return '盘前候选重复'
    for rank in ranked:
        symbol = rank.get('symbol')
        if symbol not in aligned or series[symbol].get('issue') or symbol in pending or rank.get('ready') is not True:
            return '等待盘前候选行情与指标齐备'
    return ''


def _batch_reason(frame, expected_symbols=None, *, now=None):
    """One acceptance contract shared by the API and execution admission reader."""
    as_of = frame.get('as_of')
    if (isinstance(as_of, bool) or not isinstance(as_of, (int, float))
            or not math.isfinite(as_of) or as_of <= 0 or as_of % 60):
        return '等待完整的一分钟收盘时刻'
    scoring = frame.get('scoring')
    if (not isinstance(scoring, dict) or scoring.get('model') != 'opening15_midpoint_v7'
            or type(scoring.get('version')) is not int or scoring['version'] != 7 or scoring.get('stock_session') != 'regular'):
        return '等待重建开盘小区间位置榜单'
    pending = scoring.get('pending')
    if not isinstance(pending, list):
        raise ValueError('候选评分就绪字段无效')
    if isinstance(frame.get('ranking_session'), dict) and frame['ranking_session'].get('phase') == 'premarket':
        return _premarket_reason(frame, expected_symbols, now)
    if pending and any(r.get('reason')=='opening_range_forming' for r in pending if isinstance(r,dict)):
        n=max((r.get('completed_minutes',0) for r in pending if isinstance(r,dict)),default=0)
        return f'小区间形成中，已收盘{int(n)}/15分钟；09:45起发布榜单并允许开仓'
    if pending:
        names = ', '.join(str(r.get('symbol', '未知')) for r in pending[:4] if isinstance(r, dict))
        return f"评分数据 {scoring.get('ready', 0)}/{scoring.get('eligible', 0)}，等待 {names}"
    coverage = frame.get('coverage')
    if (not isinstance(coverage, dict) or type(coverage.get('total')) is not int
            or coverage['total'] <= 0 or type(coverage.get('aligned')) is not int
            or coverage['aligned'] != coverage['total'] or coverage.get('missing') != []):
        return '等待一分钟收盘数据收齐'
    series = frame['series']
    if len(series) != coverage['total'] or (expected_symbols is not None and set(series) != expected_symbols):
        return '等待当前订阅股票池的一分钟收盘数据收齐'
    for row in series.values():
        stamp = row.get('last_closed')
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp + 60 != as_of
                or sum(p[0] == stamp for p in row['points']) != 1):
            return '等待全部标的对齐同一分钟收盘'
    return ''


def _freeze(frame):
    frame = deepcopy(frame)
    as_of = frame['as_of']
    for row in frame['series'].values():
        row['points'] = [p for p in row['points'] if p[0] + 60 <= as_of]
        row['events'] = [e for e in row.get('events', []) if e['time'] + 60 <= as_of]
        td = row.get('td')
        if isinstance(td, dict) and isinstance(td.get('counts'), list):
            td['counts'] = [p for p in td['counts'] if isinstance(p, list) and len(p) == 2
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p) and p[0] + 60 <= as_of]
    return frame


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

    views=frame.get('rankings')
    if views is not None:
        if not isinstance(views,dict) or set(views)!={'mixed','long','short'} or views['mixed']!=frame['ranks']:
            raise ValueError('方向榜与混合榜契约不一致')
        for view,rows in views.items():
            if not isinstance(rows,list) or len(rows)>10 or len({r.get('symbol') for r in rows if isinstance(r,dict)})!=len(rows):
                raise ValueError('方向榜名单无效')
            for i,r in enumerate(rows):
                if not isinstance(r,dict) or r.get('symbol') not in frame['series'] or r.get('rank')!=i+1:
                    raise ValueError('方向榜名次无效')
                if view!='mixed' and r.get('direction_sign')!=(1 if view=='long' else -1):
                    raise ValueError('方向榜方向无效')

    positions = frame.get('rank_positions')
    if positions is not None:
        if not isinstance(positions, dict) or views is None or positions.keys() != views.keys():
            raise ValueError('全池名次视图无效')
        for view, by_symbol in positions.items():
            if (not isinstance(by_symbol, dict)
                    or any(symbol not in frame['series'] or type(rank) is not int
                           for symbol, rank in by_symbol.items())
                    or set(by_symbol.values()) != set(range(1, len(by_symbol) + 1))):
                raise ValueError('全池名次无效')
            top = sorted((rank, symbol) for symbol, rank in by_symbol.items() if rank <= 10)
            if top != [(row['rank'], row['symbol']) for row in views[view]]:
                raise ValueError('全池名次与前十榜不一致')


class MarketRhythmReader:
    def __init__(self, path: Path, *, symbols=None):
        self.path = path
        self.accepted_path = path.with_name(path.stem + '.accepted.json')
        self.expected_symbols = set(symbols) if symbols is not None else None
        self._lock = threading.Lock()
        self._stamp = None
        self._frame = None
        self._accepted = None
        self._accepted_id = None
        self._accepted_at = None
        self._restored = False

    def _restore(self):
        if self._restored:
            return
        try:
            saved = json.loads(self.accepted_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            self._restored = True
            return
        if (not isinstance(saved, dict) or type(saved.get('version')) is not int
                or saved['version'] != 1 or not isinstance(saved.get('frame'), dict)):
            raise ValueError('已接纳榜单快照版本无效')
        frame = saved['frame']
        _validate(frame)
        identity = hashlib.sha256(_encoded(frame).encode()).hexdigest()
        if identity != saved.get('id'):
            raise ValueError('已接纳榜单快照校验失败')
        # An older score/session is a different model, not a damaged ledger.
        # Leave its cache on disk until a valid new-model batch is accepted.
        score=frame.get('scoring') or {}
        if (score.get('model') in ('ema9_15m_v3','vwap_z_v4','vwap_close_z_v5','vwap_open_ema5_v6')
                or score.get('model')=='opening15_midpoint_v7' and score.get('version')==7
                and score.get('stock_session')!='regular'):
            self._restored=True
            return
        # A deliberately changed subscription universe must complete its own batch.
        if not self.expected_symbols or set(frame.get('universe') or frame['series']) == self.expected_symbols:
            if _batch_reason(frame, self.expected_symbols, now=saved.get('accepted_at')):
                raise ValueError('已接纳榜单快照不完整')
            self._accepted, self._accepted_id = frame, identity
            self._accepted_at = saved.get('accepted_at')
        self._restored = True

    def _accept(self, frame, now):
        frozen = _freeze(frame)
        identity = hashlib.sha256(_encoded(frozen).encode()).hexdigest()
        saved = {'version': 1, 'id': identity, 'accepted_at': now, 'frame': frozen}
        # Publish only after durable atomic replacement. Never expose an uncommitted selection.
        atomic_write(self.accepted_path, _encoded(saved))
        self._accepted, self._accepted_id, self._accepted_at = frozen, identity, now

    def read(self, symbol: str = "", *, watch: str = "", now=None, latest_only=False):
        now = time.time() if now is None else now
        with self._lock:
            try:
                self._restore()
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self._stamp:
                    if stat.st_size > 16 * 1024 * 1024:
                        raise ValueError("联动行情缓存超出大小限制")
                    frame = json.loads(self.path.read_text(encoding="utf-8"))
                    _validate(frame)
                    self._frame, self._stamp = frame, stamp
            except FileNotFoundError:
                self._stamp = None
                if self._accepted is None:
                    return {"status": "waiting", "message": "等待一分钟收盘数据；首次加载需完成行情订阅", "tracks": [], "ranks": []}
                self._frame = {'as_of': 0, 'coverage': {}, 'scoring': {}}
            except (OSError, ValueError, TypeError) as error:
                return {"status": "error", "message": f"联动行情缓存读取失败：{error}", "tracks": [], "ranks": []}
            frame = self._frame
            as_of = frame.get("as_of") or 0
            if not as_of and self._accepted is None:
                return {"status": "waiting", "message": "正在订阅一分钟行情，等待首批收盘数据", "tracks": [], "ranks": []}
            if as_of > now:
                return {"status": "error", "message": "联动行情确认时间晚于当前时间", "tracks": [], "ranks": []}
            try:
                reason = _batch_reason(frame, self.expected_symbols, now=now)
                if not reason and (self._accepted is None or as_of > self._accepted['as_of']):
                    self._accept(frame, now)
            except (OSError, ValueError, TypeError) as error:
                return {'status': 'error', 'message': f'榜单快照接纳失败：{error}', 'tracks': [], 'ranks': []}
            progress = {'waiting': bool(reason), 'as_of': as_of, 'coverage': deepcopy(frame.get('coverage')),
                        'scoring': deepcopy(frame.get('scoring')), 'message': reason}
            if self._accepted is not None and as_of < self._accepted['as_of']:
                progress.update(waiting=True, message='源批次回退，保留已接纳榜单')
            if self._accepted is None:
                return {'status': 'waiting', 'message': reason, 'batch_progress': progress, 'tracks': [], 'ranks': []}
            frame = self._accepted
            as_of = frame['as_of']
            if as_of > now:
                return {'status': 'error', 'message': '已接纳榜单确认时间晚于当前时间', 'tracks': [], 'ranks': []}
            scoring = frame['scoring']
            available = [s for s in frame["series"] if s not in frame["beta"]]
            ranks = deepcopy((frame.get("ranks") or [])[:10])
            ranked_symbols = [r["symbol"] for r in ranks]
            watched = list(dict.fromkeys(s for s in watch.split(",")[:10] if s in available))
            selected = symbol if symbol in [*frame["beta"], *ranked_symbols, *watched] else next(iter(ranked_symbols), "")
            tracks = []
            slots = [("beta", ticker, None) for ticker in frame["beta"]]
            slots.extend(("candidate", ranks[i]["symbol"] if i < len(ranks) else "", i + 1) for i in range(10))
            base_count = len(slots)
            slots.extend(("watch", ticker, None) for ticker in watched if ticker not in ranked_symbols)
            ranked_views=deepcopy(frame.get("rankings") or {"mixed":ranks,"long":[],"short":[]})
            extra_symbols=list(dict.fromkeys(r["symbol"] for rows in ranked_views.values() for r in rows if r["symbol"] not in ranked_symbols and r["symbol"] not in watched))
            watch_end=len(slots)
            slots.extend(("ranking",ticker,None) for ticker in extra_symbols)
            for role, ticker, rank in slots:
                row = frame["series"].get(ticker) or {"symbol": ticker, "points": [], "events": [], "issue": "missing_data"}
                metrics = next((r for r in ranks if r["symbol"] == ticker), {})
                td = row.get("td")
                if isinstance(td, dict) and isinstance(td.get("counts"), list):
                    td = {**td, "counts": [p for p in td["counts"] if isinstance(p, list) and len(p) == 2
                        and isinstance(p[0], (int, float)) and math.isfinite(p[0]) and p[0] + 60 <= as_of]}
                else:
                    td = None
                points = [p for p in row["points"] if p[0] + 60 <= as_of]
                if latest_only:
                    points = points[-1:]
                    if td:
                        td['counts'] = [p for p in td['counts'] if p[0] == as_of-60]
                tracks.append({**row, "points": points,
                    "td": td,
                    "events": [] if latest_only else [e for e in row.get("events", []) if e["time"] + 60 <= as_of],
                    "role": role, "rank": rank, "score": metrics.get("score"),
                    "direction_sign": metrics.get("direction_sign", next((p[10] for p in reversed(row["points"])
                        if p[0] + 60 <= as_of), 0)),
                    "is_beta": role == "beta", "aligned": (row.get("last_closed") or 0) + 60 == as_of})
            result = {"status": "ready", "day": frame.get("day"), "as_of": as_of,
                "ranking_session": deepcopy(frame.get('ranking_session')),
                "ranking_batch_id": frame.get('ranking_batch_id'),
                "ranking_snapshot": {'version': 1, 'id': self._accepted_id, 'as_of': as_of,
                                     'day': frame.get('day'), 'accepted_at': self._accepted_at},
                "batch_progress": progress,
                "scoring": deepcopy(scoring),
                "generated_at": frame.get("generated_at"), "age_seconds": max(0, now - as_of),
                "columns": list(frame["columns"]), "coverage": deepcopy(frame.get("coverage")),
                "selected": selected, "symbols": sorted(available), "ranks": ranks,
                "tracks": tracks[:base_count],
                "watch_tracks": tracks[base_count:watch_end], "ranking_tracks":tracks[watch_end:],
                "rankings":ranked_views, "rank_positions":frame.get('rank_positions'), "revision": self._accepted_id}
            return deepcopy(result)
