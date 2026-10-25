# -*- coding: utf-8 -*-
"""统一告警出口（engine/notifier.py）回归。

守的口径：分级 info/warning/critical；同 key 活跃期内幂等（不重复记录/
通知）；recover 解除并发恢复通知；clear 静默摘除；iMessage 通道：未配置
收件人跳过、同 key 5 分钟节流、osascript 命令构造、失败/超时静默记 log。
全 mock（子进程工厂替换），不碰真实 Messages。
"""
import time

import pytest

from engine.notifier import Notifier
from .conftest import run


class FakeProc:
    def __init__(self, returncode=0, hang=False):
        self.returncode = returncode
        self._hang = hang

    async def wait(self):
        if self._hang:
            import asyncio
            await asyncio.sleep(60)   # 模拟 osascript 卡死
        return self.returncode


def mk_notifier(recipient="", throttle_s=300.0, exec_result=None):
    events = []
    calls = []

    async def fake_exec(*args, **kw):
        calls.append(args)
        return exec_result if exec_result is not None else FakeProc()

    n = Notifier(record_event=lambda t, **d: events.append({"type": t, **d}),
                 imessage_recipient=lambda: recipient,
                 throttle_s=throttle_s)
    n._exec = fake_exec
    return n, events, calls


def test_levels_and_idempotent_active():
    """同 key 活跃期内幂等；不同 level 都进事件与活跃列表；critical 排前。"""
    n, events, calls = mk_notifier()

    async def go():
        await n.notify("k1", "warning", "数据陈旧", "600s 未更新")
        await n.notify("k1", "warning", "数据陈旧", "610s 未更新")   # 重复 → 幂等
        await n.notify("k2", "critical", "TWS 断线", "请人工重启 7496")
        await n.notify("k3", "info", "提示", "info 不发 iMessage")
    run(go())
    assert len(events) == 3
    assert [e["type"] for e in events] == ["alert"] * 3
    alerts = n.active_alerts()
    assert [a["key"] for a in alerts] == ["k2", "k1", "k3"]   # critical 排前
    assert n.is_active("k1") and n.active_alerts()[1]["msg"] == "600s 未更新"


def test_recover_and_clear():
    """recover 解除+恢复通知；clear 静默摘除（无事件无通知）；重复 recover 空转。"""
    n, events, calls = mk_notifier()

    async def go():
        await n.notify("k1", "warning", "数据陈旧", "stale")
        await n.recover("k1", "已恢复更新")
        await n.recover("k1")          # 已解除 → 空转
        await n.notify("k2", "info", "提示", "x")
        n.clear("k2")
    run(go())
    types = [e["type"] for e in events]
    assert types == ["alert", "alert_recovered", "alert"]
    assert not n.is_active("k1") and not n.is_active("k2")


def test_imessage_skipped_when_unconfigured():
    """未配置收件人 → warning/critical 也不发 iMessage（通道跳过）。"""
    n, events, calls = mk_notifier(recipient="")

    async def go():
        await n.notify("k1", "critical", "TWS 断线", "重启 7496")
        await n.recover("k1")
    run(go())
    assert calls == []


def test_imessage_command_and_throttle():
    """命令构造正确；同 key 5 分钟内不重复发（含恢复通知也受节流）。"""
    n, events, calls = mk_notifier(recipient="+8613800000000", throttle_s=300)

    async def go():
        await n.notify("k1", "critical", "TWS 断线", '请人工重启 "7496"')
        await n.notify("k2", "critical", "裸仓", "STP 缺失")
        await n.recover("k1")          # 恢复通知与 k1 首发同 key → 被节流
    run(go())
    assert len(calls) == 2
    assert calls[0][0] == "osascript" and calls[0][1] == "-e"
    assert 'tell application "Messages" to send' in calls[0][2]
    assert 'to buddy "+8613800000000"' in calls[0][2]
    assert '\\"7496\\"' in calls[0][2]   # 双引号转义
    # 节流窗口过后可再发
    n._imsg_last["k1"] = time.time() - 301
    run(n.notify("k1", "critical", "TWS 断线", "再次"))  # k1 已 recover，可重报
    assert len(calls) == 3


def test_imessage_failure_and_timeout_silent(monkeypatch):
    """发送失败（rc!=0）/ 卡死超时 → 静默记 log，不抛异常。"""
    import engine.notifier as nmod
    monkeypatch.setattr(nmod, "_IMESSAGE_TIMEOUT_S", 0.2)   # 超时不等满 10s
    n1, _, _ = mk_notifier(recipient="a@b.c", exec_result=FakeProc(returncode=1))
    run(n1.notify("k1", "critical", "t", "m"))   # 不抛
    n2, _, _ = mk_notifier(recipient="a@b.c", exec_result=FakeProc(hang=True),
                           throttle_s=0)
    run(n2.notify("k2", "warning", "t", "m"))    # 超时静默
