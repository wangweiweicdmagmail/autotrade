"""research application functions and owned state. No console.app dependency."""

from engine.backtest_session import BacktestSession
from engine.local_pine_history import EMA7_MODEL, LocalPineHistory, LocalPineHistoryError
from fastapi import HTTPException
from pathlib import Path
from collections import OrderedDict
import asyncio
import config
from engine import shadow_report
import time
from console.shared import CONSOLE_FILE, logger
from console.schemas import BtLoadRequest, BtRunRequest
from console.services.rhythm import MarketRhythmReader


class ResearchService:
    def __init__(self, context):
        self.context = context
        self._backtest_history = OrderedDict()
        self._backtest_history_lock = asyncio.Lock()
        self._local_pine_history = LocalPineHistory(context.paths.data_dir / "backtest_history.sqlite3")
        self._market_rhythm = MarketRhythmReader(context.paths.data_dir / "market_rhythm.json",
            symbols=[s for s in config.WATCHLIST if s not in config.TV_DEFAULT_SESSION_SYMBOLS
                     or s in ('CAPITALCOM:NAS100', 'CAPITALCOM:SPX500')])

    def entry_candidates(self, now=None, *, ranking='trend', view='mixed'):
        from execution.entry_admission import candidate_snapshot
        now=time.time() if now is None else now
        return candidate_snapshot(self._market_rhythm.read(now=now, latest_only=True),now,self.context.collector.latest(),ranking=ranking,view=view)

    def check_candidate_entry(self, symbol, side, now, *, ranking='trend', view='mixed'):
        from execution.entry_admission import check_candidate
        return check_candidate(self.entry_candidates(now,ranking=ranking,view=view),symbol,side)

    def entry_candidate_snapshots(self, now=None):
        from execution.entry_admission import candidate_snapshot
        from execution.confirmed_minute import build_snapshot
        now=time.time() if now is None else now
        frame=self._market_rhythm.read(now=now,latest_only=True)
        market=self.context.collector.latest()
        td=build_snapshot(market,now)
        direction=build_snapshot(market,now,require_td=False)
        views={view:candidate_snapshot(frame,now,market,view=view,td_snapshot=td,direction_snapshot=direction) for view in ('mixed','long','short')}
        rank_monitor = None
        if frame.get('status') == 'ready' and (frame.get('ranking_session') or {}).get('phase') != 'premarket':
            rank_monitor = {
                'snapshot': frame['ranking_snapshot'],
                'top10': [row['symbol'] for row in frame['ranks']],
                'positions': (frame.get('rank_positions') or {}).get('mixed', {}),
            }
        return {'trend':views['mixed'],'views':views,'confirmed_td':td,'confirmed_vwap':direction,
                'rank_monitor':rank_monitor}

    async def api_market_rhythm(self, symbol: str = "", watch: str = "") -> dict:
        return await asyncio.to_thread(self._market_rhythm.read, symbol, watch=watch)

    async def api_bt_run(self, req: BtRunRequest) -> dict:
        """Reuse historical inputs while running a fresh private simulation."""
        from datetime import datetime, time as clock_time
        import math
        from engine.pine_models import PineModelRegistry
        from backtest_trend import ET, fetch_history

        symbol = req.symbol.strip().upper()
        if not symbol:
            raise HTTPException(400, "请输入回测标的")
        if ":" not in symbol:
            symbol = next((s for s in config.WATCHLIST if s.split(":")[-1] == symbol), symbol)
        day = req.day
        now = datetime.now(ET)
        try:
            if day and datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
                raise ValueError("交易日格式应为 YYYY-MM-DD")
            registry = PineModelRegistry.load()
            if registry.get(req.strategy).local_only:
                raise ValueError("该策略尚未接入历史回测")
            for key in ("initial_risk_usd", "equity", "pyramid_breakeven_r", "pyramid_add_unlock_r"):
                if key in req.params:
                    value = req.params[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError(f"{key} 必须为有限正数")
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        # Invalidate the default lookup at the next ET date or regular close.
        current_day = now.date().isoformat()
        window = (current_day, now.time() >= clock_time(16)) if day in ("", current_day) else None
        key = (symbol, day, req.strategy, req.bars, window)
        local_history = None
        async with self._backtest_history_lock:
            cached = self._backtest_history.get(key)
            reused = cached is not None
            if req.strategy == EMA7_MODEL:
                try:
                    day, rows, local_history = await self._local_pine_history.load(symbol, day, req.bars, now)
                except LocalPineHistoryError as error:
                    raise HTTPException(400, f"EMA7 本地回测数据不可用: {error}") from error
                reused = local_history["calculation_reused"]
            elif cached is None:
                try:
                    rows = await fetch_history(symbol, req.bars, day)
                except Exception as error:
                    raise HTTPException(400, f"历史数据拉取失败: {error}") from error
                if not day:
                    bar_times = (datetime.fromtimestamp(row["time"], ET) for row in rows)
                    days = {
                        bar.date() for bar in bar_times
                        if clock_time(9, 30) <= bar.time() < clock_time(16)
                        and (bar.date() < now.date() or
                             (bar.date() == now.date() and now.time() >= clock_time(16)))
                    }
                    if not days:
                        raise HTTPException(400, f"{symbol} 没有已收盘交易日的常规时段数据")
                    day = max(days).isoformat()
                self._backtest_history[key] = (day, rows)
                if len(self._backtest_history) > 8:
                    self._backtest_history.popitem(last=False)
            else:
                day, rows = cached
                self._backtest_history.move_to_end(key)
        try:
            session = BacktestSession(
                rows, symbol, day, req.strategy, req.params,
                registry=registry, start_time=req.start_time,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if not session.total_bars:
            raise HTTPException(400, f"{symbol} 在 {day} {req.start_time} ET 后无常规时段数据")
        # CPU-only simulation on a separate thread keeps the live event loop responsive.
        # The session and simulated broker are local to this request.
        result = await asyncio.to_thread(lambda: asyncio.run(session.run_all()))
        result["history_reused"] = reused
        if local_history is not None:
            result["run_context"]["history"] = local_history
        return result

    async def api_shadow(self) -> dict:
        """当日影子回测：统计 + 全部回踩事件（实时逐 bar 推演）。"""
        return {
            "stats": self.context.shadow_tracker.stats(),
            "events": self.context.shadow_tracker.events(200),
        }

    async def api_shadow_report(self, day: str = "") -> dict:
        """每日影子复盘报告：缺省=当天（ET），任意历史日可重算（实时计算）。"""
        import re as _re
        from zoneinfo import ZoneInfo
        import datetime as _dt

        if day and (not _re.match("^\\d{4}-\\d{2}-\\d{2}$", day)):
            raise HTTPException(400, "day 格式应为 YYYY-MM-DD")
        if not day:
            day = _dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        return shadow_report.generate_report(day)

    async def api_bt_load(self, req: BtLoadRequest) -> dict:
        """加载回测数据并初始化会话（拉取 5M bar + trend_state，约 5-10s）。"""
        from datetime import datetime as _dt
        from backtest_trend import ET, fetch_history, prev_trading_day

        symbol = req.symbol.strip().upper()
        if ":" not in symbol:
            match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
            if match:
                symbol = match[0]
        day = req.day or prev_trading_day(_dt.now(ET)).date().isoformat()
        try:
            rows = await fetch_history(symbol, req.bars, day)
        except Exception as e:
            raise HTTPException(400, f"历史数据拉取失败: {e}")
        try:
            session = BacktestSession(rows, symbol, day, req.strategy, req.params)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if session.total_bars == 0:
            raise HTTPException(400, f"{symbol} 在 {day} 无常规时段数据")
        self.context.research._bt_session = session
        logger.info(
            "回测会话加载 %s %s [%s]：%d 根 bar",
            symbol,
            day,
            req.strategy,
            session.total_bars,
        )
        return self.context.research._bt_session.state()

    async def api_bt_step(self, n: int = 1) -> dict:
        """单步：前进 n 根 bar（默认 1 根）。"""
        if self.context.research._bt_session is None:
            raise HTTPException(400, "请先加载回测数据（/api/bt/load）")
        return await self.context.research._bt_session.step(n)

    async def api_bt_run_all(self) -> dict:
        """一次性跑完当日剩余全部 bar。"""
        if self.context.research._bt_session is None:
            raise HTTPException(400, "请先加载回测数据（/api/bt/load）")
        return await self.context.research._bt_session.run_all()

    async def api_bt_state(self) -> dict:
        """当前回测会话状态（进度/当前 bar/实例/订单生命周期/汇总）。"""
        if self.context.research._bt_session is None:
            raise HTTPException(400, "无活跃回测会话")
        return self.context.research._bt_session.state()

    async def api_backtest(self, symbol: str, day: str = "", bars: int = 500) -> dict:
        """历史回测：把指定交易日的 5M bar 逐根喂给回测引擎，
        返回每一次入场信号的统计与 N 倍盈亏比分布。"""
        from datetime import datetime as _dt
        from backtest_trend import ET, fetch_history, prev_trading_day, run_backtest

        symbol = symbol.strip().upper()
        if ":" not in symbol:
            match = [s for s in config.WATCHLIST if s.split(":")[-1] == symbol]
            if match:
                symbol = match[0]
        if not day:
            day = prev_trading_day(_dt.now(ET)).date().isoformat()
        try:
            rows = await fetch_history(symbol, bars)
        except Exception as e:
            raise HTTPException(400, f"历史数据拉取失败: {e}")
        return {"symbol": symbol, **run_backtest(rows, day)}

    async def api_performance(self) -> dict:
        """复盘统计：从历史归档（logs/trades-*.json）读取平仓记录。"""
        records = self.context.order_mgr._store.archive_records()
        closed = [r for r in records if r.get("state") == "CLOSED"]

        def _stats(rows) -> dict:
            if not rows:
                return {
                    "count": 0,
                    "win_rate": 0,
                    "avg_win": 0,
                    "avg_loss": 0,
                    "profit_factor": None,
                    "total_pnl": 0,
                    "avg_mae": 0,
                    "avg_mfe": 0,
                    "avg_target_rr": 0,
                    "avg_actual_rr": 0,
                    "expectancy": 0,
                    "breakeven_rate": 0,
                    "avg_breakeven_min": 0,
                    "avg_stop_min": 0,
                    "avg_hold_min": 0,
                }
            wins = [r for r in rows if r.get("realized_pnl", 0) > 0]
            losses = [r for r in rows if r.get("realized_pnl", 0) <= 0]
            total = sum((r.get("realized_pnl", 0) for r in rows))
            avg_win = (
                sum((r["realized_pnl"] for r in wins)) / len(wins) if wins else 0.0
            )
            avg_loss = (
                sum((r["realized_pnl"] for r in losses)) / len(losses)
                if losses
                else 0.0
            )
            pf = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None
            avg_mae = sum((r.get("mae", 0) for r in rows)) / len(rows)
            avg_mfe = sum((r.get("mfe", 0) for r in rows)) / len(rows)
            avg_target_rr = sum((r.get("target_rr", 0) for r in rows)) / len(rows)
            avg_actual_rr = sum((r.get("actual_rr", 0) for r in rows)) / len(rows)
            be_rows = [r for r in rows if r.get("breakeven_reached")]
            be_rate = len(be_rows) / len(rows) * 100 if rows else 0.0
            avg_be_sec = (
                sum((r.get("breakeven_seconds", 0) for r in be_rows)) / len(be_rows)
                if be_rows
                else 0.0
            )
            stop_rows = [r for r in rows if r.get("stop_seconds", 0) > 0]
            avg_stop_sec = (
                sum((r.get("stop_seconds", 0) for r in stop_rows)) / len(stop_rows)
                if stop_rows
                else 0.0
            )
            avg_exit_sec = (
                sum((r.get("exit_seconds", 0) for r in rows)) / len(rows)
                if rows
                else 0.0
            )
            return {
                "count": len(rows),
                "win_rate": round(len(wins) / len(rows) * 100, 1),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": pf,
                "total_pnl": round(total, 2),
                "expectancy": round(total / len(rows), 2),
                "avg_mae": round(avg_mae, 2),
                "avg_mfe": round(avg_mfe, 2),
                "avg_target_rr": round(avg_target_rr, 2),
                "avg_actual_rr": round(avg_actual_rr, 2),
                "breakeven_rate": round(be_rate, 1),
                "avg_breakeven_min": round(avg_be_sec / 60, 1),
                "avg_stop_min": round(avg_stop_sec / 60, 1),
                "avg_hold_min": round(avg_exit_sec / 60, 1),
            }

        def _group(key) -> dict:
            out = {}
            for r in closed:
                k = r.get(key) or "未知"
                out.setdefault(k, []).append(r)
            return {k: _stats(v) for k, v in out.items()}

        return {
            "count": len(closed),
            "overall": _stats(closed),
            "by_type": _group("limit_type"),
            "by_side": _group("side"),
            "by_symbol": _group("symbol"),
            "records": closed,
        }

    async def api_returns_compare(self, start: str = "", end: str = "") -> dict:
        """按日期范围对比账户已实现日收益与 QQQ 收盘收益。"""
        from datetime import datetime as _dt
        from engine import returns_comparison as rc

        try:
            start_day, end_day = rc.parse_range(start, end)
        except ValueError as e:
            raise HTTPException(400, str(e))
        logs_dir = self.context.paths.data_dir
        daily_pnl, anchors = await asyncio.to_thread(rc.load_account_daily, logs_dir)
        status = await self.context.accounts.get_status_cached()
        now_et = _dt.now(rc.ET)
        equity = status.get("equity")
        today_pnl = status.get("daily_pnl")
        if (
            self.context.gateway._daily_pnl_session_active(now_et)
            and isinstance(equity, (int, float))
            and isinstance(today_pnl, (int, float))
        ):
            sod_equity = equity - today_pnl
            if sod_equity > 0:
                today = now_et.date().isoformat()
                anchors[today] = sod_equity
                await asyncio.to_thread(
                    rc.save_equity_anchor, logs_dir, today, sod_equity
                )
        qqq_closes, fetched_at = await asyncio.to_thread(rc.load_qqq_daily, logs_dir)
        needs_fetch = (
            not qqq_closes
            or time.time() - fetched_at > 6 * 3600
            or min(qqq_closes) > start_day.isoformat()
        )
        warnings = []
        if needs_fetch:
            try:
                bars = await rc.fetch_qqq_daily(
                    Path(str(CONSOLE_FILE)).resolve().parent.parent,
                    (end_day - start_day).days + 30,
                )
                await asyncio.to_thread(rc.save_qqq_daily, logs_dir, bars)
                for row in bars:
                    day = _dt.fromtimestamp(row["time"], rc.ET).date().isoformat()
                    qqq_closes[day] = float(row["close"])
            except Exception as e:
                logger.warning("QQQ 日线刷新失败，使用本地缓存: %s", e)
                warnings.append(f"QQQ 日线刷新失败，当前展示本地缓存：{e}")
        rows, summary = rc.build_rows(
            start_day, end_day, daily_pnl, anchors, qqq_closes
        )
        if any((row["account_estimated"] for row in rows)):
            warnings.append(
                "部分日起始净值由最近净值锚点和平仓盈亏推算，未计入出入金及未归档交易"
            )
        if not qqq_closes:
            warnings.append("暂无 QQQ 行情数据")
        return {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "symbol": rc.QQQ_SYMBOL,
            "account_basis": "realized_pnl",
            "rows": rows,
            "summary": summary,
            "warnings": warnings,
        }
