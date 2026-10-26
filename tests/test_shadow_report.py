# -*- coding: utf-8 -*-
"""每日复盘报告（engine/shadow_report.py + console 调度/API）回归。

守的口径：分组统计数学（胜率/平均R/期望值/成交率/到2R/触止损占比，
期望值分母含未成交）；breakout 按 crowd≤3/>3 细分；大行情对照四种漏因
分类（未越界/无FVG/FVG根反向/拥挤三连失败/超时间窗）；止损松紧两个统计；
EOD 调度时段判定（周末不跑/16:20 触发/当天只一次）；API 缺省与指定日期。
全部走 tmp_path 库与 tmp 报告目录，不写 logs/。
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import research_db
from engine import shadow_report
from engine.shadow_report import generate_report
from .conftest import run

ET = ZoneInfo("America/New_York")
DAY = "2026-10-23"   # 周五


def _ts(hm: int, day: str = DAY) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    d += timedelta(minutes=hm)
    return int(d.timestamp())


def _bar(hm, o, h, l, c, fvg=0, day=DAY):
    return {"time": _ts(hm, day), "open": o, "high": h, "low": l,
            "close": c, "volume": 1000, "fvg_event": fvg}


def _shadow_row(sid, day, typ, sym, side, filled=1, settled=1, outcome_r=0.0,
                outcome="eod_flat", hit_stop=0, reached_2r=0, mfe_r=0.0,
                atr=2.0, trigger=100.0, stop=98.0, crowd=None):
    meta = {"crowd": crowd} if crowd is not None else {}
    return {
        "id": sid, "day": day, "type": typ, "symbol": sym,
        "ticker": sym.split(":")[-1], "side": side, "fill_mode": "market",
        "trigger_px": trigger, "limit_px": None, "stop_px": stop, "atr": atr,
        "bar_time": _ts(600), "meta": json.dumps(meta),
        "filled": filled, "fill_px": trigger if filled else None,
        "fill_time": _ts(600) if filled else None,
        "mfe_r": mfe_r, "mae_r": 0.1, "mfe_r_eod": mfe_r, "mae_r_eod": 0.1,
        "reached_1r": 0, "reached_2r": reached_2r, "hit_stop": hit_stop,
        "last_px": trigger, "outcome": outcome, "outcome_r": outcome_r,
        "settled": settled, "settled_at": time.time() if settled else None,
        "created_at": time.time(), "updated_at": time.time(),
    }


@pytest.fixture
def db(tmp_path):
    """tmp 库：bars + shadow_signals 合成数据。

    bars 剧本（均为多头视角大行情/漏因分类）：
    - WIN：+5% 大行情，越界+FVG+阳线 → 有 breakout 影子单（抓住）
    - NOX：+4% 大行情，从未越界 → 未越界
    - NOFVG：+4%，越界但下一根无 FVG → 无FVG
    - REVF：+4%，越界下一根有 FVG 但阴线 → FVG根反向
    - CRD1..CRD4：同一确认时刻 4 只拥挤（>3）且三连不成立 → 拥挤三连失败
      （只 CRD1 是大行情）
    - LATE：+5%，越界确认在 10:35（>10:30）→ 超时间窗
    - SMALL：+0.5% 非大行情（不进对照）
    """
    path = tmp_path / "research.db"
    bars_map = {
        # pmh=100 pml=98；09:45 越界，09:50 FVG+阳线确认；15:55 收 105（+5%）
        "NASDAQ:WIN": [
            _bar(540, 99, 100, 98, 99), _bar(545, 99, 100, 98, 99),
            _bar(570, 99, 100, 98.5, 99),
            _bar(585, 99, 100.8, 98.8, 100.5),          # 越界
            _bar(590, 100.5, 101.5, 100, 101, fvg=1),   # FVG+阳线确认
            _bar(955, 104, 105.5, 103, 105),
        ],
        # pmh=104 pml=98；全天未越界（收 103 < 104 但 +4%）→ 未越界
        "NASDAQ:NOX": [
            _bar(540, 99, 104, 98, 99), _bar(545, 99, 104, 98, 99),
            _bar(570, 99, 99.8, 98.5, 99.2),
            _bar(955, 102, 103.5, 101.5, 103),
        ],
        # 越界但下一根 fvg=0 → 无FVG；收 104（+4%）
        "NASDAQ:NOFVG": [
            _bar(540, 99, 100, 98, 99), _bar(545, 99, 100, 98, 99),
            _bar(570, 99, 100, 98.5, 99),
            _bar(585, 99, 100.8, 98.8, 100.5),
            _bar(590, 100.5, 101, 99.8, 100.8, fvg=0),
            _bar(955, 103, 104.5, 102.5, 104),
        ],
        # 越界下一根 fvg=1 但阴线 → FVG根反向；收 104（+4%）
        "NASDAQ:REVF": [
            _bar(540, 99, 100, 98, 99), _bar(545, 99, 100, 98, 99),
            _bar(570, 99, 100, 98.5, 99),
            _bar(585, 99, 100.8, 98.8, 100.5),
            _bar(590, 101, 101.2, 99.5, 100.2, fvg=1),  # FVG 但阴线
            _bar(955, 103, 104.5, 102.5, 104),
        ],
        # 越界确认 10:35（超窗）；收 105（+5%）
        "NASDAQ:LATE": [
            _bar(540, 99, 100, 98, 99), _bar(545, 99, 100, 98, 99),
            _bar(570, 99, 100, 98.5, 99),
            _bar(630, 99, 100.8, 98.8, 100.5),          # 10:30 越界
            _bar(635, 100.5, 101.5, 100, 101, fvg=1),   # 10:35 确认 → 超窗
            _bar(955, 104, 105.5, 103, 105),
        ],
        # 非大行情（+0.5%）：不进对照
        "NASDAQ:SMALL": [
            _bar(540, 99, 100, 98, 99), _bar(545, 99, 100, 98, 99),
            _bar(570, 100, 100.5, 99.5, 100),
            _bar(955, 100, 101, 99.8, 100.5),
        ],
    }
    # 拥挤组：CRD1..CRD4 同时 09:50 越界、09:55 同向 FVG+阳线确认（crowd=4>3），
    # 第三根都不满足三连（阴线）→ 拥挤三连失败；只有 CRD1 是大行情
    for i, sym in enumerate(("NASDAQ:CRD1", "NASDAQ:CRD2",
                             "NASDAQ:CRD3", "NASDAQ:CRD4")):
        bars_map[sym] = [
            _bar(540, 99, 100, 98, 99), _bar(545, 99, 100, 98, 99),
            _bar(570, 99, 100, 98.5, 99),
            _bar(590, 99, 100.8, 98.8, 100.5),          # 越界
            _bar(595, 100.5, 101.5, 100, 101, fvg=1),   # FVG+阳线确认
            _bar(600, 101.5, 101.8, 100, 100.8),        # 第三根阴线 → 无三连
            _bar(955, 104, 105.5, 103, 105 if i == 0 else 100.5),
        ]
    for sym, bars in bars_map.items():
        research_db.upsert_bars(sym, [{"time": b["time"], "open": b["open"],
                                       "high": b["high"], "low": b["low"],
                                       "close": b["close"], "volume": 1000,
                                       "fvg": {"fvg_event": b["fvg_event"]}}
                                      for b in bars], db_path=path)
    # shadow_signals：WIN 的 breakout（抓住，crowd=3 直接确认）+ 分组数学样本
    rows = [
        # WIN breakout 多：已成交、+2.5R、到2R、crowd=3
        _shadow_row("b-win", DAY, "breakout", "NASDAQ:WIN", "long",
                    outcome_r=2.5, outcome="reached_2R", reached_2r=1,
                    mfe_r=2.5, crowd=3),
        # breakout 三连升级组（crowd=4）：一胜一负
        _shadow_row("b-t1", DAY, "breakout", "NASDAQ:A", "long",
                    outcome_r=1.0, outcome="eod_flat", mfe_r=1.2, crowd=4),
        _shadow_row("b-t2", DAY, "breakout", "NASDAQ:B", "long",
                    outcome_r=-1.0, outcome="hit_stop", hit_stop=1,
                    mfe_r=0.6, crowd=4),
        # fvg_event 多：2 笔一胜一负（胜率 50%，期望 0.5R）
        _shadow_row("f1", DAY, "fvg_event", "NASDAQ:WIN", "long",
                    outcome_r=2.0, outcome="reached_2R", reached_2r=1,
                    mfe_r=2.0),
        _shadow_row("f2", DAY, "fvg_event", "NASDAQ:NOX", "long",
                    outcome_r=-1.0, outcome="hit_stop", hit_stop=1,
                    mfe_r=1.3, atr=2.0, trigger=100.0, stop=98.5),
        # fvg_event 空：1 笔未成交（filled=0）→ 成交率 50%
        _shadow_row("f3", DAY, "fvg_event", "NASDAQ:NOX", "short",
                    filled=0, settled=1, outcome="unfilled", outcome_r=0.0),
        _shadow_row("f4", DAY, "fvg_event", "NASDAQ:WIN", "short",
                    outcome_r=1.0, outcome="eod_flat", mfe_r=1.1),
        # proposal 多：1 笔（对照用）
        _shadow_row("p1", DAY, "proposal", "NASDAQ:WIN", "long",
                    outcome_r=0.5, outcome="eod_flat", mfe_r=0.8),
        # hit_stop 止损松紧样本：f2 距离 1.5 < atr 2.0（紧于 1×ATR），
        # mfe 1.3R（曾赚 >1R 被打回）
        _shadow_row("f5", DAY, "fvg_retest", "NASDAQ:NOFVG", "long",
                    outcome_r=-1.0, outcome="hit_stop", hit_stop=1,
                    mfe_r=0.3, atr=2.0, trigger=100.0, stop=97.0),
        # f5：距离 3.0 ≥ 2.0（不紧），mfe 0.3R（不曾赚 0.5R）
    ]
    for r in rows:
        research_db.shadow_upsert(r, db_path=path)
    return path


# ----------------------------------------------------------------------
# ① 分组数学 + ② crowd 细分
# ----------------------------------------------------------------------

def test_groups_math(db):
    rep = generate_report(DAY, db_path=db)
    g = {(x["type"], x["side"]): x for x in rep["groups"]}
    bo = g[("breakout", "long")]
    assert bo["count"] == 3 and bo["filled"] == 3
    assert bo["win_rate"] == round(2 / 3 * 100, 1)          # 2胜1负
    assert bo["avg_r"] == round((2.5 + 1.0 - 1.0) / 3, 3)   # 0.833
    assert bo["expectancy"] == bo["avg_r"]                  # 全部成交 → 同值
    assert bo["reached_2r_pct"] == round(1 / 3 * 100, 1)
    assert bo["hit_stop_pct"] == round(1 / 3 * 100, 1)
    fe_l = g[("fvg_event", "long")]
    assert fe_l["count"] == 2 and fe_l["win_rate"] == 50.0
    assert fe_l["avg_r"] == 0.5 and fe_l["expectancy"] == 0.5
    fe_s = g[("fvg_event", "short")]
    assert fe_s["count"] == 2 and fe_s["fill_rate"] == 50.0  # 1 笔未成交
    # 期望值分母含未成交：short 组 = (1.0 + 0) / 2 = 0.5；胜率只算成交的 1 笔
    assert fe_s["expectancy"] == 0.5 and fe_s["win_rate"] == 100.0


def test_breakout_crowd_split(db):
    rep = generate_report(DAY, db_path=db)
    buckets = {b["bucket"]: b for b in rep["breakout_crowd"]}
    direct = buckets["直接确认(crowd≤3)"]
    triple = buckets["三连升级(crowd>3)"]
    assert direct["count"] == 1 and direct["win_rate"] == 100.0
    assert triple["count"] == 2 and triple["win_rate"] == 50.0
    assert triple["expectancy"] == 0.0     # (1.0 + -1.0) / 2


# ----------------------------------------------------------------------
# ③ 大行情对照四种漏因
# ----------------------------------------------------------------------

def test_big_moves_miss_reasons(db):
    rep = generate_report(DAY, db_path=db)
    bm = rep["big_moves"]
    assert bm["caught_count"] == 1
    assert bm["caught"][0]["symbol"] == "NASDAQ:WIN"
    assert bm["caught"][0]["outcome_r"] == 2.5
    reasons = {m["symbol"].split(":")[-1]: m["reason"] for m in bm["missed"]}
    assert reasons == {
        "NOX": "未越界",
        "NOFVG": "无FVG",
        "REVF": "FVG根反向",
        "CRD1": "拥挤三连失败",
        "LATE": "超时间窗",
    }, reasons
    assert "SMALL" not in reasons           # 非大行情不进对照
    assert bm["total_big_moves"] == 6 and bm["missed_count"] == 5


# ----------------------------------------------------------------------
# ④ 止损松紧
# ----------------------------------------------------------------------

def test_stop_analysis(db):
    rep = generate_report(DAY, db_path=db)
    sa = rep["stop_analysis"]
    # hit_stop 3 笔：b-t2（距2.0=1×ATR 不紧）、f2（距1.5<2.0 紧）、f5（距3.0 不紧）
    assert sa["hit_stop_count"] == 3
    assert sa["tighter_than_1atr_count"] == 1
    assert sa["tighter_than_1atr_pct"] == round(1 / 3 * 100, 1)
    # 曾浮盈 >0.5R：b-t2(0.6) + f2(1.3)；>1R：仅 f2
    assert sa["had_05r_count"] == 2 and sa["had_1r_count"] == 1
    assert sa["had_1r_pct"] == round(1 / 3 * 100, 1)


def test_summary_lines(db):
    rep = generate_report(DAY, db_path=db)
    lines = shadow_report.summary_lines(rep)
    assert any("复盘" in l and "影子单" in l for l in lines)
    assert any("breakout" in l and "胜率" in l for l in lines)
    assert any("大行情" in l and "抓住" in l for l in lines)
    assert any("止损" in l and "曾赚" in l for l in lines)


# ----------------------------------------------------------------------
# ⑤ EOD 调度时段判定；⑥ API 缺省/指定日期
# ----------------------------------------------------------------------

def test_report_schedule_timing(console_app, monkeypatch, tmp_path, db):
    """调度：周末不跑；16:19 不跑；16:20 触发且当天只一次；落盘 tmp +
    事件 + iMessage 摘要通道。"""
    app = console_app
    monkeypatch.setattr(app, "_REPORTS_DIR", tmp_path)
    sent = []
    import engine.notifier as nmod
    n = nmod.Notifier(record_event=lambda *a, **k: None,
                      imessage_recipient=lambda: "+8613800000000")
    async def fake_exec(*a, **k):
        sent.append(a[2])
        class P:
            returncode = 0
            async def wait(self):
                return 0
        return P()
    n._exec = fake_exec
    monkeypatch.setattr(app, "notifier", n)
    events = []
    monkeypatch.setattr(app, "record_event",
                        lambda t, **d: events.append({"type": t, **d}))

    done = set()
    # 周六 16:30 → 不跑
    sat = datetime(2026, 10, 24, 16, 30, tzinfo=ET)
    assert run(app._report_check_once(sat, done, db_path=db)) is False
    # 周五 16:19 → 不跑
    fri_early = datetime(2026, 10, 23, 16, 19, tzinfo=ET)
    assert run(app._report_check_once(fri_early, done, db_path=db)) is False
    # 周五 16:20 → 生成
    fri = datetime(2026, 10, 23, 16, 20, tzinfo=ET)
    assert run(app._report_check_once(fri, done, db_path=db)) is True
    path = tmp_path / "report-20261023.json"
    assert path.exists()
    rep = json.loads(path.read_text(encoding="utf-8"))
    assert rep["day"] == DAY and rep["total_signals"] == 9
    assert any(e["type"] == "shadow_report" and e["day"] == DAY
               for e in events)
    assert sent and "复盘 2026-10-23" in sent[0]   # iMessage 摘要已发
    # 同一天再次到点 → 不重复生成
    assert run(app._report_check_once(fri, done, db_path=db)) is False


def test_api_report_default_and_named_day(console_app, monkeypatch):
    """API：day 缺省=当天（ET）；指定日期重算；非法格式 400。"""
    app = console_app
    called = []

    def fake_generate(day, db_path=None):
        called.append(day)
        return {"day": day, "total_signals": 0, "groups": [],
                "breakout_crowd": [], "big_moves": {}, "stop_analysis": {}}

    monkeypatch.setattr(app.shadow_report, "generate_report", fake_generate)
    r = run(app.api_shadow_report("2026-10-23"))
    assert r["day"] == "2026-10-23" and called == ["2026-10-23"]
    r = run(app.api_shadow_report(""))
    from datetime import datetime as _dt
    today = _dt.now(ET).strftime("%Y-%m-%d")
    assert called[-1] == today and r["day"] == today
    with pytest.raises(app.HTTPException) as ei:
        run(app.api_shadow_report("2026/10/23"))
    assert ei.value.status_code == 400
