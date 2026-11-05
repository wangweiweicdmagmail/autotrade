"""Coordinate order commands on the existing per-lot lock.

An exit/cancel invalidates queued lower-priority commands. It cannot undo a
broker request already in flight. Broker fills are facts, never queued commands.
No ordering is invented between human, risk and Pine exits: all are terminal
intents and retain arrival order, with the lot lifecycle checked inside the lock.
"""

import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import wraps

placement_received_at = ContextVar("placement_received_at", default=None)


def serialized_placement(function):
    """Keep account risk check and accepted-entry registration in one critical section."""

    @wraps(function)
    async def wrapped(manager, *args, **kwargs):
        token = placement_received_at.set(time.time())
        try:
            async with manager._placement_lock:
                return await function(manager, *args, **kwargs)
        finally:
            placement_received_at.reset(token)

    return wrapped


class OperationKind(str, Enum):
    EXIT = "exit"
    CANCEL = "cancel"
    REDUCE = "reduce"
    STOP = "stop"
    REPRICE = "reprice"


@dataclass(frozen=True)
class OrderCommand:
    lot_id: str
    kind: OperationKind
    submitted_at: float
    exit_generation: int


@dataclass(frozen=True)
class OperationDecision:
    command: OrderCommand
    allowed: bool
    reason: str = ""


@dataclass
class _Scope:
    generation: int = 0
    terminal_requests: int = 0
    users: int = 0


class OrderOperationCoordinator:
    def __init__(self):
        self._scopes = {}

    @asynccontextmanager
    async def run(self, lot_id, kind: OperationKind, *, lock):
        scope = self._scopes.setdefault(lot_id, _Scope())
        scope.users += 1
        terminal = kind in (OperationKind.EXIT, OperationKind.CANCEL)
        if terminal:
            scope.generation += 1
            scope.terminal_requests += 1
        command = OrderCommand(lot_id, kind, time.monotonic(), scope.generation)
        try:
            async with lock:
                allowed = terminal or (
                    scope.terminal_requests == 0
                    and command.exit_generation == scope.generation
                )
                yield OperationDecision(
                    command,
                    allowed,
                    ""
                    if allowed
                    else "退出或撤单请求优先，已跳过排队中的改单/减仓操作",
                )
        finally:
            if terminal:
                scope.terminal_requests -= 1
            scope.users -= 1
            if not scope.users:
                self._scopes.pop(lot_id, None)
