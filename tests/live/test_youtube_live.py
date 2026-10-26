# -*- coding: utf-8 -*-
"""LIVE：YouTube 字幕抓取真网冒烟（yt-dlp + 网络 + Chrome cookies）。

守的口径：真实视频元数据可抓、字幕可抓且 VTT 解析出非空文本。
默认套件不跑：python3 -m pytest tests/live/ -m live
"""
import shutil

import pytest

pytestmark = pytest.mark.live

from engine import youtube_digest as yd

# 一个稳定的公开课/财经长视频（字幕一直存在）
URL = "https://www.youtube.com/watch?v=wldMfxicWa4"

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not shutil.which(yd.YT_DLP),
                                 reason="yt-dlp 未安装")]


def test_real_metadata_and_transcript():
    meta = yd.fetch_metadata(URL)
    assert meta["video_id"] and meta["title"]
    text = yd.fetch_transcript(meta["video_id"], meta["url"])
    assert text and len(text.split()) > 50, "字幕为空或过短"
