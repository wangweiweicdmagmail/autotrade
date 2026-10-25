# -*- coding: utf-8 -*-
"""盘前区间过滤开关回归（console/app.py）。

守的口径：screen_premarket_filter / fvg_premarket_filter 两个独立开关
（settings 持久化，默认关）；开关开时多头要求现价 > attack_c.pmh、空头要求
现价 < attack_c.pml（严格不等）；attack 无数据/非法区间/现价缺失一律保留
（不误杀）；两开关互不影响；/api/settings 部分更新/持久化/空请求 400/
越界 400。现价 = collector 实时 bar.close。
"""
import json

import pytest

T = 1792764000  # 任意 300 对齐 epoch（作快照锚点）


def mk(direction, close, pmh=None, pml=None):
    d = {
        "ts_c": {"direction": direction, "trend_state": direction,
                 "divergence": 1.0, "atr": 1.0},
        "bar": {"time": T, "close": close},
        "bar_c": {"time": T - 300, "close": close},
        "fvg_c": {},
    }
    if pmh is not None:
        d["attack_c"] = {"time": T - 300, "pmh": pmh, "pml": pml}
    return d


MARKET = {
    "NASDAQ:UP_OK": mk(1, 105.0, 100.0, 90.0),   # 多头站上 pmh → 保留
    "NASDAQ:UP_NO": mk(1, 95.0, 100.0, 90.0),    # 多头未站上 → 剔除
    "NASDAQ:UP_EQ": mk(1, 100.0, 100.0, 90.0),   # 恰好=pmh → 剔除（严格>）
    "NASDAQ:UP_NA": mk(1, 95.0),                 # 无 attack → 保留
    "NASDAQ:DN_OK": mk(-1, 85.0, 100.0, 90.0),   # 空头跌破 pml → 保留
    "NASDAQ:DN_NO": mk(-1, 95.0, 100.0, 90.0),   # 空头未跌破 → 剔除
    "NASDAQ:DN_NA": mk(-1, 95.0),                # 无 attack → 保留
}


@pytest.fixture
def app(console_app, monkeypatch):
    console_app._settings.pop("screen_premarket_filter", None)
    console_app._settings.pop("fvg_premarket_filter", None)
    monkeypatch.setattr(console_app.collector, "latest", lambda: MARKET)
    console_app._fvg_events.clear()
    console_app._fvg_retests.clear()
    console_app._fvg_warnings.clear()
    return console_app


def tickers(rows):
    return sorted(r["ticker"] for r in rows)


def test_screen_off_unchanged(app):
    """开关关 = 行为不变（全量候选）。"""
    s = app.compute_screen()
    assert tickers(s["long"]) == ["UP_EQ", "UP_NA", "UP_NO", "UP_OK"]
    assert tickers(s["short"]) == ["DN_NA", "DN_NO", "DN_OK"]


def test_screen_on_filters_and_keeps_na(app):
    """开关开 = 不满足剔除（含恰好相等）/ 无数据保留 / 行带 pmh/pml。"""
    app._settings["screen_premarket_filter"] = True
    s = app.compute_screen()
    assert tickers(s["long"]) == ["UP_NA", "UP_OK"]
    assert tickers(s["short"]) == ["DN_NA", "DN_OK"]
    row = next(r for r in s["long"] if r["ticker"] == "UP_OK")
    assert row["pmh"] == 100.0 and row["pml"] == 90.0
    row_na = next(r for r in s["long"] if r["ticker"] == "UP_NA")
    assert row_na["pmh"] is None and row_na["pml"] is None


def _ev(sym, side, bt):
    return {"id": f"{sym}:{bt}:{side}", "symbol": sym, "ticker": sym.split(":")[-1],
            "side": side, "bar_time": bt, "top": 101.0, "bottom": 100.0,
            "height": 1.0, "atr": 1.0, "ratio": 1.0}


def _seed_fvg_buffers(app):
    for sym, side in (("NASDAQ:UP_OK", "long"), ("NASDAQ:UP_NO", "long"),
                      ("NASDAQ:DN_OK", "short"), ("NASDAQ:DN_NO", "short"),
                      ("NASDAQ:UP_NA", "long")):
        app._fvg_events.append(_ev(sym, side, T - 300))
        app._fvg_retests.append({**_ev(sym, side, T - 300), "retest_time": T,
                                 "retest_px": 100.5})
        app._fvg_warnings.append({**_ev(sym, side, T - 300),
                                  "warn_time": 1700000000})


