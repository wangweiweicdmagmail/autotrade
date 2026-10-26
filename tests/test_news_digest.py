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
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

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


def _rss_recent(hours=(1.0, 2.5)):
    """pubDate 相对当前时间的个股 RSS（18h 时间窗用——RSS 固期 10-25
    一旦距今超窗个股层就采空，测试必须跟墙钟走）。"""
    out = RSS
    for fixed, h in zip(("Sat, 25 Oct 2026 11:30:00 GMT",
                         "Sat, 25 Oct 2026 10:00:00 GMT"), hours):
        ts = datetime.now(timezone.utc) - timedelta(hours=h)
        out = out.replace(fixed, format_datetime(ts))
    return out

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
                             fetcher=_fake_fetcher(ticker_xml=_rss_recent(),
                                                   fail=("FAIL%20stock",)),
                             interval=0.25, sleep=sleeps.append)
    assert sleeps == [0.25, 0.25]          # 第 2、3 次前各睡一次
    assert set(out) == {"NVDA", "AAPL"}    # FAIL 跳过
    assert len(out["NVDA"]) == 2           # 相对时间 fixture 必在 18h 窗内


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
    payload = nd.generate(["NVDA"],
                          fetcher=_fake_fetcher(ticker_xml=_rss_recent()),
                          llm=fake_llm, sleep=lambda s: None)
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


# ----------------------------------------------------------------------
# 报告层（盘前交易分析报告 v1：窗口/回填/ticker 识别/markdown 报告）
# ----------------------------------------------------------------------

def test_news_window_definition():
    """窗口：D-1 16:00 ET ~ D 09:30 ET（盘后+盘前）。"""
    from datetime import datetime as dt
    start, end = nd.news_window("2026-10-23")
    s = dt.fromtimestamp(start, nd.ET_TZ)
    e = dt.fromtimestamp(end, nd.ET_TZ)
    assert (s.month, s.day, s.hour, s.minute) == (10, 22, 16, 0)
    assert (e.month, e.day, e.hour, e.minute) == (10, 23, 9, 30)


def test_backfill_suffix():
    """Google News 日期运算符后缀格式。"""
    assert nd.backfill_suffix("2026-10-22", "2026-10-23") == \
        " after:2026-10-22 before:2026-10-23"


def test_filter_window():
    """窗口过滤：落在 [start,end] 外丢弃；published 缺失丢弃（回填宁缺毋滥）。"""
    win = (1000, 2000)
    items = [{"title": "in", "published": 1500},
             {"title": "before", "published": 999},
             {"title": "after", "published": 2001},
             {"title": "unknown", "published": 0}]
    assert [i["title"] for i in nd.filter_window(items, win)] == ["in"]


def test_identify_tickers():
    """ticker 识别：$CASHTAG 与全大写词命中；首字母大写普通词不误判。"""
    ts = {"NVDA", "TSLA", "NOW", "APP"}
    assert nd.identify_tickers("TSLA surges on deliveries", ts) == ["TSLA"]
    assert nd.identify_tickers("$nvda and $TSLA rally", ts) == ["NVDA", "TSLA"]
    # 「Now」「App」首字母大写普通词不匹配全大写规则（防误报）
    assert nd.identify_tickers("Now is the time for App makers", ts) == []
    assert nd.identify_tickers("", ts) == []


def test_collect_market_backfill_mode():
    """回填模式：跳过 MarketWatch、查询带日期运算符、窗口过滤生效、
    条目附 tickers。"""
    seen_urls = []
    old = RSS.replace("Sat, 25 Oct 2026 11:30:00 GMT",
                      "Wed, 22 Oct 2026 17:00:00 GMT")   # 窗内（10-22 16:00 后）
    new_in = old.replace("Sat, 25 Oct 2026 10:00:00 GMT",
                         "Fri, 23 Oct 2026 12:00:00 GMT")  # 窗外（09:30 后）

    def fetch(url):
        seen_urls.append(url)
        assert "dowjones" not in url, "回填模式不应抓 MarketWatch"
        assert "after%3A2026-10-22" in url and "before%3A2026-10-23" in url
        return new_in

    win = nd.news_window("2026-10-23")
    items = nd.collect_market(fetcher=fetch, window=win, include_mw=False,
                              query_suffix=nd.backfill_suffix("2026-10-22",
                                                              "2026-10-23"),
                              ticker_set={"NVDA"})
    # 每条查询返回 2 条：一条窗内（10-22 17:00）一条窗外（10-23 12:00）
    assert len(items) == 1
    assert "tickers" in items[0]
    assert len(seen_urls) == len(nd.MARKET_QUERIES)


def test_collect_tickers_window_mode():
    """个股回填：窗口过滤 + 条目不落 tickers 字段缺失。"""
    win = nd.news_window("2026-10-23")
    out = nd.collect_tickers(["NVDA"], fetcher=_fake_fetcher(),
                             sleep=lambda s: None, window=win,
                             query_suffix=" after:2026-10-22 before:2026-10-23")
    # fixture 的 pubDate 是 10-25，落在 10-23 窗口外 → 空
    assert out == {}


