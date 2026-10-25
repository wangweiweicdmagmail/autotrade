# -*- coding: utf-8 -*-
"""
盘前热点新闻统计 (news_digest.py)
==================================

盘前新闻摘要：RSS 采集（无 key）+ LLM 主题归纳 + 自选股热点识别。

数据源（2026-10-25 实测可用）：
- MarketWatch 头条 RSS：feeds.content.dowjones.io/public/rss/mw_topstories
- Google News RSS：news.google.com/rss/search?q=...&hl=en-US&gl=US&ceid=US:en
- ⚠️ Yahoo 个股 RSS 已失效（返回 HTML），不要用。

分层：
1. 大盘层：MarketWatch top stories + Google News 主题查询（premarket
   movers / stock market today / Fed rate / earnings，各取前 10 条）。
2. 个股层：watchlist 每个 ticker 一次 Google News 查询（0.25s 限速），
   只保留 18 小时内的条目。
3. 摘要层：原始条目喂 engine/agent_llm（JSON 约束），产出
   {market_mood, themes, hot_symbols}；LLM 不可用 → 降级纯标题列表，
   不阻塞（summary=None）。

持久化：logs/news-YYYYMMDD.json（原始条目 + 摘要 + 生成时间，ET 日期）。

纯逻辑 + 两个 IO 边界（fetcher / llm 可注入，测试零网络零 key）。
"""

import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
ET_TZ = ZoneInfo("America/New_York")

MW_TOPSTORIES = "https://feeds.content.dowjones.io/public/rss/mw_topstories"
GOOGLE_NEWS = ("https://news.google.com/rss/search?q={q}"
               "&hl=en-US&gl=US&ceid=US:en")

MARKET_QUERIES = ("premarket movers", "stock market today",
                  "Fed rate", "earnings")
MARKET_QUERY_TOP_N = 10          # 每个主题查询取前 10 条
TICKER_MAX_AGE_H = 18            # 个股新闻时间窗（小时）
TICKER_QUERY_INTERVAL = 0.25     # 个股查询限速（秒/次）
HTTP_TIMEOUT = 10

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ---------------------------------------------------------------------------
# IO 边界 1：HTTP 抓取
# ---------------------------------------------------------------------------

