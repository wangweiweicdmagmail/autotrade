"""events application functions and owned state. No console.app dependency."""

from typing import Optional
import logging
import time
from console.shared import logger


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
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("事件记录失败: %s", e)

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
        self, event_type: Optional[str] = None, limit: int = 500
    ) -> list:
        """查询历史事件（JSONL 持久日志，可回溯分析）。"""
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
                        r = _json.loads(line)
                        if event_type is None or r.get("type") == event_type:
                            records.append(r)
            except Exception:
                continue
        records.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return records[:limit]
