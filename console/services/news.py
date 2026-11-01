"""news application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
from typing import List
from typing import Optional
import datetime as _dt
import asyncio
import config
from engine import news_digest
import time
from console.shared import logger


class NewsService:
    def __init__(self, context):
        self.context = context

    def _news_tickers(self) -> List[str]:
        """watchlist 去重 ticker（剔除期货/CFD 前缀——个股新闻查股票）。"""
        return sorted(
            {
                s.split(":")[-1]
                for s in config.WATCHLIST
                if not s.startswith(("BLACKBULL:", "CAPITALCOM:"))
            }
        )

    def _news_today(self) -> Optional[dict]:
        """今日（ET）摘要：内存缓存优先，未命中懒加载落盘文件并缓存。"""
        day = _dt.datetime.now(self.context.news._ZI_ET).strftime("%Y%m%d")
        if day not in self.context.news._news_cache:
            self.context.news._news_cache[day] = news_digest.load_digest(day)
        return self.context.news._news_cache[day]

    def _news_status(self) -> dict:
        """盘前热点 + 新浪实时快讯标签状态（实时部分只读内存缓存）。"""
        live = self.context.news._live_news.status()
        p = self.context.news._news_today()
        if not p:
            return {
                "generated_at": None,
                "theme_count": 0,
                "hot_count": 0,
                "hot": {},
                **live,
            }
        summary = p.get("summary") or {}
        hot = {
            k: v.get("gist") or "" if isinstance(v, dict) else ""
            for k, v in (summary.get("hot_symbols") or {}).items()
        }
        themes = summary.get("themes") or []
        return {
            "generated_at": p.get("generated_at"),
            "generated_at_et": p.get("generated_at_et"),
            "theme_count": len(themes),
            "hot_count": len(hot),
            "hot": hot,
            "degraded": summary == {} and (not p.get("summary")),
            **live,
        }

    async def _news_generate(self, reason: str) -> Optional[dict]:
        """在线程池跑完整采集+摘要（阻塞 IO 不进事件循环），落盘并更新缓存。"""
        tickers = self.context.news._news_tickers()
        try:
            payload = await asyncio.to_thread(news_digest.generate, tickers)
            await asyncio.to_thread(news_digest.save_digest, payload)
        except Exception as e:
            logger.exception("盘前新闻生成失败（%s）: %s", reason, e)
            self.context.events.record_event(
                "news_digest_failed", reason=reason, error=str(e)
            )
            return None
        day = _dt.datetime.fromtimestamp(
            payload["generated_at"], self.context.news._ZI_ET
        ).strftime("%Y%m%d")
        self.context.news._news_cache[day] = payload
        summary = payload.get("summary") or {}
        meta = payload.get("summary_meta") or {}
        logger.warning(
            "盘前新闻已生成（%s）：主题 %d 热点 %d，LLM=%s，耗时 %ss",
            reason,
            len(summary.get("themes") or []),
            len(summary.get("hot_symbols") or {}),
            meta.get("model") or "降级",
            payload.get("elapsed_s"),
        )
        self.context.events.record_event(
            "news_digest",
            reason=reason,
            generated_at=payload["generated_at"],
            theme_count=len(summary.get("themes") or []),
            hot_count=len(summary.get("hot_symbols") or {}),
            hot=sorted((summary.get("hot_symbols") or {}).keys()),
            model=meta.get("model"),
            degraded=not payload.get("summary"),
            elapsed_s=payload.get("elapsed_s"),
        )
        return payload

    async def _news_loop(self) -> None:
        """盘前新闻自动生成（每 60s 检查）：07:30 ET 后当天无摘要 → 生成一次。"""
        while True:
            await asyncio.sleep(60.0)
            try:
                now = _dt.datetime.now(self.context.news._ZI_ET)
                if now.hour * 60 + now.minute < self.context.news.NEWS_AUTO_HM:
                    continue
                if self.context.news._news_today() is not None:
                    continue
                await self.context.news._news_generate("auto_0730")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("盘前新闻循环异常: %s", e)

    async def _news_startup(self) -> None:
        self.context.news._news_task = self.context.tasks.start(
            "news._news", lambda: self.context.news._news_loop()
        )
        await self.context.news._live_news.start()

    async def _news_shutdown(self) -> None:
        if self.context.news._news_task:
            self.context.news._news_task.cancel()
        await self.context.news._live_news.stop()

    async def api_live_news(self, ticker: str, minutes: int = 15) -> dict:
        """新浪 7×24 个股快讯，默认最近 15 分钟。"""
        if not ticker.strip():
            raise HTTPException(400, "ticker 不能为空")
        return self.context.news._live_news.news(ticker, minutes=minutes)

    async def api_news(self) -> dict:
        """今日盘前新闻摘要；无数据返回空态。"""
        payload = self.context.news._news_today()
        if not payload:
            return {"empty": True}
        return payload

    async def api_news_refresh(self, trader: str = None) -> dict:
        """手动刷新（冷却 10 分钟，全客户端共享）。"""
        wait = self.context.news.NEWS_REFRESH_COOLDOWN_S - (
            time.time() - self.context.news._news_last_manual
        )
        if wait > 0:
            raise HTTPException(429, f"刷新冷却中，{int(wait)}s 后再试")
        self.context.news._news_last_manual = time.time()
        payload = await self.context.news._news_generate(f"manual_by_{trader}")
        if payload is None:
            raise HTTPException(500, "生成失败（详见日志）")
        return {
            "ok": True,
            "generated_at": payload["generated_at"],
            "generated_at_et": payload["generated_at_et"],
        }
