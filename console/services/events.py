"""events application functions and owned state. No console.app dependency."""

from typing import Optional
import asyncio
import logging
import time
import uuid
from console.shared import logger
from engine.live_audit import decision_market_frame


class _BufferHandler(logging.Handler):
    def __init__(self, context):
        super().__init__()
        self.context = context

    "把日志写入内存环形缓冲区，供 /api/logs 与 WebSocket 推送。"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.context.events._log_buffer.append(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "name": record.name.split(".")[-1],
                    "msg": record.getMessage(),
                }
            )
        except Exception:
            pass


class EventsService:
    def __init__(self, context):
        self.context = context

    def record_event(self, event_type: str, **data) -> None:
        """追加一条事件到当日 JSONL 日志（持久化，可回溯分析）。"""
        import json as _json

        try:
            day = time.strftime("%Y%m%d")
            path = self.context.paths.data_dir / f"events-{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.time(), "type": event_type, **data}
            rec.setdefault('event_id', str(uuid.uuid4()))
            decision_frame = decision_market_frame.get()
            event_frame = getattr(getattr(self.context, "collector", None), "market_frame_id", None)
            if decision_frame is not None:
                rec.setdefault("decision_market_frame", decision_frame)
            if event_frame is not None:
                rec.setdefault("market_frame_at_event", event_frame)
            try:
                self._attach_research(rec, data, event_frame)
            except Exception as error:
                rec['research_evidence_error'] = str(error)
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("事件记录失败: %s", e)

    def _attach_research(self, rec, data, event_frame):
        # Observational metadata only; never invokes an order or mutates a lot.
        if data.get('lot_id') or data.get('symbol'):
            lot = getattr(getattr(self.context, 'order_mgr', None), '_lots', {}).get(data.get('lot_id'))
            symbol = data.get('symbol') or getattr(lot, 'symbol', None)
            collector = getattr(self.context, 'collector', None)
            latest = collector.latest() if collector and hasattr(collector, 'latest') else {}
            row = latest.get(symbol, {})
            if not row and symbol:
                matches = [r for k,r in latest.items() if k.split(':')[-1] == symbol]
                row = matches[0] if len(matches) == 1 else {}
            rec['research_evidence'] = {'symbol': symbol, 'role': 'event_observation',
                'frame_id': event_frame, 'market': {k:row[k] for k in
                ('bar','bar_c','market_1m','ts','ts_c','e9','e9_c','e7v2','e7v2_c',
                 'vwap_1m','candidate_st_1m_c','td_exit_1m_c','rhythm_confirmed_1m','rhythm_price_1m','price_observed_at','session') if k in row}}
            rec['research_evidence']['beta'] = {key:{k:latest[key][k] for k in
                ('bar','market_1m','rhythm_price_1m','rhythm_confirmed_1m','td_exit_1m_c','price_observed_at') if k in latest[key]}
                for key in ('CAPITALCOM:NAS100','CAPITALCOM:SPX500') if key in latest}
            settings = getattr(self.context, 'settings', None)
            if settings and hasattr(settings, '_candidate_order_protection'):
                rec['research_evidence']['candidate_protection'] = settings._candidate_order_protection()
            if settings and hasattr(settings, '_intelligent_exit'):
                rec['research_evidence']['intelligent_exit'] = settings._intelligent_exit()

    def _on_lot_closed(self, d: dict) -> None:
        """保护单触发 / 对账补登的平仓事件：完整字段落事件流，供复盘。"""
        self.context.events.record_event(
            "lot_closed",
            **{
                k: d.get(k)
                for k in (
                    "lot_id",
                    "symbol",
                    "side",
                    "qty",
                    "filled_qty",
                    "avg_fill",
                    "exit_px",
                    "realized_pnl",
                    "actual_rr",
                    "target_rr",
                    "mae",
                    "mfe",
                    "exit_reason",
                    "limit_type",
                    "entry_time",
                    "closed_at",
                    "reason",
                )
            },
        )

    def _on_lot_revived(self, d: dict) -> None:
        """OCA 竞态漏单后的对账复活：落事件流，供审计回溯。"""
        self.context.events.record_event(
            "lot_revived",
            **{
                k: d.get(k)
                for k in (
                    "lot_id",
                    "symbol",
                    "side",
                    "qty",
                    "filled_qty",
                    "avg_fill",
                    "stop_px",
                    "limit_type",
                    "note",
                )
            },
        )

    async def api_logs(self, limit: int = 200) -> list:
        """返回最近 N 条日志（内存环形缓冲区）。"""
        logs = list(self.context.events._log_buffer)
        return logs[-limit:]

    async def api_events(
        self, event_type: Optional[str] = None, limit: int = 500,
        *, lot_id: Optional[str] = None, order_id: Optional[int] = None,
    ) -> list:
        """查询历史事件（JSONL 持久日志，可回溯分析）。"""
        return await asyncio.to_thread(self._read_events, event_type, limit, lot_id, order_id)

    def _read_events(self, event_type, limit, lot_id, order_id):
        import glob
        import json as _json

        logs_dir = self.context.paths.data_dir
        records = []
        for f in sorted(glob.glob(str(logs_dir / "events-*.jsonl")), reverse=True):
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = _json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(r, dict):
                            continue
                        if event_type is not None and r.get("type") != event_type:
                            continue
                        if lot_id is not None and r.get("lot_id") != lot_id:
                            continue
                        if order_id is not None and order_id not in (
                            r.get("order_id"), r.get("entry_order_id"), r.get("stop_order_id"),
                        ):
                            continue
                        records.append(r)
            except Exception:
                continue
        records.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return records[:limit]
