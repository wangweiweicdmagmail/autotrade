# -*- coding: utf-8 -*-
"""新浪 7×24 实时快讯解析、15 分钟窗口、故障缓存与 API 回归。"""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.live_news import LiveNewsService, fetch_sina_feed, parse_sina_feed
from .conftest import run


def _payload():
    return {"result": {"data": {"feed": {"list": [
        {"id": 101, "rich_text": "微软发行债券", "create_time": "2026-10-30 00:21:01",
         "docurl": "https://finance.sina.cn/7x24/msft.d.html",
         "ext": json.dumps({"stocks": [
             {"market": "us", "symbol": "msft"},
             {"market": "hk", "symbol": "04338"}]})},
        {"id": 102, "rich_text": "英伟达回应传闻", "create_time": "2026-10-30 00:02:17",
         "ext": json.dumps({"stocks": [{"market": "us", "symbol": "nvda"}],
                            "docurl": "https://finance.sina.com.cn/7x24/nvda.shtml"})},
        {"id": 103, "rich_text": "欧洲股指上涨", "create_time": "2026-10-30 00:30:55",
         "ext": "not-json"},
        {"id": 104, "rich_text": "", "create_time": "2026-10-30 00:30:55"},
    ]}}}}


def test_parse_sina_payload_and_structured_us_tickers():
    items = parse_sina_feed(_payload())
    assert len(items) == 3
    msft, nvda, other = items
    assert msft["tickers"] == ["MSFT"]
    assert msft["link"].endswith("msft.d.html")
    expected = datetime(2026, 10, 30, 0, 21, 1,
                        tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert msft["published"] == expected
    assert nvda["tickers"] == ["NVDA"]
    assert nvda["link"].endswith("nvda.shtml")
    assert other["tickers"] == []
    assert other["link"].startswith("https://finance.sina.com.cn/7x24/")


def test_fetch_sina_feed_json():
    text = json.dumps(_payload())
    items = fetch_sina_feed(fetcher=lambda url: text)
    assert [item["id"] for item in items] == ["101", "102", "103"]


def test_service_window_empty_state_and_failed_refresh_cache():
    now = time.time()
    rows = [
        {"id": "new", "title": "new", "published": now - 60,
         "link": "https://x/new", "source": "新浪财经", "tickers": ["MSFT"]},
        {"id": "old", "title": "old", "published": now - 901,
         "link": "https://x/old", "source": "新浪财经", "tickers": ["MSFT"]},
        {"id": "other", "title": "other", "published": now - 30,
         "link": "https://x/other", "source": "新浪财经", "tickers": ["NVDA"]},
    ]
    service = LiveNewsService(fetcher=lambda: rows)

    async def go():
        assert await service.refresh() is True
        payload = service.news("msft", now=now)
        assert payload["count"] == 1
        assert payload["items"][0]["id"] == "new"
        assert payload["source"]["name"] == "新浪财经"
        assert service.news("AAPL", now=now)["items"] == []
        assert service.status(now)["live_tickers"] == ["MSFT", "NVDA"]

        def fail():
            raise RuntimeError("network down")
        service._fetcher = fail
        previous_update = service.updated_at
        assert await service.refresh() is False
        assert service.updated_at == previous_update
        assert service.news("MSFT", now=now)["count"] == 1
        assert "network down" in service.news("MSFT", now=now)["error"]

        later = now + 1_000
        assert service.news("MSFT", now=later)["items"] == []
        assert service.status(later)["live_tickers"] == []
    run(go())


def test_live_news_api_and_snapshot_status(console_app, monkeypatch):
    app = console_app

    class FakeService:
        def status(self):
            return {"live_tickers": ["MSFT"], "live_updated_at": 123.0,
                    "live_error": ""}

        def news(self, ticker, minutes=15):
            assert ticker == "MSFT" and minutes == 15
            return {"ticker": ticker, "minutes": minutes, "items": [], "count": 0}

    monkeypatch.setattr(app.news, "_live_news", FakeService())
    monkeypatch.setattr(app.news, "_news_today", lambda: None)
    status = app.news._news_status()
    assert status["live_tickers"] == ["MSFT"]
    assert status["live_updated_at"] == 123.0
    payload = run(app.news.api_live_news("MSFT"))
    assert payload["ticker"] == "MSFT" and payload["count"] == 0


@pytest.mark.parametrize("minutes", [0, 120])
def test_news_window_is_bounded(minutes):
    service = LiveNewsService(fetcher=list)
    payload = service.news("MSFT", minutes=minutes, now=1_000.0)
    assert payload["minutes"] == minutes
    assert payload["items"] == []
