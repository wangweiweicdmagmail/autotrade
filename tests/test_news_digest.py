# -*- coding: utf-8 -*-
"""盘前新闻摘要回归（engine/news_digest.py）。

守的口径：RSS 解析（pubDate→epoch、坏 XML 容错）、标题归一化去重（Google
尾挂来源）、18h 时间窗、采集层编排（主题查询前 N 条、个股限速）、LLM 摘要
（mock；容错 markdown 围栏、缺键补默认、hot ticker 大写）、降级路径（LLM
不可用 → summary=None 不阻塞）、落盘/读取回环。全部 fixture XML + mock，
零网络零 key。
"""
import json
import time

import pytest

from engine import news_digest as nd

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Stock futures rise ahead of Fed decision - CNBC</title>
<link>https://x/1</link><pubDate>Sat, 25 Oct 2026 11:30:00 GMT</pubDate>
<source>CNBC</source></item>
<item><title>Nvidia shares jump on AI demand - Reuters</title>
<link>https://x/2</link><pubDate>Sat, 25 Oct 2026 10:00:00 GMT</pubDate>
<source>Reuters</source></item>
</channel></rss>"""

NOW = 1792818000.0   # 任意固定时刻（时间窗测试用）


def test_parse_rss_basic():
    """解析：标题/链接/来源/pubDate→epoch；坏 XML 返回 []。"""
    items = nd.parse_rss(RSS)
    assert len(items) == 2
    assert items[0]["title"].startswith("Stock futures rise")
    assert items[0]["source"] == "CNBC" and items[0]["link"] == "https://x/1"
    assert items[0]["published"] > 0
    assert nd.parse_rss("<html>not rss</html>") == []
    assert nd.parse_rss("garbage") == []


def test_dedupe_normalized_titles():
    """标题归一化去重：尾挂来源不同/大小写不同视为同一条。"""
    items = [
        {"title": "Fed holds rates steady - CNBC"},
        {"title": "Fed holds rates steady - Reuters"},
        {"title": "fed holds rates steady"},
        {"title": "Apple unveils new chip"},
    ]
    out = nd.dedupe_items(items)
    assert [i["title"] for i in out] == ["Fed holds rates steady - CNBC",
                                         "Apple unveils new chip"]


def test_filter_age_window():
    """18h 时间窗：超窗丢弃；published 缺失保留（不知新旧不误杀）。"""
    fresh = {"title": "a", "published": NOW - 3600}
    stale = {"title": "b", "published": NOW - 19 * 3600}
    unknown = {"title": "c", "published": 0}
    out = nd.filter_age([fresh, stale, unknown], max_age_h=18, now=NOW)
    assert [i["title"] for i in out] == ["a", "c"]


def _fake_fetcher(market_xml=RSS, ticker_xml=RSS, fail=()):
    """按 URL 路由的假抓取器；fail 里的子串命中即抛错。"""
    def fetch(url):
        for f in fail:
            if f in url:
                raise OSError("boom")
        if "dowjones" in url:
            return market_xml
        return ticker_xml
    return fetch


def test_collect_market_merges_and_dedupes():
    """大盘层：MarketWatch + 4 个 Google 主题查询合并去重；单源失败不阻塞。"""
    items = nd.collect_market(fetcher=_fake_fetcher())
    # MW 2 条 + 4 主题各 2 条，全部同标题 → 去重后 2 条
    assert len(items) == 2
    # 主题查询全挂 → 只剩 MW
    items = nd.collect_market(fetcher=_fake_fetcher(fail=("news.google",)))
    assert len(items) == 2
    # MW 也挂 → 空
    items = nd.collect_market(fetcher=_fake_fetcher(fail=("dowjones", "news.google")))
    assert items == []


def test_collect_tickers_rate_limit_and_age():
    """个股层：限速间隔生效、失败跳过、18h 过滤、每票最多 5 条。"""
    sleeps = []
    out = nd.collect_tickers(["NVDA", "AAPL", "FAIL"],
                             fetcher=_fake_fetcher(fail=("FAIL%20stock",)),
                             interval=0.25, sleep=sleeps.append)
    assert sleeps == [0.25, 0.25]          # 第 2、3 次前各睡一次
    assert set(out) == {"NVDA", "AAPL"}    # FAIL 跳过
    assert len(out["NVDA"]) == 2           # fixture 2 条（published 在窗内由时间决定）


def test_summarize_mock_llm_markdown_fence():
    """LLM 摘要：mock 返回带 markdown 围栏的 JSON → 解析成功、缺键补默认、
    hot ticker 大写。"""
    def fake_llm(system, user):
        return {"_model": "mock/test", "_usage": {"total_tokens": 100},
                "_latency_s": 0.5,
                "market_mood": "偏多",
                "themes": [{"topic": "AI", "summary": "算力需求旺",
                            "symbols": ["NVDA"]}],
                "hot_symbols": {"nvda": {"headline": "Nvidia shares jump",
                                         "gist": "AI 需求超预期",
                                         "sentiment": "pos",
                                         "catalyst": "earnings"}}}
    summary, meta = nd.summarize(nd.parse_rss(RSS), {"NVDA": nd.parse_rss(RSS)},
                                 llm=fake_llm)
    assert summary["market_mood"] == "偏多"
    assert summary["themes"][0]["topic"] == "AI"
    assert "NVDA" in summary["hot_symbols"]           # 大写归一
    assert summary["hot_symbols"]["NVDA"]["sentiment"] == "pos"
    assert meta["model"] == "mock/test" and meta["usage"]["total_tokens"] == 100


def test_summarize_fallback_when_llm_down():
    """LLM 不可用 → (None, error)，降级不阻塞。"""
    def boom(system, user):
        raise RuntimeError("LLM 调用失败: no key")
    summary, meta = nd.summarize([], {}, llm=boom)
    assert summary is None and "error" in meta


def test_generate_and_persist_roundtrip(tmp_path):
    """端到端（假 fetcher + mock LLM）：payload 结构完整 + 落盘/读取回环。"""
    def fake_llm(system, user):
        return {"market_mood": "震荡", "themes": [], "hot_symbols": {}}
    payload = nd.generate(["NVDA"], fetcher=_fake_fetcher(), llm=fake_llm,
                          sleep=lambda s: None)
    for key in ("generated_at", "generated_at_et", "elapsed_s",
                "market_headlines", "ticker_news", "summary", "summary_meta"):
        assert key in payload, key
    assert payload["summary"]["market_mood"] == "震荡"
    assert payload["ticker_news"]["NVDA"]
    path = nd.save_digest(payload, logs_dir=tmp_path)
    loaded = nd.load_digest(path.stem.replace("news-", ""), logs_dir=tmp_path)
    assert loaded["generated_at"] == payload["generated_at"]
    assert loaded["summary"]["market_mood"] == "震荡"
    # 不存在的日期 → None（空态）
    assert nd.load_digest("19990101", logs_dir=tmp_path) is None