def test_generate_report_mock_llm():
    """报告层：mock LLM 返回五章节 markdown → 原样产出 + meta。"""
    md = ("## 一、隔夜市场综述\n多。\n## 二、今日主要主题\nAI。\n"
          "## 三、自选股催化清单\nNVDA：需求超预期（利多）。\n"
          "## 四、盘前异动股（非自选股热点）\n无。\n## 五、今日风险提示\n无。")

    def fake_llm(system, user):
        return {"text": md, "_model": "mock/test",
                "_usage": {"total_tokens": 800}, "_latency_s": 1.2}

    text, meta = nd.generate_report("2026-10-23", nd.parse_rss(RSS), {},
                                    llm_text=fake_llm)
    assert text == md and meta["degraded"] is False
    assert meta["model"] == "mock/test"


def test_generate_report_fallback_when_llm_down():
    """LLM 不可用 → 降级标题汇编：五章节齐全 + 标题进正文。"""
    def boom(system, user):
        raise RuntimeError("LLM 文本调用失败: no key")
    text, meta = nd.generate_report("2026-10-23", nd.parse_rss(RSS),
                                    {"NVDA": nd.parse_rss(RSS)}, llm_text=boom)
    assert meta["degraded"] is True and "error" in meta
    for s in nd.REPORT_SECTIONS:
        assert s in text, s
    assert "Stock futures rise" in text          # 大盘标题进第一节
    assert "**NVDA**" in text                    # 自选股进催化清单


def test_generate_report_incomplete_sections_fallback():
    """LLM 输出章节不全 → 回退降级（防半吊子报告入档）。"""
    def partial(system, user):
        return {"text": "## 一、隔夜市场综述\n只有一节", "_model": "m"}
    text, meta = nd.generate_report("2026-10-23", nd.parse_rss(RSS), {},
                                    llm_text=partial)
    assert meta["degraded"] is True
    for s in nd.REPORT_SECTIONS:
        assert s in text


def test_save_report_structure(tmp_path):
    """落盘：文件名 premarket-YYYYMMDD.md + 报告头（窗口/统计/降级标注）
    + 附录带链接与 ticker 标注。"""
    items = nd.parse_rss(RSS)
    items[0]["tickers"] = ["NVDA"]
    md = nd.render_fallback_report("2026-10-23", items, {})
    path = nd.save_report("2026-10-23", md, items, {"NVDA": items},
                          {"degraded": True, "error": "no key"},
                          window=nd.news_window("2026-10-23"),
                          reports_dir=tmp_path)
    assert path.name == "premarket-20261023.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 盘前交易分析报告 2026-10-23")
    assert "新闻窗口：10-22 16:00 ~ 10-23 09:30 ET" in text
    assert "LLM 降级" in text
    assert "## 附录：原始条目" in text
    assert "[NVDA]" in text and "https://x/1" in text


def test_noise_filter_titles():
    """标题噪音过滤：期权链/forecast/quote/chart 模板页剔除，正常新闻保留，
    过滤数记进 stats["filtered_noise"]。"""
    noisy = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>CRWD 261023 315.00P (CRWD261023P315000) Stock Options Chain | Quotes &amp; News - Moomoo</title><link>https://x/1</link></item>
<item><title>Arm Holdings plc (ARM) Stock Forecasts - Yahoo Finance</title><link>https://x/2</link></item>
<item><title>PANW Jun 2028 35.000 call Stock Price, News, Quote &amp; History - Yahoo</title><link>https://x/3</link></item>
<item><title>XLU Nov 2026 put Interactive Stock Chart - Yahoo Finance</title><link>https://x/4</link></item>
<item><title>MRVL Stock Quote Price and Forecast - CNN</title><link>https://x/5</link></item>
<item><title>Why Micron Stock Dropped on Thursday - Yahoo Finance</title><link>https://x/6</link></item>
<item><title>MU leads memory stock rebound candidates - Investing.com</title><link>https://x/7</link></item>
</channel></rss>"""
    stats = {}
    out = nd.collect_tickers(["MU"], fetcher=lambda url: noisy,
                             sleep=lambda s: None, stats=stats)
    titles = [it["title"] for it in out["MU"]]
    assert titles == ["Why Micron Stock Dropped on Thursday - Yahoo Finance",
                      "MU leads memory stock rebound candidates - Investing.com"], titles
    assert stats["filtered_noise"] == 5


def test_noise_filter_case_insensitive():
    """大小写不敏感 + 无噪音时 stats 计数为 0。"""
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>aapl stock options chain</title><link>https://x/1</link></item>
<item><title>Apple hits record high on strong sales</title><link>https://x/2</link></item>
</channel></rss>"""
    stats = {}
    out = nd.collect_tickers(["AAPL"], fetcher=lambda url: xml,
                             sleep=lambda s: None, stats=stats)
    assert [it["title"] for it in out["AAPL"]] == \
        ["Apple hits record high on strong sales"]
    assert stats["filtered_noise"] == 1
    # 全部正常 → 0
    stats2 = {}
    nd.collect_tickers(["AAPL"], fetcher=lambda url: RSS,
                       sleep=lambda s: None, stats=stats2)
    assert stats2["filtered_noise"] == 0
