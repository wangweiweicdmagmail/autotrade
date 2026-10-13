# -*- coding: utf-8 -*-
"""
Node 控制器 Python 适配器 (tv_control_client)
=============================================

通过常驻子进程（daemon 模式）调用 ``controller/tv_control.js``
（基于开源项目 tradesdontlie/tradingview-mcp 二次开发的 CDP 控制器），
为 Python 主流程提供与 ``controller/tv_cdp.py`` 一致的接口。

为什么用 daemon 模式：每次调用都新建 CDP 连接时，重连退避实测可达
40s+，无法支撑轮询主循环；常驻进程持有一条 CDP 连接，单次调用
毫秒级返回。

协议（stdin/stdout 按行 JSON）：
    请求:  {"id": 1, "cmd": "setGridSymbols", "args": ["NASDAQ:QQQ"]}
    响应:  {"id": 1, "result": {...}} 或 {"id": 1, "error": "..."}
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TV_CONTROL_JS = PROJECT_ROOT / "controller" / "tv_control.js"

CALL_TIMEOUT = 30.0


class NodeTVController:
    """
    Node CDP 控制器（daemon 模式）的 Python 封装。

    用法::

        async with NodeTVController() as ctl:
            report = await ctl.apply_grid(["NASDAQ:AAPL", ...])
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def __aenter__(self) -> "NodeTVController":
        if not TV_CONTROL_JS.exists():
            raise RuntimeError(f"控制器不存在: {TV_CONTROL_JS}")
        self._proc = await asyncio.create_subprocess_exec(
            "node", str(TV_CONTROL_JS), "daemon",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        # 连接自检：listPanes 失败说明 TV 未以调试端口启动
        panes = await self.list_panes()
        logger.info("CDP 控制器就绪: %d 个 pane (source=%s)",
                    panes.get("count", 0), panes.get("source"))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    # ------------------------------------------------------------------
    # JSON-RPC 收发
    # ------------------------------------------------------------------

    async def _call(self, cmd: str, args=None, retries: int = 1) -> dict:
        """发送 JSON-RPC 请求并等待响应；超时按 retries 重试。"""
        assert self._proc and self._proc.stdin, "控制器未启动"
        for attempt in range(retries + 1):
            self._req_id += 1
            req_id = self._req_id
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = fut
            self._proc.stdin.write(
                (json.dumps({"id": req_id, "cmd": cmd, "args": args}) + "\n")
                .encode("utf-8"))
            await self._proc.stdin.drain()
            try:
                reply = await asyncio.wait_for(fut, timeout=CALL_TIMEOUT)
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                if attempt < retries:
                    logger.warning("tv_control %s 超时，重试 (%d/%d)",
                                   cmd, attempt + 1, retries)
                    continue
                raise RuntimeError(f"tv_control {cmd} 超时（重试 {retries} 次后）")
            if "error" in reply:
                raise RuntimeError(f"tv_control {cmd} 失败: {reply['error']}")
            return reply["result"]

    async def _reader_loop(self) -> None:
        """按行读取 daemon 响应，分发到等待中的 Future。"""
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break  # 进程退出
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # 与 TradingViewCDPController 一致的对外接口
    # ------------------------------------------------------------------

    async def list_panes(self) -> dict:
        """列出视觉行优先的 pane 列表。"""
        return await self._call("listPanes")

    async def apply_grid(self, symbols: List[str],
                         center_view: bool = True) -> dict:
        """
        按视觉行优先灌入 symbol 列表，可选居中视图。
        回读校验只比 ticker（容忍 BATS 前缀归一化），由 JS 侧完成。
        """
        if not symbols:
            logger.warning("apply_grid 收到空列表，跳过本轮 UI 刷新")
            return {"skipped": True}
        report = await self._call("setGridSymbols", list(symbols))
        ok = sum(1 for p in report.get("panes", []) if p.get("verified"))
        logger.info("八宫格灌入: %d/%d 校验通过 (source=%s)",
                    ok, report.get("applied", 0), report.get("source"))
        if center_view:
            report["centered"] = await self.center_all()
        return report

    async def center_all(self) -> dict:
        """全格最新 K 线居中。"""
        return await self._call("centerAll")

    # ------------------------------------------------------------------
    # 提案卡（人机确认交互）
    # ------------------------------------------------------------------

    async def show_proposals(self, proposals: List[dict]) -> dict:
        """
        批量呈现交易提案：宫格标的按卡片顺序灌入，右侧停靠栏纵向堆卡。
        proposals: [{symbol, side('long'/'short'), entry, stop, qty, risk, reason?}]
        """
        if not proposals:
            return {"skipped": True}
        report = await self._call("showProposals", proposals)
        logger.info("提案卡呈现: %d 张 (gridApplied=%d, verified=%s)",
                    len(report.get("cards", [])),
                    report.get("gridApplied", 0), report.get("gridVerified"))
        return report

    async def poll_decisions(self) -> List[dict]:
        """取走全部人工决策（批准/拒绝 + 修改后的仓位/止损），卡片自动移除。"""
        report = await self._call("pollDecisions")
        return report.get("decisions", [])

    async def clear_proposals(self) -> dict:
        """清空停靠栏全部提案卡。"""
        return await self._call("clearProposals")
