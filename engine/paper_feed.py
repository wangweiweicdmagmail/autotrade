"""Read-only Pine feed, deliberately separate from the production collector."""

import asyncio
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PinePaperFeed:
    def __init__(self, symbol, model, session):
        self.symbol, self.model, self.session = symbol, model, session
        self.data = {}
        self.process = None
        self.tasks = []
        self.error = ""
        self.fault = False
        self.received_at = 0

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "service/pine_paper_daemon.js"),
            "--symbol",
            self.symbol,
            "--model",
            self.model,
            "--session",
            self.session,
            cwd=str(ROOT / "service"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        )
        self.tasks = [
            asyncio.create_task(self._read()),
            asyncio.create_task(self._errors()),
        ]

    async def _read(self):
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
                if message.get("type") == "error":
                    self.error = message.get("reason", "数据订阅错误")
                    self.fault = True
                elif message.get("type") == "tick":
                    self.data = (message.get("symbols") or {}).get(self.symbol) or {}
                    self.received_at = time.time()
            except (ValueError, TypeError, AttributeError):
                self.error = "数据订阅消息无法解析"

    async def _errors(self):
        while await self.process.stderr.readline():
            # Do not reflect upstream diagnostic text or credentials to API responses.
            self.error = "数据服务有诊断信息；若无数据，请核对网络、登录及本地PineTS环境"

    def health(self, now=None):
        now = now or time.time()
        bar = self.data.get("bar") or {}
        observed = self.data.get("price_observed_at") or 0
        alive = bool(self.process and self.process.returncode is None)
        fresh = bool(
            alive
            and not self.fault
            and self.received_at
            and now - self.received_at <= 10
            and observed
            and -5 <= now - observed <= 45
            and bar.get("time")
            and -5 <= now - bar["time"] <= 600
        )
        return {
            "connected": alive,
            "fresh": fresh,
            "received_at": self.received_at,
            "observed_at": observed,
            "error": self.error,
            "reason": "" if fresh else "等待新鲜的 5 分钟行情；旧快照不会触发模拟订单",
        }

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