def fetch_url(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    """GET 文本（带 UA，Google/DowJones 都验 UA）。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 纯逻辑：解析 / 归一化去重 / 时间窗
# ---------------------------------------------------------------------------

def parse_rss(xml_text: str) -> List[dict]:
    """RSS → [{title, link, source, published(epoch)}]；坏 XML 返回 []。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("RSS 解析失败: %s", e)
        return []
    out = []
    for item in root.iter("item"):
        def _text(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        title = _text("title")
        if not title:
            continue
        published = 0.0
        pub = _text("pubDate")
        if pub:
            try:
                published = parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                published = 0.0
        out.append({"title": title, "link": _text("link"),
                    "source": _text("source"), "published": published})
    return out


def normalize_title(title: str) -> str:
    """标题归一化（去重用）：去尾挂「 - 来源」（Google 格式）、小写、
    只留字母数字。"""
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", title)   # Google 尾挂来源
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def dedupe_items(items: List[dict]) -> List[dict]:
    """按归一化标题去重（保留先到的）。"""
    seen = set()
    out = []
    for it in items:
        key = normalize_title(it.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def filter_age(items: List[dict], max_age_h: float = TICKER_MAX_AGE_H,
               now: Optional[float] = None) -> List[dict]:
    """时间窗过滤：只保留 max_age_h 小时内的（published 缺失的保留——
    不知新旧不误杀）。"""
    cutoff = (now if now is not None else time.time()) - max_age_h * 3600
    return [it for it in items
            if not it.get("published") or it["published"] >= cutoff]


# ---------------------------------------------------------------------------
# 采集层
# ---------------------------------------------------------------------------

def collect_market(fetcher: Callable[[str], str] = fetch_url) -> List[dict]:
    """大盘层：MarketWatch 头条 + Google News 主题查询（各前 N 条），
    合并去重（按标题）。"""
    items = []
    try:
        items.extend(parse_rss(fetcher(MW_TOPSTORIES)))
    except Exception as e:
        logger.warning("MarketWatch RSS 抓取失败: %s", e)
    for q in MARKET_QUERIES:
        try:
            got = parse_rss(fetcher(GOOGLE_NEWS.format(q=urllib.parse.quote(q))))
            items.extend(got[:MARKET_QUERY_TOP_N])
        except Exception as e:
            logger.warning("Google News 主题 %r 抓取失败: %s", q, e)
    return dedupe_items(items)


def collect_tickers(tickers: List[str],
                    fetcher: Callable[[str], str] = fetch_url,
                    interval: float = TICKER_QUERY_INTERVAL,
                    max_age_h: float = TICKER_MAX_AGE_H,
                    sleep: Callable[[float], None] = time.sleep
                    ) -> Dict[str, List[dict]]:
    """个股层：每个 ticker 一次 Google News 查询（限速），18h 时间窗过滤。"""
    out: Dict[str, List[dict]] = {}
    for i, ticker in enumerate(tickers):
        if i:
            sleep(interval)
        try:
            items = parse_rss(fetcher(
                GOOGLE_NEWS.format(q=urllib.parse.quote(f"{ticker} stock"))))
        except Exception as e:
            logger.warning("Google News 个股 %s 抓取失败: %s", ticker, e)
            continue
        items = filter_age(items, max_age_h)
        if items:
            out[ticker] = items[:5]   # 每票最多留 5 条，控制摘要输入体量
    return out


# ---------------------------------------------------------------------------
# 摘要层（IO 边界 2：LLM，可注入 mock）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """你是美股盘前新闻分析员。输入是今日盘前大盘头条与自选股个股新闻标题。
只输出一个 JSON 对象，结构严格如下：
{
 "market_mood": "一句话大盘情绪（中文，含偏多/偏空/震荡判断）",
 "themes": [{"topic": "主题名（中文）", "summary": "一句话", "symbols": ["相关ticker"]}],
 "hot_symbols": {"TICKER": {"headline": "最关键标题原文", "gist": "一句话催化（中文）",
                 "sentiment": "pos|neg|neu", "catalyst": "earnings|guidance|analyst|macro|deal|product|other"}}
}
rules: themes 最多 5 个；hot_symbols 只收有实质催化的自选股 ticker（大写），
宁缺毋滥；没有热点时 hot_symbols 给 {}。不要输出任何 JSON 以外的文本。"""


def _build_user_prompt(market_items: List[dict],
                       ticker_news: Dict[str, List[dict]]) -> str:
    lines = ["【大盘头条】"]
    for it in market_items[:15]:
        lines.append("- " + it["title"])
    lines.append("\n【自选股个股新闻】")
    for ticker, items in sorted(ticker_news.items()):
        for it in items[:3]:
            lines.append(f"- [{ticker}] {it['title']}")
    return "\n".join(lines)


def summarize(market_items: List[dict], ticker_news: Dict[str, List[dict]],
              llm: Callable = None) -> tuple:
    """LLM 摘要。返回 (summary|None, meta)。
    LLM 不可用/输出非法 → (None, {"error": ...})，降级不阻塞。
    summary 结构校验：缺键补默认（market_mood ""、themes []、hot_symbols {}）。"""
    if llm is None:
        from engine.agent_llm import call_llm
        llm = call_llm
    try:
        out = llm(_SYSTEM_PROMPT, _build_user_prompt(market_items, ticker_news))
    except Exception as e:
        logger.warning("新闻摘要 LLM 不可用，降级纯标题列表: %s", e)
        return None, {"error": str(e)}
    meta = {"model": out.pop("_model", None), "usage": out.pop("_usage", {}),
            "latency_s": out.pop("_latency_s", None)}
    out.pop("_reasoning", None)
    if not isinstance(out, dict):
        return None, {**meta, "error": "输出非 JSON 对象"}
    summary = {
        "market_mood": str(out.get("market_mood") or ""),
        "themes": [t for t in (out.get("themes") or []) if isinstance(t, dict)],
        "hot_symbols": {k.upper(): v for k, v in (out.get("hot_symbols") or {}).items()
                        if isinstance(v, dict)},
    }
    return summary, meta


# ---------------------------------------------------------------------------
# 编排 + 持久化
# ---------------------------------------------------------------------------

def generate(tickers: List[str], fetcher: Callable = fetch_url,
             llm: Callable = None, sleep: Callable = time.sleep) -> dict:
    """完整生成一轮盘前新闻摘要（采集 → 摘要 → payload）。"""
    t0 = time.time()
    market_items = collect_market(fetcher)
    ticker_news = collect_tickers(tickers, fetcher=fetcher, sleep=sleep)
    summary, meta = summarize(market_items, ticker_news, llm=llm)
    now = time.time()
    return {
        "generated_at": now,
        "generated_at_et": datetime.fromtimestamp(now, ET_TZ)
        .strftime("%Y-%m-%d %H:%M"),
        "elapsed_s": round(time.time() - t0, 1),
        "market_headlines": market_items,
        "ticker_news": ticker_news,
        "summary": summary,          # None = LLM 降级（纯标题列表）
        "summary_meta": meta,
    }


def digest_path(day: str, logs_dir: Path = LOGS_DIR) -> Path:
    """day = ET 日期串（YYYY-MM-DD）。"""
    return logs_dir / f"news-{day.replace('-', '')}.json"


def save_digest(payload: dict, logs_dir: Path = LOGS_DIR) -> Path:
    """按生成时刻的 ET 日期落盘（tmp + replace 原子写）。"""
    day = datetime.fromtimestamp(payload["generated_at"], ET_TZ).strftime("%Y%m%d")
    path = logs_dir / f"news-{day}.json"
    logs_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def load_digest(day: Optional[str] = None,
                logs_dir: Path = LOGS_DIR) -> Optional[dict]:
    """读指定 ET 日（缺省=今天）的摘要；不存在/损坏返回 None。"""
    day = day or datetime.now(ET_TZ).strftime("%Y%m%d")
    path = logs_dir / f"news-{day}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("新闻摘要加载失败: %s", e)
        return None