def test_fvg_off_unchanged(app):
    """FVG 开关关 = 事件/回踩/预警全量；screen 开关不影响 FVG。"""
    _seed_fvg_buffers(app)
    snap = app._fvg_monitor_snapshot()
    slot_events = [e for sl in snap["slots"] for e in sl["long"] + sl["short"]]
    assert len(slot_events) == 5 and len(snap["retests"]) == 5 \
        and len(snap["warnings"]) == 5


def test_fvg_on_filters_all_channels(app):
    """FVG 开关开 = 槽位事件/首次回踩/预警同口径过滤，无数据保留。"""
    _seed_fvg_buffers(app)
    app._settings["fvg_premarket_filter"] = True
    snap = app._fvg_monitor_snapshot()
    slot_events = [e for sl in snap["slots"] for e in sl["long"] + sl["short"]]
    kept = sorted((e["ticker"], e["side"]) for e in slot_events)
    assert kept == [("DN_OK", "short"), ("UP_NA", "long"), ("UP_OK", "long")]
    assert sorted(r["ticker"] for r in snap["retests"]) == ["DN_OK", "UP_NA", "UP_OK"]
    assert sorted(w["ticker"] for w in snap["warnings"]) == ["DN_OK", "UP_NA", "UP_OK"]


def test_switches_independent(app):
    """两开关互不影响：fvg 开 + screen 关 → 选股仍全量。"""
    app._settings["fvg_premarket_filter"] = True
    app._settings["screen_premarket_filter"] = False
    s = app.compute_screen()
    assert len(s["long"]) == 4 and len(s["short"]) == 3


def test_settings_endpoint_partial_update_and_validation(app, tmp_path):
    """/api/settings：部分更新 / 落盘（tmp）/ 空请求400 / 越界400 / GET 全量键。"""
    from .conftest import run
    app._settings.clear()
    app._settings.update({"stop_atr_mult": 1.5})
    resp = run(app.api_update_settings(
        app.SettingsRequest(screen_premarket_filter=True), trader="master"))
    assert resp["ok"] and resp["screen_premarket_filter"] is True
    assert resp["fvg_premarket_filter"] is False
    assert resp["stop_atr_mult"] == 1.5
    on_disk = json.loads(app._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert on_disk["screen_premarket_filter"] is True
    assert on_disk["stop_atr_mult"] == 1.5
    resp = run(app.api_update_settings(
        app.SettingsRequest(stop_atr_mult=2.0), trader="master"))
    assert resp["screen_premarket_filter"] is True   # 只传倍数：开关保持
    with pytest.raises(app.HTTPException) as ei:
        run(app.api_update_settings(app.SettingsRequest(), trader="master"))
    assert ei.value.status_code == 400
    with pytest.raises(app.HTTPException) as ei:
        run(app.api_update_settings(
            app.SettingsRequest(stop_atr_mult=99.0), trader="master"))
    assert ei.value.status_code == 400
    got = run(app.api_get_settings())
    assert set(got) == {"stop_atr_mult", "screen_premarket_filter",
                        "fvg_premarket_filter", "imessage_recipient"}
    assert got["imessage_recipient"] == ""        # 未配置默认空
    # iMessage 收件人：设置 → 落盘 → 空串清除
    resp = run(app.api_update_settings(
        app.SettingsRequest(imessage_recipient=" +8613800000000 "),
        trader="master"))
    assert resp["imessage_recipient"] == "+8613800000000"   # strip
    on_disk = json.loads(app._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert on_disk["imessage_recipient"] == "+8613800000000"
    resp = run(app.api_update_settings(
        app.SettingsRequest(imessage_recipient=""), trader="master"))
    assert resp["imessage_recipient"] == ""
    on_disk = json.loads(app._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "imessage_recipient" not in on_disk


def test_defaults_off(app):
    """默认关闭：settings 缺键回退 False。"""
    assert app._screen_premarket_filter() is False
    assert app._fvg_premarket_filter() is False
