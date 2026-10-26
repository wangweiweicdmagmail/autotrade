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
from datetime import datetime, timedelta
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
                  "Fed rate", "earnings", "stock futures")
MARKET_QUERY_TOP_N = 10          # 每个主题查询取前 10 条
TICKER_MAX_AGE_H = 18            # 个股新闻时间窗（小时）
TICKER_QUERY_INTERVAL = 0.25     # 个股查询限速（秒/次）
HTTP_TIMEOUT = 10

# 个股标题噪音过滤（2026-10-25 实测占个股条目约 40%）：期权链页、
# stock forecast 页、quote/chart 页（Moomoo/Yahoo 模板页，无新闻价值）
NOISE_TITLE_RE = re.compile(
    r"options? chain|stock forecasts?\b|stock price, news|"
    r"interactive stock chart|stock quote( price)?\b",
    re.IGNORECASE)

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
# 窗口 / 回填 / ticker 识别（报告层用）
# ---------------------------------------------------------------------------

def news_window(day: str) -> tuple:
    """目标交易日 D（YYYY-MM-DD，ET）的新闻窗口：D-1 16:00 ET ~ D 09:30 ET
    （盘后 + 盘前），返回 (start_epoch, end_epoch)。"""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET_TZ)
    end = int(d.replace(hour=9, minute=30).timestamp())
    start = int((d - timedelta(days=1)).replace(hour=16, minute=0).timestamp())
    return start, end


def backfill_suffix(after_day: str, before_day: str) -> str:
    """Google News 日期运算符后缀（2026-10-25 实测生效；before 含当天）。
    仍须配合 pubDate 窗口过滤兜精确边界。"""
    return f" after:{after_day} before:{before_day}"


def filter_window(items: List[dict], window: tuple) -> List[dict]:
    """窗口过滤：published 落在 [start, end] 内（published 缺失的丢弃——
    回填模式无法确认时间，宁缺毋滥）。"""
    start, end = window
    return [it for it in items
            if it.get("published") and start <= it["published"] <= end]


_TICKER_TOKEN_RE = re.compile(r"\$([A-Za-z]{1,6})\b|\b([A-Z]{2,5})\b")


def identify_tickers(title: str, ticker_set) -> List[str]:
    """从标题识别 watchlist ticker：$CASHTAG 或全大写词（2-5 字母）。
    标题里的普通英文词（Now/App/Be 等）是首字母大写，不匹配全大写规则，
    天然规避大部分误报。"""
    found = []
    for m in _TICKER_TOKEN_RE.finditer(title or ""):
        t = (m.group(1) or m.group(2) or "").upper()
        if t and t in ticker_set and t not in found:
            found.append(t)
    return found


# ---------------------------------------------------------------------------
# 采集层
# ---------------------------------------------------------------------------

def collect_market(fetcher: Callable[[str], str] = fetch_url,
                   window: Optional[tuple] = None,
                   include_mw: bool = True,
                   query_suffix: str = "",
                   ticker_set: Optional[set] = None) -> List[dict]:
    """大盘层：MarketWatch 头条 + Google News 主题查询（各前 N 条），
    合并去重（按标题）。window=(start,end) 时按 pubDate 窗口过滤（回填
    模式：include_mw=False——MarketWatch 没有历史）；query_suffix 追加
    Google 日期运算符；ticker_set 提供时每条附 tickers 识别结果。"""
    items = []
    if include_mw:
        try:
            items.extend(parse_rss(fetcher(MW_TOPSTORIES)))
        except Exception as e:
            logger.warning("MarketWatch RSS 抓取失败: %s", e)
    for q in MARKET_QUERIES:
        try:
            got = parse_rss(fetcher(
                GOOGLE_NEWS.format(q=urllib.parse.quote(q + query_suffix))))
            items.extend(got[:MARKET_QUERY_TOP_N])
        except Exception as e:
            logger.warning("Google News 主题 %r 抓取失败: %s", q, e)
    items = dedupe_items(items)
    if window is not None:
        items = filter_window(items, window)
    if ticker_set:
        for it in items:
            it["tickers"] = identify_tickers(it.get("title") or "", ticker_set)
    return items


