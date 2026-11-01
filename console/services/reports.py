"""reports application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
from typing import Optional
from pathlib import Path
import datetime as _dt
import asyncio
import json
from engine import news_digest
import os
import re
from engine import shadow_report
import time
from console.shared import logger
from console.schemas import ReportSourceAddRequest


class ReportsService:
    def __init__(self, context):
        self.context = context

    async def _generate_daily_report(
        self, day: str, db_path: Optional[Path] = None
    ) -> dict:
        """生成并落盘当日复盘报告（logs/report-YYYYMMDD.json）+ 事件 +
        iMessage 摘要（未配置收件人自动跳过）。"""
        report = shadow_report.generate_report(day, db_path=db_path)
        path = self.context.reports._REPORTS_DIR / f"report-{day.replace('-', '')}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)
        bm = report["big_moves"]
        self.context.events.record_event(
            "shadow_report",
            day=day,
            total=report["total_signals"],
            big_moves=bm["total_big_moves"],
            caught=bm["caught_count"],
            missed=bm["missed_count"],
            path=str(path),
        )
        lines = shadow_report.summary_lines(report)
        await self.context.notifier.notify(
            f"report:{day}", "info", "每日复盘", lines[0]
        )
        await self.context.notifier.send_imessage(f"report:{day}", "\n".join(lines))
        logger.info(
            "每日复盘报告已生成: %s（%d 笔影子单）", path, report["total_signals"]
        )
        return report

    async def _report_check_once(
        self, now, done_days: set, db_path: Optional[Path] = None
    ) -> bool:
        """调度单拍（可测）：ET 工作日且时间 ≥16:20 且当天未生成 → 生成。
        返回本拍是否生成。周末（weekday>=5）不跑。"""
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        day_key = now.strftime("%Y-%m-%d")
        if hm < 16 * 60 + 20 or day_key in done_days:
            return False
        await self.context.reports._generate_daily_report(day_key, db_path=db_path)
        done_days.add(day_key)
        return True

    async def _report_loop(self) -> None:
        """每日复盘调度（每 60s 检查；单拍见 _report_check_once）。"""
        from zoneinfo import ZoneInfo
        import datetime as _dt

        et = ZoneInfo("America/New_York")
        done_days: set = set()
        while True:
            await asyncio.sleep(60.0)
            try:
                await self.context.reports._report_check_once(
                    _dt.datetime.now(et), done_days
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("每日复盘调度异常: %s", e)

    def _report_meta_from_text(self, text: str) -> dict:
        """从报告文件头解析元信息（条目数/模型/degraded/耗时/tokens）。"""
        meta: dict = {"degraded": "⚠️ LLM 降级" in text}
        m = re.search("条目：大盘 (\\d+) 条 / 自选股 (\\d+) 票 (\\d+) 条", text)
        if m:
            meta["market_items"], meta["ticker_count"], meta["ticker_items"] = (
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
            )
        m = re.search("模型：(\\S+) · 耗时 ([\\d.]+)s · tokens (\\S+)", text)
        if m:
            meta["model"], meta["latency_s"], meta["tokens"] = (
                m.group(1),
                float(m.group(2)),
                m.group(3),
            )
        return meta

    def _report_file(self, date: str) -> Optional[Path]:
        """date（YYYYMMDD，正则硬校验防路径穿越）→ 报告文件路径或 None。"""
        if not self.context.reports._REPORT_DATE_RE.match(date):
            return None
        path = (self.context.reports._REPORTS_DIR / f"premarket-{date}.md").resolve()
        if self.context.reports._REPORTS_DIR.resolve() not in path.parents:
            return None
        return path if path.exists() else None

    async def api_reports_list(self) -> dict:
        """历史报告列表（日期倒序，含文件头元信息）。"""
        rows = []
        if self.context.reports._REPORTS_DIR.exists():
            for p in self.context.reports._REPORTS_DIR.glob("premarket-*.md"):
                date = p.stem.replace("premarket-", "")
                if not self.context.reports._REPORT_DATE_RE.match(date):
                    continue
                try:
                    head = p.read_text(encoding="utf-8")[:2000]
                except OSError:
                    continue
                rows.append(
                    {
                        "date": date,
                        "mtime": p.stat().st_mtime,
                        "meta": self.context.reports._report_meta_from_text(head),
                    }
                )
        rows.sort(key=lambda r: -r["mtime"])
        rows.sort(key=lambda r: r["date"], reverse=True)
        return {"reports": rows}

    async def api_reports_status(self) -> dict:
        """生成进度：running/started_at/elapsed_s/last_result/last_error。"""
        st = dict(self.context.reports._report_state)
        st["elapsed_s"] = (
            round(time.time() - st["started_at"], 1) if st["running"] else None
        )
        return st

    async def api_reports_get(self, date: str) -> dict:
        """该日报告 markdown 全文（date 格式校验，防路径穿越）。"""
        if not self.context.reports._REPORT_DATE_RE.match(date):
            raise HTTPException(400, "date 必须是 YYYYMMDD")
        path = self.context.reports._report_file(date)
        if path is None:
            raise HTTPException(404, f"无 {date} 的报告")
        text = path.read_text(encoding="utf-8")
        return {
            "date": date,
            "markdown": text,
            "meta": self.context.reports._report_meta_from_text(text[:2000]),
        }

    async def _report_generate_run(self, day: str, trader: str) -> None:
        """后台生成任务：采集+LLM 全部 to_thread，完成/失败更新 _report_state。"""
        try:
            tickers = self.context.news._news_tickers()
            ticker_set = set(tickers)

            def _work():
                market = news_digest.collect_market(ticker_set=ticker_set)
                ticker_news = news_digest.collect_tickers(tickers)
                from engine import youtube_digest

                window = (time.time() - 18 * 3600, time.time())
                videos = youtube_digest.process_videos(
                    self.context.reports._report_enabled_urls(),
                    window=window,
                    ticker_set=ticker_set,
                )
                md, meta = news_digest.generate_report(
                    day, market, ticker_news, videos=videos
                )
                path = news_digest.save_report(
                    day, md, market, ticker_news, meta, videos=videos
                )
                return (path, meta, market, ticker_news, videos)

            path, meta, market, ticker_news, videos = await asyncio.to_thread(_work)
            self.context.reports._report_apply_video_status(videos)
            self.context.reports._report_last_done = time.time()
            result = {
                "date": day.replace("-", ""),
                "path": str(path),
                "degraded": bool(meta.get("degraded")),
                "model": meta.get("model"),
                "tokens": (meta.get("usage") or {}).get("total_tokens"),
                "latency_s": meta.get("latency_s"),
                "market_items": len(market),
                "ticker_count": len(ticker_news),
                "finished_at": time.time(),
            }
            self.context.reports._report_state["last_result"] = result
            self.context.reports._report_state["last_error"] = None
            logger.warning(
                "盘前报告已生成（by %s）: %s（%s）",
                trader,
                path,
                "降级" if result["degraded"] else result["model"],
            )
            self.context.events.record_event(
                "news_report_generated", trader=trader, **result
            )
        except Exception as e:
            logger.exception("盘前报告生成失败")
            self.context.reports._report_state["last_error"] = str(e)
            self.context.events.record_event(
                "news_report_failed", trader=trader, error=str(e)
            )
        finally:
            self.context.reports._report_state["running"] = False

    async def api_reports_generate(self, trader: str = None) -> dict:
        """生成今日报告（异步长任务）：立即返回 running=true；在跑 → 409；
        10 分钟冷却内 → 429。"""
        if self.context.reports._report_state["running"]:
            raise HTTPException(409, "报告生成中，请等待完成")
        wait = self.context.reports.REPORT_GEN_COOLDOWN_S - (
            time.time() - self.context.reports._report_last_done
        )
        if wait > 0:
            raise HTTPException(429, f"生成冷却中，{int(wait)}s 后再试")
        day = _dt.datetime.now(self.context.news._ZI_ET).strftime("%Y-%m-%d")
        self.context.reports._report_state["running"] = True
        self.context.reports._report_state["started_at"] = time.time()
        self.context.reports._report_state["last_error"] = None
        self.context.reports._report_task = self.context.tasks.start(
            "reports._report_generate_run",
            lambda: self.context.reports._report_generate_run(day, trader),
        )
        return {"ok": True, "running": True, "day": day}

    def _load_report_sources(self) -> list:
        """加载视频源列表；缺文件/损坏返回 []。"""
        try:
            payload = json.loads(
                self.context.reports._REPORT_SOURCES_PATH.read_text(encoding="utf-8")
            )
            if isinstance(payload, list):
                return payload
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.warning("report_sources.json 加载失败: %s", e)
        return []

    def _save_report_sources(self, sources: list) -> None:
        """原子写落盘（tmp + os.replace）。"""
        try:
            self.context.reports._REPORT_SOURCES_PATH.parent.mkdir(
                parents=True, exist_ok=True
            )
            tmp = self.context.reports._REPORT_SOURCES_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.context.reports._REPORT_SOURCES_PATH)
        except OSError:
            logger.exception("report_sources.json 落盘失败")

    def _report_enabled_urls(self) -> list:
        return [
            s["url"]
            for s in self.context.reports._load_report_sources()
            if s.get("enabled")
        ]

    def _report_apply_video_status(self, videos: list) -> None:
        """生成报告后把各视频的处理状态回写进源列表（失败原因前端可见）。"""
        if not videos:
            return
        sources = self.context.reports._load_report_sources()
        by_id = {v.get("video_id"): v for v in videos if v.get("video_id")}
        dirty = False
        for s in sources:
            v = by_id.get(s.get("video_id"))
            if v is None:
                continue
            s["last_status"] = v.get("status")
            s["last_error"] = v.get("error") or ""
            if v.get("title"):
                s["title"] = v["title"]
            if v.get("channel"):
                s["channel"] = v["channel"]
            dirty = True
        if dirty:
            self.context.reports._save_report_sources(sources)

    async def api_report_sources(self) -> dict:
        """视频源列表。"""
        return {"sources": self.context.reports._load_report_sources()}

    async def _source_fill_meta(self, video_id: str) -> None:
        """后台补视频元数据（title/channel）；失败原因写进 last_error。"""
        from engine import youtube_digest

        sources = self.context.reports._load_report_sources()
        row = next((s for s in sources if s.get("video_id") == video_id), None)
        if row is None:
            return
        try:
            meta = await asyncio.to_thread(youtube_digest.fetch_metadata, row["url"])
            row["title"] = meta.get("title") or ""
            row["channel"] = meta.get("channel") or ""
            row["last_status"] = "meta_ok"
            row["last_error"] = ""
        except Exception as e:
            row["last_status"] = "meta_failed"
            row["last_error"] = str(e)
            logger.warning("视频源元数据抓取失败 %s: %s", video_id, e)
        self.context.reports._save_report_sources(sources)

    async def api_report_source_add(
        self, req: ReportSourceAddRequest, trader: str = None
    ) -> dict:
        """添加视频源（幂等：同 video_id 返回已有行）；异步补元数据。"""
        from engine import youtube_digest

        url = req.url.strip()
        video_id = youtube_digest.parse_video_id(url)
        if not video_id:
            raise HTTPException(
                400,
                "无法解析 YouTube video_id（支持 watch?v=/youtu.be//shorts/ 三种形态）",
            )
        sources = self.context.reports._load_report_sources()
        for s in sources:
            if s.get("video_id") == video_id:
                return {"ok": True, "source": s, "duplicated": True}
        row = {
            "url": url,
            "video_id": video_id,
            "title": "",
            "channel": "",
            "added_at": time.time(),
            "enabled": True,
            "last_status": "pending",
            "last_error": "",
        }
        sources.append(row)
        self.context.reports._save_report_sources(sources)
        self.context.events.record_event(
            "report_source_added", video_id=video_id, url=url, trader=trader
        )
        self.context.tasks.start(
            f"reports.source_meta:{video_id}",
            lambda: self.context.reports._source_fill_meta(video_id),
        )
        return {"ok": True, "source": row}

    async def api_report_source_toggle(self, video_id: str, trader: str = None) -> dict:
        """启用/停用翻转。"""
        sources = self.context.reports._load_report_sources()
        row = next((s for s in sources if s.get("video_id") == video_id), None)
        if row is None:
            raise HTTPException(404, "视频源不存在")
        row["enabled"] = not row.get("enabled", True)
        self.context.reports._save_report_sources(sources)
        return {"ok": True, "enabled": row["enabled"]}

    async def api_report_source_delete(self, video_id: str, trader: str = None) -> dict:
        """删除视频源。"""
        sources = self.context.reports._load_report_sources()
        new = [s for s in sources if s.get("video_id") != video_id]
        if len(new) == len(sources):
            raise HTTPException(404, "视频源不存在")
        self.context.reports._save_report_sources(new)
        self.context.events.record_event(
            "report_source_deleted", video_id=video_id, trader=trader
        )
        return {"ok": True}
