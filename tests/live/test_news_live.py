# -*- coding: utf-8 -*-
"""LIVE：盘前新闻真网采集冒烟（真实 Google News / MarketWatch RSS）。

守的口径：真网 collect_market 能拿到非空大盘头条；两个 watchlist ticker
真查询有结果结构。需网络，默认套件不跑：
python3 -m pytest tests/live/ -m live
"""
import pytest

pytestmark = pytest.mark.live

from engine import news_digest as nd


def test_real_market_fetch():
    items = nd.collect_market()
    assert len(items) >= 3, "大盘头条为空（数据源可能失效）"
    assert all(i["title"] for i in items)


def test_real_ticker_fetch():
    out = nd.collect_tickers(["NVDA", "AAPL"], interval=0.25)
    assert out, "个股新闻全空"
    for ticker, items in out.items():
        assert all(i.get("published", 0) >= 0 for i in items)
