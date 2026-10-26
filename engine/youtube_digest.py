# -*- coding: utf-8 -*-
"""
YouTube 视频字幕 → 盘前报告内容源 (youtube_digest.py)
======================================================

用户贴 YouTube 链接，把视频字幕抓取 + LLM 摘要后并入盘前交易分析报告
（第六章节「盘前视频观点」+ 参与主题归纳）。

- 链接解析：watch?v= / youtu.be/ / shorts/ 三种形态 → video_id。
- 字幕抓取：subprocess 调本机 yt-dlp（/opt/homebrew/bin/yt-dlp，已实测），
  零新 Python 依赖。先 `-j` 拿元数据（title/upload_date/timestamp/
  duration/channel）；再 --write-auto-subs --write-subs --sub-langs en.*
  --skip-download --sub-format vtt 抓字幕（人工优先，自动生成也行）到
  临时目录；VTT→纯文本（去时间戳、连续重复行合并）。
- 窗口过滤：优先用元数据 timestamp（精确），缺失退回 upload_date（日级
  精度，按窗口覆盖的 ET 日期集合判定）；窗口外跳过并注明。
- LLM 摘要：字幕截断 ~6000 词 → agent_llm.call_llm_text 生成中文摘要
  （~200 字：核心观点+标的/板块+多空倾向）。缓存 logs/yt_cache/
  <video_id>.json（含字幕哈希）：重跑报告不重复抓、不重复调 LLM。
- 单视频失败（无字幕/抓取失败/LLM 失败）→ 标记 status/failed 原因跳过，
  不阻塞报告。

纯逻辑 + IO 边界分离（runner/llm_text 可注入，测试零网络零 yt-dlp）。
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from engine.news_digest import identify_tickers

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "logs" / "yt_cache"
ET_TZ = ZoneInfo("America/New_York")

YT_DLP = os.environ.get("YT_DLP", "/opt/homebrew/bin/yt-dlp")
# YouTube 反爬（2026-10-25 本机实测）：裸调被拦「Sign in to confirm you're
# not a bot」，必须带 Chrome cookies；env YT_DLP_COOKIES="" 可关
YT_DLP_COOKIES = os.environ.get("YT_DLP_COOKIES", "chrome")
TRANSCRIPT_MAX_WORDS = 6000      # 字幕截断（词）
SUB_TIMEOUT = 120                # 单次 yt-dlp 调用超时（秒）
META_TIMEOUT = 60

_VID_RE = re.compile(
    r"(?:youtube\.com/watch\?[^\s]*\bv=|youtu\.be/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ---------------------------------------------------------------------------
# 链接解析（纯逻辑）
# ---------------------------------------------------------------------------

def parse_video_id(url: str) -> Optional[str]:
    """watch?v= / youtu.be/ / shorts/ 三种形态 → 11 位 video_id；否则 None。"""
    m = _VID_RE.search(url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# VTT → 纯文本（纯逻辑）
# ---------------------------------------------------------------------------

_VTT_TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s-->")
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def vtt_to_text(vtt: str, max_words: int = TRANSCRIPT_MAX_WORDS) -> str:
    """VTT → 纯文本：去时间戳/序号/样式标签，连续重复行合并（自动字幕
    逐词滚动会产生大量重复行），词数截断。"""
    lines = []
    prev = ""
    for raw in vtt.splitlines():
        line = raw.strip()
        if (not line or line.startswith("WEBVTT") or line.isdigit()
                or _VTT_TS_RE.match(line) or line.startswith(("Kind:", "Language:"))):
            continue
        line = _VTT_TAG_RE.sub("", line).strip()
        if not line:
            continue
        # 自动字幕滚动重复：当前行被上一行包含（或相等）则跳过
        if prev and (line == prev or (line in prev and len(line) < len(prev))):
            continue
        if prev and prev in line:      # 扩展行：用更全的新行替换上一行
            if lines:
                lines.pop()
        prev = line
        lines.append(line)
    text = " ".join(lines)
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]) + " …[truncated]"
    return text


# ---------------------------------------------------------------------------
# IO 边界：yt-dlp subprocess（runner 可注入）
# ---------------------------------------------------------------------------

def _run(cmd: List[str], cwd: Optional[str] = None) -> str:
    """跑 yt-dlp，返回 stdout；非零退出/超时抛 RuntimeError。"""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=SUB_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"yt-dlp 超时: {e}") from e
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp 失败 rc={r.returncode}: "
                           f"{(r.stderr or '')[-300:]}")
    return r.stdout


def _cookie_args() -> List[str]:
    return (["--cookies-from-browser", YT_DLP_COOKIES]
            if YT_DLP_COOKIES else [])


def fetch_metadata(url: str, runner: Callable = _run) -> dict:
    """yt-dlp -j 元数据：{video_id,title,channel,upload_date,timestamp,
    duration,url}。timestamp（精确秒）优先，upload_date（日级）兜底。
    附带提取字幕轨道直链（sub_vtt_url 人工 / auto_vtt_url 自动，en 优先）：
    字幕优先走直链 urllib（少一次 yt-dlp 调用，降低 429 限流暴露）。"""
    out = runner([YT_DLP, "-j", "--skip-download"] + _cookie_args() + [url])
    meta = json.loads(out)

    def _vtt_url(caps: dict) -> Optional[str]:
        for lang in ("en", "en-US", "en-orig"):
            for t in (caps or {}).get(lang, []):
                if t.get("ext") == "vtt" and t.get("url"):
                    return t["url"]
        for lang, tracks in (caps or {}).items():   # 任意 en.* 兜底
            if str(lang).startswith("en"):
                for t in tracks:
                    if t.get("ext") == "vtt" and t.get("url"):
                        return t["url"]
        return None

    return {
        "video_id": meta.get("id") or parse_video_id(url),
        "title": meta.get("title") or "",
        "channel": meta.get("channel") or meta.get("uploader") or "",
        "upload_date": meta.get("upload_date") or "",     # YYYYMMDD（日级）
        "timestamp": meta.get("timestamp"),               # epoch 秒（可能 None）
        "duration": meta.get("duration"),
        "url": meta.get("webpage_url") or url,
        "sub_vtt_url": _vtt_url(meta.get("subtitles")),
        "auto_vtt_url": _vtt_url(meta.get("automatic_captions")),
    }


def _fetch_url_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_transcript(video_id: str, url: str,
                     runner: Callable = _run,
                     meta: Optional[dict] = None,
                     fetcher: Callable[[str], str] = _fetch_url_text
                     ) -> Optional[str]:
    """抓字幕 → VTT 纯文本；无字幕返回 None。
    优先用元数据里的字幕直链（人工 sub_vtt_url 优先，自动 auto_vtt_url 兜底）
    urllib 直取；直链缺失/失败回退 yt-dlp --write-auto-subs 通道。"""
    for cap_url in ((meta or {}).get("sub_vtt_url"),
                    (meta or {}).get("auto_vtt_url")):
        if not cap_url:
            continue
        try:
            text = vtt_to_text(fetcher(cap_url))
            if text:
                return text
        except Exception as e:
            logger.warning("字幕直链抓取失败 %s: %s", video_id, e)
    tmp = tempfile.mkdtemp(prefix=f"yt-{video_id}-")
    runner([YT_DLP, "--write-auto-subs", "--write-subs",
            "--sub-langs", "en.*", "--skip-download",
            "--sub-format", "vtt", "-o", f"{video_id}.%(ext)s"]
           + _cookie_args() + [url], cwd=tmp)
    vtts = sorted(Path(tmp).glob("*.vtt"))
    if not vtts:
        return None
    # 人工字幕与自动字幕同名语言时会各出一个文件，取体积最大的（信息最全）
    best = max(vtts, key=lambda p: p.stat().st_size)
    text = vtt_to_text(best.read_text(encoding="utf-8", errors="replace"))
    return text or None


# ---------------------------------------------------------------------------
# 窗口过滤（纯逻辑）
# ---------------------------------------------------------------------------

def _window_days(window: tuple) -> set:
    """窗口覆盖的 ET 日期集合（upload_date 日级精度兜底用）。"""
    start, end = window
    days = set()
    t = start
    while t <= end + 86400:
        days.add(datetime.fromtimestamp(t, ET_TZ).strftime("%Y%m%d"))
        t += 86400
    return days


def in_window(meta: dict, window: Optional[tuple]) -> bool:
    """视频是否落在报告窗口：timestamp（精确）优先；缺失退回 upload_date
    （日级精度，落在窗口覆盖的任一 ET 日期即算）。window=None 不过滤。"""
    if window is None:
        return True
    ts = meta.get("timestamp")
    if ts:
        return window[0] <= ts <= window[1]
    ud = meta.get("upload_date") or ""
    if len(ud) == 8 and ud.isdigit():
        return ud in _window_days(window)
    return False


# ---------------------------------------------------------------------------
# 缓存（logs/yt_cache/<video_id>.json）
# ---------------------------------------------------------------------------

def _cache_path(video_id: str, cache_dir: Path) -> Path:
    return cache_dir / f"{video_id}.json"


def _load_cache(video_id: str, cache_dir: Path) -> Optional[dict]:
    try:
        d = json.loads(_cache_path(video_id, cache_dir)
                       .read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _save_cache(entry: dict, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path(entry["video_id"], cache_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_cache_path(entry["video_id"], cache_dir))


# ---------------------------------------------------------------------------
# LLM 摘要（IO 边界，llm_text 可注入）
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """你是美股交易员助手。输入是一个英文财经视频的字幕文本。
输出中文摘要（约 200 字）：核心观点 + 提到的标的/板块 + 多空倾向
（偏多/偏空/中性）。只输出摘要正文，不要标题不要前缀。"""


def summarize_video(meta: dict, transcript: str,
                    llm_text: Callable = None) -> tuple:
    """单视频 LLM 摘要。返回 (summary|None, meta)。LLM 失败 → (None, err)。"""
    if llm_text is None:
        from engine.agent_llm import call_llm_text
        llm_text = call_llm_text
    user = (f"视频标题：{meta.get('title')}\n频道：{meta.get('channel')}\n\n"
            f"字幕：\n{transcript}")
    try:
        out = llm_text(_SUMMARY_SYSTEM, user)
    except Exception as e:
        return None, {"error": str(e)}
    return (out.get("text") or "").strip(), {
        "model": out.get("_model"), "usage": out.get("_usage", {}),
        "latency_s": out.get("_latency_s")}


# ---------------------------------------------------------------------------
# 编排：单视频处理（缓存 → 元数据 → 字幕 → 摘要）
# ---------------------------------------------------------------------------

def process_video(url: str, window: Optional[tuple] = None,
                  ticker_set: Optional[set] = None,
                  llm_text: Callable = None, runner: Callable = _run,
                  cache_dir: Path = CACHE_DIR) -> dict:
    """处理一个视频源。返回 {video_id,title,channel,url,published,tickers,
    summary,status(ok/failed/cached/out_of_window),error?,from_cache}。
    任何一步失败都不抛异常（标记原因跳过，不阻塞报告）。"""
    video_id = parse_video_id(url)
    if not video_id:
        return {"url": url, "status": "failed", "error": "无法解析 video_id"}
    base = {"video_id": video_id, "url": url}
    try:
        meta = fetch_metadata(url, runner=runner)
    except Exception as e:
        logger.warning("视频元数据抓取失败 %s: %s", video_id, e)
        return {**base, "status": "failed", "error": f"元数据抓取失败: {e}"}
    base.update({"title": meta.get("title"), "channel": meta.get("channel"),
                 "url": meta.get("url") or url,
                 "published": meta.get("timestamp") or 0,
                 "upload_date": meta.get("upload_date")})
    if not in_window(meta, window):
        return {**base, "status": "out_of_window",
                "error": "发布时间不在报告窗口内"}

    # 字幕（不缓存原文，只缓存哈希；抓取本身便宜，LLM 才贵）
    try:
        transcript = fetch_transcript(video_id, base["url"], runner=runner,
                                      meta=meta)
    except Exception as e:
        logger.warning("字幕抓取失败 %s: %s", video_id, e)
        return {**base, "status": "failed", "error": f"字幕抓取失败: {e}"}
    if not transcript:
        return {**base, "status": "failed", "error": "无英文字幕"}
    t_hash = hashlib.sha1(transcript.encode("utf-8")).hexdigest()
    base["transcript_words"] = len(transcript.split())

    # 缓存命中（字幕哈希一致）→ 不重复调 LLM
    cached = _load_cache(video_id, cache_dir)
    if cached and cached.get("transcript_hash") == t_hash \
            and cached.get("summary"):
        base["summary"] = cached["summary"]
        base["summary_meta"] = {**cached.get("summary_meta", {}),
                                "cached": True}
    else:
        summary, smeta = summarize_video(meta, transcript, llm_text=llm_text)
        if summary is None:
            return {**base, "status": "failed",
                    "error": f"LLM 摘要失败: {smeta.get('error')}"}
        base["summary"] = summary
        base["summary_meta"] = smeta
        _save_cache({"video_id": video_id, "title": base["title"],
                     "channel": base["channel"], "url": base["url"],
                     "upload_date": base.get("upload_date"),
                     "published": base.get("published"),
                     "transcript_hash": t_hash, "summary": summary,
                     "summary_meta": smeta, "cached_at": time.time()},
                    cache_dir)
    if ticker_set:
        base["tickers"] = identify_tickers(
            f"{base.get('title') or ''} {base.get('summary') or ''}",
            ticker_set)
    base["status"] = "ok"
    base["from_cache"] = bool(cached and cached.get("transcript_hash") == t_hash)
    return base


def process_videos(urls: List[str], window: Optional[tuple] = None,
                   ticker_set: Optional[set] = None,
                   llm_text: Callable = None, runner: Callable = _run,
                   cache_dir: Path = CACHE_DIR) -> List[dict]:
    """批量处理视频源（顺序处理，yt-dlp 本身不慢；失败的标记跳过）。"""
    out = []
    for url in urls:
        out.append(process_video(url, window=window, ticker_set=ticker_set,
                                 llm_text=llm_text, runner=runner,
                                 cache_dir=cache_dir))
    return out
