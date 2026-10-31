# -*- coding: utf-8 -*-
"""新浪 7×24 实时快讯：共享轮询缓存与美股 ticker 关联。"""
import asyncio
import json
import logging
import time
import urllib.request
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SINA_HOME = "https://finance.sina.com.cn/7x24/"
SINA_FEED = ("https://zhibo.sina.com.cn/api/zhibo/feed"
             "?zhibo_id=152&page_size={page_size}&dire=f")
SINA_SOURCE = {"name": "新浪财经", "url": SINA_HOME}
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def fetch_url(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": SINA_HOME})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _extension(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _published(value: str) -> float:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def parse_sina_feed(payload: dict) -> list:
    rows = (((payload.get("result") or {}).get("data") or {})
            .get("feed") or {}).get("list") or []
    items = []
    for row in rows:
        title = (row.get("rich_text") or "").strip()
        if not title:
            continue
        ext = _extension(row.get("ext"))
        tickers = sorted({str(s.get("symbol") or "").upper()
                          for s in (ext.get("stocks") or [])
                          if isinstance(s, dict) and s.get("market") == "us"
                          and s.get("symbol")})
        items.append({
            "id": str(row.get("id") or ext.get("docid") or ""),
            "title": title,
            "published": _published(row.get("create_time")),
            "link": row.get("docurl") or ext.get("docurl") or SINA_HOME,
            "source": "新浪财经",
            "tickers": tickers,
        })
    return items


def fetch_sina_feed(page_size: int = 50,
                    fetcher: Callable[[str], str] = fetch_url) -> list:
    text = fetcher(SINA_FEED.format(page_size=page_size))
    return parse_sina_feed(json.loads(text))


class LiveNewsService:
    def __init__(self, fetcher: Callable[[], list] = fetch_sina_feed,
                 refresh_seconds: float = 15.0,
                 window_seconds: float = 15 * 60.0):
        self._fetcher = fetcher
        self.refresh_seconds = refresh_seconds
        self.window_seconds = window_seconds
        self._items = []
        self.updated_at = 0.0
        self.error = ""
        self._refresh_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    async def refresh(self) -> bool:
        async with self._refresh_lock:
            try:
                items = await asyncio.to_thread(self._fetcher)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = str(e)
                logger.warning("新浪实时新闻抓取失败: %s", e)
                return False
            self._items = items
            self.updated_at = time.time()
            self.error = ""
            return True

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            await self.refresh()

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def status(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        tickers = sorted({ticker for item in self._items
                          if item.get("published", 0) >= cutoff
                          for ticker in item.get("tickers") or []})
        return {"live_tickers": tickers,
                "live_updated_at": self.updated_at or None,
                "live_error": self.error}

    def news(self, ticker: str, minutes: int = 15,
             now: Optional[float] = None) -> dict:
        ticker = ticker.strip().upper()
        now = time.time() if now is None else now
        cutoff = now - max(1, min(int(minutes), 60)) * 60
        items = [item for item in self._items
                 if ticker in (item.get("tickers") or [])
                 and item.get("published", 0) >= cutoff]
        items.sort(key=lambda item: item.get("published") or 0, reverse=True)
        return {"ticker": ticker, "minutes": minutes,
                "items": items, "count": len(items),
                "updated_at": self.updated_at or None,
                "error": self.error, "source": SINA_SOURCE}
