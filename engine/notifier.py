# -*- coding: utf-8 -*-
"""
统一告警出口 (Notifier)
========================

分级 info / warning / critical，三通道：
1. record_event + 前端通知：活跃告警进 `active_alerts()`（console 快照
   下发，前端横幅 + 声音通报由前端按 level 驱动，复用现有 speak/playTick）。
2. 声音：前端侧实现（见上），后端只供状态。
3. iMessage：osascript 子进程
   `tell application "Messages" to send "<msg>" to buddy "<recipient>"`，
   asyncio 执行、10s 超时、失败静默记 log；**未配置收件人则跳过该通道**
   （recipient 由调用方以 callable 注入，读全局设置 imessage_recipient）。

节流：同一告警 key 5 分钟内不重复发 iMessage（前端/事件不节流——同 key
活跃期内本来就不重复记录）。
语义：notify(key,...) 同 key 活跃期内幂等（不重复记录/通知）；
recover(key) 解除并发恢复通知；clear(key) 静默摘除（不通知，用于非交易
时段直接清理数据陈旧类告警）。全 mock 可测：通道全走注入的 callable /
可替换的子进程工厂。
"""

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

LEVELS = ("info", "warning", "critical")
# iMessage 通道只覆盖 warning/critical（info 不打扰手机）
_IMESSAGE_LEVELS = ("warning", "critical")
_IMESSAGE_TIMEOUT_S = 10.0
_DEFAULT_THROTTLE_S = 300.0          # 同 key 5 分钟内不重复发 iMessage

_LOG_LEVEL = {"info": logging.INFO, "warning": logging.WARNING,
              "critical": logging.CRITICAL}


def _escape_applescript(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


class Notifier:
    """统一告警出口。record_event / imessage_recipient 均以 callable 注入。"""

    def __init__(self,
                 record_event: Optional[Callable] = None,
                 imessage_recipient: Optional[Callable[[], str]] = None,
                 throttle_s: float = _DEFAULT_THROTTLE_S) -> None:
        self._record_event = record_event
        self._imessage_recipient = imessage_recipient
        self._throttle_s = throttle_s
        self._active: Dict[str, dict] = {}        # key -> 活跃告警
        self._imsg_last: Dict[str, float] = {}    # key -> 上次 iMessage 发送时间
        # 子进程工厂可替换（测试 mock）；签名同 asyncio.create_subprocess_exec
        self._exec = asyncio.create_subprocess_exec

    # ------------------------------------------------------------------
    # 告警生命周期
    # ------------------------------------------------------------------

    async def notify(self, key: str, level: str, title: str, msg: str) -> None:
        """发出/刷新告警。同 key 活跃期内幂等（不重复记录、不重复通知）。"""
        assert level in LEVELS, f"非法告警级别: {level}"
        if key in self._active:
            return
        self._active[key] = {"key": key, "level": level, "title": title,
                             "msg": msg, "since": time.time()}
        logger.log(_LOG_LEVEL[level], "告警[%s] %s: %s", key, title, msg)
        if self._record_event:
            self._record_event("alert", key=key, level=level,
                               title=title, msg=msg)
        if level in _IMESSAGE_LEVELS:
            await self._send_imessage(key, f"[{level}] {title}：{msg}")

    async def recover(self, key: str, msg: str = "") -> None:
        """解除告警 + 恢复通知（事件/前端/iMessage，iMessage 同受节流）。"""
        entry = self._active.pop(key, None)
        if entry is None:
            return
        logger.info("告警解除[%s]: %s", key, msg or "已恢复")
        if self._record_event:
            self._record_event("alert_recovered", key=key,
                               level=entry["level"], title=entry["title"],
                               msg=msg or "已恢复")
        if entry["level"] in _IMESSAGE_LEVELS:
            await self._send_imessage(
                key, f"[恢复] {entry['title']}：{msg or '已恢复正常'}")

    def clear(self, key: str) -> None:
        """静默摘除（不记录不通知）：用于非交易时段直接清理等场景。"""
        self._active.pop(key, None)

    def active_alerts(self) -> List[dict]:
        """活跃告警列表（critical 在前，同级按触发时间）。"""
        return sorted(self._active.values(),
                      key=lambda a: (a["level"] != "critical", a["since"]))

    def is_active(self, key: str) -> bool:
        return key in self._active

    async def send_imessage(self, key: str, text: str) -> None:
        """直发 iMessage（不受告警级别限制，如每日复盘摘要；
        仍受同 key 节流与「未配置收件人跳过」约束）。"""
        await self._send_imessage(key, text)

    # ------------------------------------------------------------------
    # iMessage 通道
    # ------------------------------------------------------------------

    async def _send_imessage(self, key: str, text: str) -> None:
        """osascript 发 iMessage；未配置收件人/节流期内/失败均静默记 log。"""
        recipient = (self._imessage_recipient() if self._imessage_recipient
                     else "") or ""
        recipient = recipient.strip()
        if not recipient:
            return
        now = time.time()
        if now - self._imsg_last.get(key, 0) < self._throttle_s:
            return
        self._imsg_last[key] = now
        script = (f'tell application "Messages" to send '
                  f'"{_escape_applescript(text)}" '
                  f'to buddy "{_escape_applescript(recipient)}"')
        try:
            proc = await self._exec("osascript", "-e", script,
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=_IMESSAGE_TIMEOUT_S)
            if proc.returncode != 0:
                logger.warning("iMessage 发送失败 rc=%s key=%s",
                               proc.returncode, key)
            else:
                logger.info("iMessage 已发送 key=%s → %s", key, recipient)
        except Exception as e:
            logger.warning("iMessage 发送异常（静默降级）key=%s: %s", key, e)
