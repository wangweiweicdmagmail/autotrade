"""system application functions and owned state. No console.app dependency."""

from fastapi.responses import FileResponse
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from typing import List
from fastapi import Request
import asyncio
import os
import signal
import time
from console.shared import logger


class SystemService:
    def __init__(self, context):
        self.context = context

    def _is_trading_hours(self) -> bool:
        """是否在美股常规交易时段（9:30-16:00 ET，周一~周五）。"""
        from zoneinfo import ZoneInfo
        import datetime as _dt

        now = _dt.datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= minutes < 16 * 60

    async def _tws_pids(self) -> List[int]:
        proc = await asyncio.create_subprocess_exec(
            "pgrep",
            "-f",
            self.context.system._TWS_PROCESS_PATTERN,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return sorted((int(pid) for pid in out.decode().split()))

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    async def _wait_tws_exit(self, pids: List[int], timeout: float) -> List[int]:
        deadline = time.monotonic() + timeout
        remaining = [pid for pid in pids if self.context.system._pid_alive(pid)]
        while remaining and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            remaining = [
                pid for pid in remaining if self.context.system._pid_alive(pid)
            ]
        return remaining

    async def _close_tws_process(self) -> bool:
        """关闭 TWS 主进程；先正常退出，超时后 TERM/KILL 确保不残留。"""
        pids = await self.context.system._tws_pids()
        if not pids:
            return True
        try:
            await self.context.desktop._osascript(
                'tell application "Trader Workstation" to quit'
            )
        except Exception as e:
            logger.warning("TWS 正常退出请求失败，将直接结束进程: %s", e)
        remaining = await self.context.system._wait_tws_exit(pids, 8.0)
        if remaining:
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            remaining = await self.context.system._wait_tws_exit(remaining, 3.0)
        if remaining:
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            remaining = await self.context.system._wait_tws_exit(remaining, 2.0)
        if remaining:
            logger.error("TWS 进程仍未退出: %s", remaining)
            return False
        logger.info("TWS 进程已关闭")
        return True

    def _schedule_console_exit(self) -> None:

        async def _exit() -> None:
            await asyncio.sleep(0.5)
            os.kill(os.getpid(), signal.SIGTERM)

        self.context.tasks.start("exit", lambda: _exit())

    async def api_system_shutdown(self, request: Request) -> dict:
        """仅本机：关交易门 → 停跟单 → 关 TWS → 退出控制台。"""
        if not self.context.clients._is_local(request):
            raise HTTPException(403, "仅本机（master）可关闭系统")
        if self.context.system._system_stopping:
            return {"ok": True, "stopping": True, "tws_closed": None, "errors": []}
        self.context.system._system_stopping = True
        logger.warning("收到手动关闭系统请求，停止全部 IBKR 控制")
        self.context.events.record_event("system_shutdown_requested")
        errors = []
        try:
            await self.context.gateway.disable_control()
        except Exception as e:
            logger.exception("关闭 IBKR 交易门异常")
            errors.append(f"IBKR API: {e}")
        try:
            await self.context.copy_supervisor.stop_all()
        except Exception as e:
            logger.exception("停止跟单进程异常")
            errors.append(f"跟单进程: {e}")
        try:
            tws_closed = await self.context.system._close_tws_process()
        except Exception as e:
            logger.exception("关闭 TWS 异常")
            tws_closed = False
            errors.append(f"TWS: {e}")
        self.context.events.record_event(
            "system_shutdown_ready", tws_closed=tws_closed, errors=errors
        )
        self.context.system._schedule_console_exit()
        return {
            "ok": tws_closed and (not errors),
            "stopping": True,
            "tws_closed": tws_closed,
            "errors": errors,
        }

    async def index(self) -> FileResponse:
        name = (
            "index.html"
            if self.context.lifecycle._startup_state["ready"]
            else "loading.html"
        )
        return FileResponse(
            self.context.system.STATIC_DIR / name, headers={"Cache-Control": "no-store"}
        )

    async def _no_cache_static(self, request, call_next):
        """静态文件禁缓存；系统关闭期间拒绝一切新的写请求。"""
        if (
            self.context.system._system_stopping
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and (request.url.path != "/api/system/shutdown")
        ):
            return JSONResponse(
                {"detail": "系统正在关闭，已停止交易控制"}, status_code=503
            )
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response
