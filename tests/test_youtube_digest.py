# -*- coding: utf-8 -*-
"""YouTube 视频字幕内容源回归（engine/youtube_digest.py）。

守的口径：链接解析三形态（watch?v=/youtu.be//shorts/）；VTT→纯文本
（去时间戳/标签、滚动重复行合并、词数截断）；窗口过滤（timestamp 精确优先、
upload_date 日级兜底、窗口外跳过注明）；缓存命中（字幕哈希一致）不重复调
LLM；单视频失败标记跳过不阻塞。runner/llm_text 全注入，零网络零 yt-dlp。
"""
import json

import pytest

from engine import youtube_digest as yd

VID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VID}"

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
<c>Hello</c> <c>everyone</c>

00:00:02.000 --> 00:00:04.000
Hello everyone

00:00:04.000 --> 00:00:06.000
Hello everyone welcome back

00:00:06.000 --> 00:00:08.000
Today we talk about NVDA earnings

2
00:00:08.000 --> 00:00:10.000
and the Fed rate decision
"""


def test_parse_video_id_three_forms():
    assert yd.parse_video_id(URL) == VID
    assert yd.parse_video_id(f"https://youtu.be/{VID}") == VID
    assert yd.parse_video_id(f"https://www.youtube.com/shorts/{VID}") == VID
    assert yd.parse_video_id(f"https://www.youtube.com/watch?v={VID}&t=30s") == VID
    assert yd.parse_video_id("https://example.com/foo") is None
    assert yd.parse_video_id("") is None


def test_vtt_to_text():
    """去时间戳/序号/标签；滚动重复行合并（扩展行替换前缀行）。"""
    text = yd.vtt_to_text(VTT)
    assert "-->" not in text and "<c>" not in text and "WEBVTT" not in text
    # 「Hello everyone」被扩展行「Hello everyone welcome back」替换，不重复出现
    assert text.count("Hello everyone") == 1
    assert "welcome back" in text
    assert "NVDA earnings" in text and "Fed rate decision" in text


def test_vtt_truncation():
    """词数截断到上限。"""
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n" + " ".join(
        f"w{i}" for i in range(100)) + "\n"
    text = yd.vtt_to_text(vtt, max_words=10)
    assert text.endswith("…[truncated]")
    assert len(text.replace(" …[truncated]", "").split()) == 10


def test_in_window():
    """timestamp 精确优先；缺失退回 upload_date 日级；窗口外/无日期剔除。"""
    win = (10000, 20000)
    assert yd.in_window({"timestamp": 15000}, win) is True
    assert yd.in_window({"timestamp": 9999}, win) is False
    assert yd.in_window({"timestamp": 20001}, win) is False
    assert yd.in_window({"timestamp": None, "upload_date": "19700105"}, win) is False
    assert yd.in_window({"timestamp": None, "upload_date": ""}, win) is False
    assert yd.in_window({"timestamp": 1}, None) is True   # 无窗口不过滤


def test_in_window_upload_date_fallback():
    """upload_date 日级兜底：落在窗口覆盖的 ET 日期集合内即纳入。"""
    from engine.news_digest import news_window
    win = news_window("2026-10-23")   # 10-22 16:00 ~ 10-23 09:30 ET
    assert yd.in_window({"upload_date": "20261023"}, win) is True
    assert yd.in_window({"upload_date": "20261022"}, win) is True
    assert yd.in_window({"upload_date": "20261021"}, win) is False


META_JSON = json.dumps({
    "id": VID, "title": "Market Morning: NVDA earnings preview",
    "channel": "Test Channel", "upload_date": "20261025",
    "timestamp": 1792800000, "duration": 600,
    "webpage_url": URL,
})


def _runner_factory(transcript_vtt=VTT, meta=META_JSON, fail_meta=False,
                    fail_subs=False):
    """假 yt-dlp runner：-j 返回元数据；字幕命令往 cwd 写 fixture VTT。"""
    def runner(cmd, cwd=None):
        if fail_meta and "-j" in cmd:
            raise RuntimeError("meta boom")
        if "-j" in cmd:
            return meta
        if fail_subs:
            raise RuntimeError("subs boom")
        if "--write-auto-subs" in cmd:
            if transcript_vtt is not None:
                from pathlib import Path
                Path(cwd, f"{VID}.en.vtt").write_text(transcript_vtt,
                                                      encoding="utf-8")
            return ""
        raise AssertionError(f"未预期命令: {cmd}")
    return runner


def _fake_llm_factory(counter):
    def fake(system, user):
        counter["n"] += 1
        return {"text": "看多 NVDA 财报，核心观点：需求旺。",
                "_model": "mock/test", "_usage": {"total_tokens": 50},
                "_latency_s": 0.1}
    return fake


def test_process_video_full_flow_and_cache(tmp_path):
    """全流程：解析→元数据→字幕→LLM 摘要→缓存；二次跑缓存命中不调 LLM。"""
    counter = {"n": 0}
    r1 = yd.process_video(URL, ticker_set={"NVDA"},
                          llm_text=_fake_llm_factory(counter),
                          runner=_runner_factory(), cache_dir=tmp_path)
    assert r1["status"] == "ok" and r1["from_cache"] is False
    assert r1["title"] == "Market Morning: NVDA earnings preview"
    assert r1["channel"] == "Test Channel"
    assert r1["summary"].startswith("看多 NVDA")
    assert r1["tickers"] == ["NVDA"]
    assert counter["n"] == 1
    assert (tmp_path / f"{VID}.json").exists()

    # 二次跑：同一 runner（字幕仍被抓），但缓存命中 → 不再调 LLM
    counter2 = {"n": 0}
    r2 = yd.process_video(URL, llm_text=_fake_llm_factory(counter2),
                          runner=_runner_factory(), cache_dir=tmp_path)
    assert r2["status"] == "ok" and r2["from_cache"] is True
    assert r2["summary"] == r1["summary"]
    assert counter2["n"] == 0


def test_process_video_failure_paths(tmp_path):
    """失败不阻塞：无法解析/元数据失败/字幕失败/无字幕/窗口外/LLM 失败，
    全部返回 failed（或 out_of_window）+ error，不抛异常。"""
    llm = _fake_llm_factory({"n": 0})
    assert yd.process_video("https://example.com/x", llm_text=llm,
                            runner=_runner_factory(),
                            cache_dir=tmp_path)["status"] == "failed"
    r = yd.process_video(URL, llm_text=llm,
                         runner=_runner_factory(fail_meta=True),
                         cache_dir=tmp_path)
    assert r["status"] == "failed" and "元数据" in r["error"]
    r = yd.process_video(URL, llm_text=llm,
                         runner=_runner_factory(fail_subs=True),
                         cache_dir=tmp_path)
    assert r["status"] == "failed" and "字幕" in r["error"]
    r = yd.process_video(URL, llm_text=llm,
                         runner=_runner_factory(transcript_vtt=None),
                         cache_dir=tmp_path)
    assert r["status"] == "failed" and "无英文字幕" in r["error"]
    # 窗口外
    r = yd.process_video(URL, window=(1792900000, 1792910000), llm_text=llm,
                         runner=_runner_factory(), cache_dir=tmp_path)
    assert r["status"] == "out_of_window"
    # LLM 失败
    def boom(s, u):
        raise RuntimeError("no key")
    r = yd.process_video(URL, llm_text=boom, runner=_runner_factory(),
                         cache_dir=tmp_path)
    assert r["status"] == "failed" and "LLM" in r["error"]


def test_report_section6_with_videos():
    """generate_report 接视频：第六章「盘前视频观点」必检 + 摘要并入输入。"""
    from engine import news_digest as nd
    md = ("## 一、隔夜市场综述\n多。\n## 二、今日主要主题\nAI。\n"
          "## 三、自选股催化清单\n无。\n## 四、盘前异动股（非自选股热点）\n无。\n"
          "## 五、今日风险提示\n无。\n## 六、盘前视频观点\n看多 NVDA。")
    seen = {}

    def fake_llm(system, user):
        seen["user"] = user
        return {"text": md, "_model": "mock/test", "_usage": {}, "_latency_s": 0.1}

    videos = [{"status": "ok", "title": "Morning show", "channel": "CNBC",
               "summary": "看多 NVDA 财报", "url": URL, "video_id": VID}]
    text, meta = nd.generate_report("2026-10-25", [], {}, llm_text=fake_llm,
                                    videos=videos)
    assert meta["degraded"] is False
    assert "六、盘前视频观点" in text
    assert "看多 NVDA 财报" in seen["user"]     # 视频摘要并入主题归纳输入


def test_report_section6_fallback_lists_videos():
    """降级路径：LLM 失败时第六章列视频标题+抓取状态。"""
    from engine import news_digest as nd

    def boom(s, u):
        raise RuntimeError("no key")

    videos = [{"status": "ok", "title": "Morning show", "channel": "CNBC",
               "summary": "x", "url": URL, "video_id": VID,
               "transcript_words": 1234},
              {"status": "failed", "url": "https://youtu.be/x", "video_id": "x",
               "error": "无英文字幕"}]
    text, meta = nd.generate_report("2026-10-25", [], {}, llm_text=boom,
                                    videos=videos)
    assert meta["degraded"] is True
    assert "六、盘前视频观点" in text
    assert "Morning show" in text and "无英文字幕" in text


def test_report_without_videos_unchanged():
    """无视频源时行为不变：五章节即可通过校验（不含第六章）。"""
    from engine import news_digest as nd
    md = ("## 一、隔夜市场综述\n多。\n## 二、今日主要主题\nAI。\n"
          "## 三、自选股催化清单\n无。\n## 四、盘前异动股（非自选股热点）\n无。\n"
          "## 五、今日风险提示\n无。")
    text, meta = nd.generate_report("2026-10-25", [], {},
                                    llm_text=lambda s, u: {"text": md})
    assert meta["degraded"] is False
    assert "六、盘前视频观点" not in text


def test_transcript_direct_url_path():
    """字幕直链优先：meta 带 auto_vtt_url → urllib 直取，不调 yt-dlp 字幕命令；
    直链失败回退 yt-dlp。"""
    meta = {"sub_vtt_url": None, "auto_vtt_url": "https://timedtext/vtt"}
    called = {"yt": 0}

    def runner(cmd, cwd=None):
        called["yt"] += 1
        from pathlib import Path
        Path(cwd, f"{VID}.en.vtt").write_text(VTT, encoding="utf-8")
        return ""

    text = yd.fetch_transcript(VID, URL, runner=runner, meta=meta,
                               fetcher=lambda u: VTT)
    assert "NVDA earnings" in text
    assert called["yt"] == 0                     # 直链成功，未走 yt-dlp
    # 直链 429/失败 → 回退 yt-dlp 通道
    def boom(u):
        raise OSError("429")
    text = yd.fetch_transcript(VID, URL, runner=runner, meta=meta, fetcher=boom)
    assert "NVDA earnings" in text
    assert called["yt"] == 1


def test_metadata_extracts_caption_urls():
    """fetch_metadata 从 -j JSON 提取人工/自动字幕 vtt 直链（en 优先）。"""
    raw = json.loads(META_JSON)
    raw["automatic_captions"] = {"en": [{"ext": "vtt", "url": "https://t/auto"}]}
    raw["subtitles"] = {"en": [{"ext": "vtt", "url": "https://t/manual"}]}
    meta = yd.fetch_metadata(URL, runner=lambda cmd, cwd=None: json.dumps(raw))
    assert meta["sub_vtt_url"] == "https://t/manual"
    assert meta["auto_vtt_url"] == "https://t/auto"
    assert meta["title"] == "Market Morning: NVDA earnings preview"