def collect_tickers(tickers: List[str],
                    fetcher: Callable[[str], str] = fetch_url,
                    interval: float = TICKER_QUERY_INTERVAL,
                    max_age_h: float = TICKER_MAX_AGE_H,
                    sleep: Callable[[float], None] = time.sleep,
                    window: Optional[tuple] = None,
                    query_suffix: str = "",
                    stats: Optional[dict] = None) -> Dict[str, List[dict]]:
    """个股层：每个 ticker 一次 Google News 查询（限速），18h 时间窗过滤
    （window 提供时改为窗口过滤——回填模式）。每条附 tickers=[该 ticker]
    并合并标题识别结果。标题噪音（期权链/forecast/quote 模板页，
    NOISE_TITLE_RE）先剔除；stats 提供时记入 stats["filtered_noise"]。"""
    out: Dict[str, List[dict]] = {}
    ticker_set = set(tickers)
    for i, ticker in enumerate(tickers):
        if i:
            sleep(interval)
        try:
            items = parse_rss(fetcher(GOOGLE_NEWS.format(
                q=urllib.parse.quote(f"{ticker} stock" + query_suffix))))
        except Exception as e:
            logger.warning("Google News 个股 %s 抓取失败: %s", ticker, e)
            continue
        n0 = len(items)
        items = [it for it in items
                 if not NOISE_TITLE_RE.search(it.get("title") or "")]
        if stats is not None:
            stats["filtered_noise"] = stats.get("filtered_noise", 0) + n0 - len(items)
        items = (filter_window(items, window) if window is not None
                 else filter_age(items, max_age_h))
        if items:
            for it in items:
                it["tickers"] = sorted({ticker} | set(
                    identify_tickers(it.get("title") or "", ticker_set)))
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


# ---------------------------------------------------------------------------
# 报告层：盘前交易分析报告（中文 markdown，固定五章节 + 原始条目附录）
# ---------------------------------------------------------------------------

REPORTS_DIR = LOGS_DIR / "reports"

REPORT_SECTIONS = ("隔夜市场综述", "今日主要主题", "自选股催化清单",
                   "盘前异动股（非自选股热点）", "今日风险提示")

_REPORT_SYSTEM = """你是美股交易员助手，为日内交易员写盘前交易分析报告。
输出中文 markdown，严格按以下五个章节（标题用 ## 且措辞一致）：
## 一、隔夜市场综述
## 二、今日主要主题
## 三、自选股催化清单
## 四、盘前异动股（非自选股热点）
## 五、今日风险提示
要求：主题 3~6 条（每条注明涉及标的 ticker）；自选股催化清单逐票一行
「TICKER：一句话催化（利多/利空）」，宁缺毋滥；盘前异动股只收标题里
出现、且不在自选股名单的热点；风险提示写流动性/数据/事件雷。
只输出报告正文 markdown，不要任何额外说明。"""


def _fmt_items(market_items: List[dict], ticker_news: Dict[str, List[dict]],
               max_market: int = 30) -> str:
    lines = ["【大盘头条】（标题 | 来源 | 美东时间）"]
    for it in market_items[:max_market]:
        ts = datetime.fromtimestamp(it["published"], ET_TZ).strftime(
            "%m-%d %H:%M") if it.get("published") else "--"
        lines.append(f"- {it['title']} | {it.get('source') or ''} | {ts}")
    lines.append("\n【自选股个股新闻】")
    for ticker, items in sorted(ticker_news.items()):
        for it in items[:3]:
            lines.append(f"- [{ticker}] {it['title']}")
    return "\n".join(lines)


def render_fallback_report(day: str, market_items: List[dict],
                           ticker_news: Dict[str, List[dict]],
                           videos: Optional[List[dict]] = None) -> str:
    """降级报告（LLM 不可用）：固定章节 + 纯标题汇编，报告仍产出。
    有视频源时第六章节列视频标题+抓取状态（降级不做观点归纳）。"""
    lines = [
        "## 一、隔夜市场综述",
        "（LLM 不可用，降级为标题汇编）",
        "",
    ] + [f"- {it['title']}" for it in market_items[:20]] + [
        "",
        "## 二、今日主要主题",
        "（降级模式不归纳主题，请人工阅读第一节与附录）",
        "",
        "## 三、自选股催化清单",
    ]
    if ticker_news:
        for ticker, items in sorted(ticker_news.items()):
            lines.append(f"- **{ticker}**：{items[0]['title']}")
    else:
        lines.append("（窗口内无自选股新闻）")
    lines += [
        "",
        "## 四、盘前异动股（非自选股热点）",
        "（降级模式不可用）",
        "",
        "## 五、今日风险提示",
        "降级模式：请人工阅读附录原始条目，自行评估事件风险。",
    ]
    if videos:
        lines += ["", "## 六、盘前视频观点", "（降级模式：列标题与抓取状态，"
                  "观点归纳需 LLM）"]
        for v in videos:
            if v.get("status") == "ok":
                lines.append(f"- **{v.get('title')}**（{v.get('channel')}）"
                             f"：字幕 {v.get('transcript_words', '?')} 词")
            else:
                lines.append(f"- {v.get('title') or v.get('url')}："
                             f"⚠️ {v.get('error')}")
    return "\n".join(lines)


