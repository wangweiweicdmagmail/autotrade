"""盘前交易分析报告 CLI（premarket_report.py）
==================================================
用法：
    python3 premarket_report.py                    # 当日模式（现在往前 18h）
    python3 premarket_report.py --day 2026-10-23   # 回填模式：Google News
                                                   # after:/before: 日期运算符
                                                   # + pubDate 窗口过滤，
                                                   # 跳过 MarketWatch（无历史）

流程：news_digest 采集（大盘层 + watchlist 个股层）→ LLM 生成中文 markdown
盘前报告（降级则标题汇编）→ logs/reports/premarket-YYYYMMDD.md。
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from engine import news_digest

ET = ZoneInfo("America/New_York")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="", help="美东交易日，如 2026-10-23；"
                                              "缺省 = 当日模式（前 18h）")
    args = ap.parse_args()

    tickers = sorted({s.split(":")[-1] for s in config.WATCHLIST
                      if not s.startswith(("BLACKBULL:", "CAPITALCOM:"))})
    ticker_set = set(tickers)

    if args.day:
        day = args.day
        window = news_digest.news_window(day)
        d = datetime.strptime(day, "%Y-%m-%d")
        after = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        suffix = news_digest.backfill_suffix(after, day)   # before 含当天
        print(f"回填 {day}（美东）| 窗口 {after} 16:00 ~ {day} 09:30 ET"
              f" | {len(tickers)} 标的", flush=True)
        market = news_digest.collect_market(window=window, include_mw=False,
                                            query_suffix=suffix,
                                            ticker_set=ticker_set)
        ticker_news = news_digest.collect_tickers(tickers, window=window,
                                                  query_suffix=suffix)
    else:
        day = datetime.now(ET).strftime("%Y-%m-%d")
        window = None
        print(f"当日模式 {day}（美东）| 前 18h | {len(tickers)} 标的", flush=True)
        market = news_digest.collect_market(ticker_set=ticker_set)
        ticker_news = news_digest.collect_tickers(tickers)
    n_tk = sum(len(v) for v in ticker_news.values())
    print(f"采集完成：大盘 {len(market)} 条 / 自选股 {len(ticker_news)} 票"
          f" {n_tk} 条", flush=True)

    # 视频源（logs/report_sources.json 里 enabled 的）：字幕 + LLM 摘要
    from engine import youtube_digest
    try:
        sources = json.loads((Path("logs") / "report_sources.json")
                             .read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        sources = []
    urls = [s["url"] for s in sources if isinstance(s, dict) and s.get("enabled")]
    videos = []
    if urls:
        v_window = window if args.day else (time.time() - 18 * 3600,
                                            time.time())
        print(f"视频源 {len(urls)} 个，抓取字幕+摘要 …", flush=True)
        videos = youtube_digest.process_videos(urls, window=v_window,
                                               ticker_set=ticker_set)
        ok_n = sum(1 for v in videos if v.get("status") == "ok")
        print(f"视频就绪 {ok_n}/{len(videos)}"
              + "".join(f"\n  ⚠️ {v.get('title') or v.get('url')}: "
                        f"{v.get('error')}"
                        for v in videos if v.get("status") != "ok"),
              flush=True)

    t0 = time.time()
    report, meta = news_digest.generate_report(day, market, ticker_news,
                                               videos=videos)
    path = news_digest.save_report(day, report, market, ticker_news, meta,
                                   window=window, videos=videos)
    print(f"报告已生成 → {path}", flush=True)
    print(f"  模型: {meta.get('model') or '降级'} | degraded: "
          f"{meta.get('degraded')} | 耗时 {meta.get('latency_s')}s"
          f" | tokens {(meta.get('usage') or {}).get('total_tokens', '?')}",
          flush=True)
    if meta.get("degraded"):
        print(f"  降级原因: {meta.get('error')}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
