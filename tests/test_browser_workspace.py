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
    monkeypatch.setattr(workspace, "workspace_pages", lambda *_args: (
        {"title": 'autotrade "quoted" title'},
        {"id": "chart-tab", "url": "https://cn.tradingview.com/chart/id/"}))
    activations = []
    monkeypatch.setattr(workspace, "activate_page", lambda *args: activations.append(args))

    class Process:
        def __init__(self, payload):
            self.payload = payload
            self.returncode = payload[2]

        async def communicate(self):
            return self.payload[:2]

    async def spawn(*args, **_kwargs):
        if args[0] != "lsof":
            assert activations == [("http://localhost:9333", "chart-tab")]
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
    assert commands[1][-2:] == ('autotrade "quoted" title', "127.0.0.1:8000")


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
        "id": "chart-tab", "url": "https://cn.tradingview.com/chart/id/", "title": "Chart"}))
    monkeypatch.setattr(workspace, "activate_page", lambda *_: None)

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


def test_activate_chart_uses_existing_tab_and_escapes_id(monkeypatch):
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return b"Target activated"
    def open_tab(url, **kwargs):
        requests.append((url, kwargs))
        return Response()
    monkeypatch.setattr(workspace, "build_opener", lambda *_: type("Opener", (), {"open": staticmethod(open_tab)})())
    workspace.activate_page("http://localhost:9222", "chart/id")
    assert requests == [("http://localhost:9222/json/activate/chart%2Fid", {"timeout": 3})]


def test_independent_windows_reuse_exact_pages_and_create_only_missing_in_new_windows(monkeypatch):
    from controller import tv_cdp, window_chart
    urls=['https://cn.tradingview.com/chart/eight/', 'https://cn.tradingview.com/chart/four/', 'http://127.0.0.1:8000/']
    pages=[{'id':'existing','url':urls[0]}];commands=[];windows={'existing':1}
    class Page:
        def __init__(self,endpoint,*,target_id):self.id=target_id
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def window_id(self):return windows[self.id]
        async def _send_cdp(self,method,params):
            commands.append((method,params));target='new-'+str(len(commands))
            assert method=='Target.createTarget' and params['newWindow']
            pages.append({'id':target,'url':params['url']});windows[target]=len(commands)+1
            return {'targetId':target}
    monkeypatch.setattr(window_chart,'_pages',lambda _:pages)
    monkeypatch.setattr(tv_cdp,'TradingViewCDPController',Page)
    result=run(workspace.ensure_independent_windows('http://localhost:9222',urls))
    assert result[urls[0]]=='existing' and len(commands)==2
    assert run(workspace.ensure_independent_windows('http://localhost:9222',urls))==result
    assert len(commands)==2
    windows['new-1']=1
    with pytest.raises(RuntimeError,match='三个独立窗口'):
        run(workspace.ensure_independent_windows('http://localhost:9222',urls))
    assert len(commands)==2  # Never close/reload a chart to force separation.