def generate_report(day: str, market_items: List[dict],
                    ticker_news: Dict[str, List[dict]],
                    llm_text: Callable = None,
                    videos: Optional[List[dict]] = None) -> tuple:
    """LLM 生成中文 markdown 盘前报告。返回 (markdown, meta)。
    videos（youtube_digest.process_videos 产出）提供时：摘要并入主题归纳
    输入，报告加第六章节「六、盘前视频观点」。
    LLM 不可用 → 降级标题汇编（meta 带 error + degraded=True）。"""
    if llm_text is None:
        from engine.agent_llm import call_llm_text
        llm_text = call_llm_text
    ok_videos = [v for v in (videos or []) if v.get("status") == "ok"]
    system = _REPORT_SYSTEM
    if videos:
        system += ("\n另有第六章节：## 六、盘前视频观点（每条：视频标题/频道/"
                   "摘要/涉及标的；抓取失败的视频列一行注明原因）。视频观点同时"
                   "参与「今日主要主题」的归纳。")
    user = (f"目标交易日：{day}（美东）。以下是盘前新闻条目：\n\n"
            + _fmt_items(market_items, ticker_news))
    if ok_videos:
        user += "\n\n【盘前视频观点】（标题 | 频道 | 摘要）"
        for v in ok_videos:
            user += (f"\n- {v.get('title')} | {v.get('channel')} | "
                     f"{v.get('summary')}")
    try:
        out = llm_text(system, user)
    except Exception as e:
        logger.warning("盘前报告 LLM 不可用，降级标题汇编: %s", e)
        return render_fallback_report(day, market_items, ticker_news,
                                      videos=videos), \
            {"error": str(e), "degraded": True}
    text = out.get("text") or ""
    meta = {"model": out.get("_model"), "usage": out.get("_usage", {}),
            "latency_s": out.get("_latency_s"), "degraded": False}
    # 章节完整性校验：缺章节则回退降级（防半吊子报告入档）；
    # 有视频源时第六章节也在校验范围内
    required = list(REPORT_SECTIONS)
    if videos:
        required.append("盘前视频观点")
    if not all(s in text for s in required):
        logger.warning("盘前报告章节不全，回退降级汇编")
        fb = render_fallback_report(day, market_items, ticker_news,
                                    videos=videos)
        return fb, {**meta, "degraded": True,
                    "error": "章节不全", "raw_text_head": text[:500]}
    return text, meta


def report_path(day: str, reports_dir: Path = REPORTS_DIR) -> Path:
    """day = ET 日期串（YYYY-MM-DD）→ logs/reports/premarket-YYYYMMDD.md。"""
    return reports_dir / f"premarket-{day.replace('-', '')}.md"


def save_report(day: str, markdown: str, market_items: List[dict],
                ticker_news: Dict[str, List[dict]], meta: dict,
                window: Optional[tuple] = None,
                videos: Optional[List[dict]] = None,
                reports_dir: Path = REPORTS_DIR) -> Path:
    """拼报告头（日期/窗口/条目统计/降级标注/生成元数据）+ 原始条目附录
    落盘（tmp+replace 原子写）。返回路径。"""
    head = [f"# 盘前交易分析报告 {day}", ""]
    if window:
        fmt = lambda ts: datetime.fromtimestamp(ts, ET_TZ).strftime("%m-%d %H:%M")
        head.append(f"新闻窗口：{fmt(window[0])} ~ {fmt(window[1])} ET")
    head.append(f"条目：大盘 {len(market_items)} 条 / "
                f"自选股 {len(ticker_news)} 票 "
                f"{sum(len(v) for v in ticker_news.values())} 条")
    if meta.get("degraded"):
        head.append(f"⚠️ LLM 降级（{meta.get('error', '')}）：纯标题汇编")
    else:
        head.append(f"模型：{meta.get('model')} · 耗时 "
                    f"{meta.get('latency_s')}s · tokens "
                    f"{(meta.get('usage') or {}).get('total_tokens', '?')}")
    head.append("\n---\n")
    appendix = ["\n---\n", "## 附录：原始条目", ""]
    for it in market_items:
        ts = datetime.fromtimestamp(it["published"], ET_TZ).strftime(
            "%m-%d %H:%M") if it.get("published") else "--"
        link = f"（{it['link']}）" if it.get("link") else ""
        tickers = f" [{'/'.join(it['tickers'])}]" if it.get("tickers") else ""
        appendix.append(f"- {ts} {it['title']}{tickers}{link}")
    for ticker, items in sorted(ticker_news.items()):
        for it in items:
            ts = datetime.fromtimestamp(it["published"], ET_TZ).strftime(
                "%m-%d %H:%M") if it.get("published") else "--"
            link = f"（{it['link']}）" if it.get("link") else ""
            appendix.append(f"- {ts} [{ticker}] {it['title']}{link}")
    if videos:
        appendix += ["", "### 视频源", ""]
        for v in videos:
            ts = datetime.fromtimestamp(v["published"], ET_TZ).strftime(
                "%m-%d %H:%M") if v.get("published") else "--"
            status = "" if v.get("status") == "ok" \
                else f" ⚠️{v.get('status')}: {v.get('error')}"
            appendix.append(f"- {ts} {v.get('title') or v.get('url')}"
                            f"（{v.get('channel') or ''}）{status} "
                            f"{v.get('url')}")
    full = "\n".join(head) + "\n" + markdown.strip() + "\n" + "\n".join(appendix) + "\n"
    path = report_path(day, reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(full, encoding="utf-8")
    tmp.replace(path)
    return path
