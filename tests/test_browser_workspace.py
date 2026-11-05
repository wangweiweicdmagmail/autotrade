"""Desktop arrangement tests use metadata and process stubs only."""

import json

import pytest

from controller import browser_workspace as workspace
from .conftest import run


def test_workspace_metadata_matches_real_chart_host(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps([
                {"type": "page", "id": "console", "url": "http://127.0.0.1:8000/"},
                {"type": "page", "id": "fake", "url": "https://nottradingview.com/chart/id/"},
                {"type": "page", "id": "chart", "url": "https://cn.tradingview.com/chart/id/"},
            ]).encode()

    monkeypatch.setattr(workspace, "build_opener", lambda *_args: type(
        "Opener", (), {"open": lambda *_args, **_kwargs: Response()})())
    left, right = workspace.workspace_pages("http://localhost:9222", "http://127.0.0.1:8000/")
    assert (left["id"], right["id"]) == ("console", "chart")
    with pytest.raises(RuntimeError, match="尚未就绪"):
        workspace.workspace_pages("http://localhost:9222", "http://127.0.0.1:9000/")


@pytest.mark.parametrize("result,error", [
    ((b"split-ready\n", b"", 0), None),
    ((b"", b"assistive access denied -1719", 1), "辅助功能"),
    ((b"", b"split was not confirmed", 1), "not confirmed"),
])
def test_split_targets_listener_pid_and_verifies_result(monkeypatch, result, error):
    commands = []
    monkeypatch.setattr(workspace, "workspace_pages", lambda *_args: ({}, {
        "title": 'NVDA "quoted" title', "url": "https://cn.tradingview.com/chart/id/"}))

    class Process:
        def __init__(self, payload):
            self.payload = payload
            self.returncode = payload[2]

        async def communicate(self):
            return self.payload[:2]

    async def spawn(*args, **_kwargs):
        commands.append(args)
        return Process((b"321\n321\n", b"", 0) if args[0] == "lsof" else result)

    monkeypatch.setattr(workspace.asyncio, "create_subprocess_exec", spawn)
    call = workspace.arrange_split("http://localhost:9333", "http://127.0.0.1:8000/")
    if error:
        with pytest.raises(RuntimeError, match=error):
            run(call)
    else:
        run(call)
    assert "-iTCP:9333" in commands[0]
    assert commands[1][2] == "321"
    assert commands[1][-2] == 'NVDA "quoted" title'


def test_ambiguous_listener_never_sends_ui_actions(monkeypatch):
    monkeypatch.setattr(workspace, "workspace_pages", lambda *_args: ({}, {}))
    commands = []

    class Process:
        async def communicate(self):
            return b"123\n456\n", b""

    async def spawn(*args, **_kwargs):
        commands.append(args)
        return Process()

    monkeypatch.setattr(workspace.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="唯一确定"):
        run(workspace.arrange_split("http://localhost:9222", "http://127.0.0.1:8000/"))
    assert len(commands) == 1


def test_remote_endpoint_never_targets_local_browser():
    with pytest.raises(RuntimeError, match="本机专用"):
        run(workspace.arrange_split("http://192.0.2.1:9222", "http://127.0.0.1:8000/"))


def test_split_timeout_kills_helper_and_reports_readable_reason(monkeypatch):
    monkeypatch.setattr(workspace, "workspace_pages", lambda *_args: ({}, {
        "url": "https://cn.tradingview.com/chart/id/", "title": "Chart"}))

    class Listener:
        async def communicate(self):
            return b"123\n", b""

    class StalledUI:
        killed = False
        reads = 0

        async def communicate(self):
            self.reads += 1
            if self.reads == 1:
                raise workspace.asyncio.TimeoutError()
            return b"", b""

        def kill(self):
            self.killed = True

    helper = StalledUI()

    async def spawn(*args, **_kwargs):
        return Listener() if args[0] == "lsof" else helper

    monkeypatch.setattr(workspace.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="超过 30 秒"):
        run(workspace.arrange_split("http://localhost:9222", "http://127.0.0.1:8000/"))
    assert helper.killed and helper.reads == 2
