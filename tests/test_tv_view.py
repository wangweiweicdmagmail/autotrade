"""Host chart routes stay local after LAN clients move to their own extensions."""

import io
import json
from zipfile import ZipFile

import pytest
from starlette.testclient import TestClient

from controller import window_chart
from .conftest import run


@pytest.mark.parametrize("path", ["/api/tv/show", "/api/tv/view"])
def test_host_chart_rejects_approved_remote_client(console_app, monkeypatch, path):
    calls = []

    async def fake_show(symbol, **window):
        calls.append((symbol, window))
        return {"ok": True, "shown": symbol}

    monkeypatch.setattr(console_app.desktop, "_tv_show_symbol", fake_show)
    master = TestClient(console_app.app, client=("127.0.0.1", 50000))
    remote = TestClient(console_app.app, client=("192.0.2.10", 50000))
    ticket = remote.post("/api/clients/register", json={"name": "alice"}).json()["token"]
    assert master.post(f"/api/clients/{ticket}/approve").status_code == 200

    request = {"symbol": "NASDAQ:NVDA"}
    window = {}
    if path.endswith("/view"):
        assert master.post(path, json=request).status_code == 400
        request["source_token"] = "a" * 32
        window = {"origin": "http://testserver", "source_token": "a" * 32}
    response = remote.post(path, json=request, headers={"X-Trader-Token": ticket})
    assert response.status_code == 403
    assert calls == []

    response = master.post(path, json=request)
    assert response.status_code == 200
    assert response.json()["shown"] == "NASDAQ:NVDA"
    assert calls == [("NASDAQ:NVDA", window)]


def test_extension_download_preconfigures_the_clients_console(console_app):
    browser = TestClient(console_app.app, base_url="http://192.0.2.10:8766")
    response = browser.get("/api/tv/extension.zip")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="autotrade-tv.zip"'
    assert response.headers["cache-control"] == "no-store"
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {f"autotrade-tv/{name}" for name in (
            "manifest.json", "defaults.json", "presence.json", "worker.js", "chart-grid.js",
            "console-bridge.js", "popup.html", "popup.js", "README.md",
        )}
        manifest = json.loads(archive.read("autotrade-tv/manifest.json"))
        assert manifest["web_accessible_resources"][0]["matches"] == ["http://192.0.2.10/*"]
        assert json.loads(archive.read("autotrade-tv/defaults.json")) == {"origin": "http://192.0.2.10:8766"}


def test_scoped_cdp_selects_the_originating_window_and_refuses_ambiguous_tabs(monkeypatch):
    origin = "http://127.0.0.1:8000"
    chart = "https://www.tradingview.com/chart/layout/"
    token = "a" * 32
    pages = [
        {"id": "other-chart", "url": chart}, {"id": "other-console", "url": origin + "/"},
        {"id": "same-chart", "url": chart}, {"id": "same-console", "url": origin + "/"},
    ]
    windows = {"other-chart": 1, "other-console": 1, "same-chart": 2, "same-console": 2}
    visible = {"same-chart"}
    evaluated = []

    class Controller:
        def __init__(self, endpoint, *, target_id):
            self.id = target_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def evaluate(self, expr):
            evaluated.append((self.id, expr))
            if expr.startswith("location.origin"):
                return self.id == "same-console" and token in expr
            return self.id in visible

        async def window_id(self):
            return windows[self.id]

    monkeypatch.setattr(window_chart, "_pages", lambda _: pages)
    monkeypatch.setattr(window_chart, "TradingViewCDPController", Controller)

    async def resolve():
        async with window_chart.same_window_chart("http://localhost:9222", origin, token) as (controller, address):
            return controller.id, address

    assert run(resolve()) == ("same-chart", chart)
    assert not any(name == "other-chart" for name, _ in evaluated)
    pages.append({"id": "second-chart", "url": chart})
    windows["second-chart"] = 2
    assert run(resolve()) == ("same-chart", chart)
    visible.clear()
    with pytest.raises(RuntimeError, match="多个 TradingView 图表"):
        run(resolve())
    windows.update({"same-chart": 1, "second-chart": 1})
    with pytest.raises(RuntimeError, match="当前 AutoTrade 的 Chrome 窗口"):
        run(resolve())


@pytest.mark.parametrize('path,body',[
    ('/api/tv/grid',{'symbols':['NASDAQ:S9','NASDAQ:S7','NASDAQ:S5','NASDAQ:S3','NASDAQ:S10','NASDAQ:S8','NASDAQ:S6','NASDAQ:S4']}),
    ('/api/tv/entry-markers',{}),
])
def test_grid_and_entry_markers_require_local_master_and_origin_window(console_app,monkeypatch,path,body):
    from contextlib import asynccontextmanager
    calls=[]
    class Chart:
        async def evaluate(self,script):
            calls.append(script)
            if 'return inspectTradingViewGrid(' in script:
                return {'ok':True,'panes':[{'ok':True}]*8}
            return {'ok':True,'marked':0}
    @asynccontextmanager
    async def chart(endpoint,origin,token,address=None):
        assert origin=='http://testserver' and token=='a'*32
        yield Chart(),'https://cn.tradingview.com/chart/test/'
    monkeypatch.setattr(window_chart,'same_window_chart',chart)
    monkeypatch.setattr(window_chart,'layout_chart',chart)
    master=TestClient(console_app.app,client=('127.0.0.1',5000))
    remote=TestClient(console_app.app,client=('192.0.2.10',5000))
    token=remote.post('/api/clients/register',json={'name':'gridclient'}).json()['token']
    assert master.post(f'/api/clients/{token}/approve').status_code==200
    req={**body,'source_token':'a'*32}
    assert remote.post(path,json=req,headers={'X-Trader-Token':token}).status_code==403
    assert calls==[]
    result=master.post(path,json=req)
    assert result.status_code==200 and result.json()['ok']
    assert len(calls)==(2 if path.endswith('entry-markers') else 1) and 'tv_paper_bridge' not in calls[0]


def test_grid_endpoint_refuses_a_legacy_six_symbol_request_before_chart_access(console_app,monkeypatch):
    def forbidden(*_):
        pytest.fail('invalid eight-pane request must not reach a chart')
    monkeypatch.setattr(window_chart,'same_window_chart',forbidden)
    client=TestClient(console_app.app,client=('127.0.0.1',5000))
    response=client.post('/api/tv/grid',json={'symbols':['NASDAQ:AMD']*6,'source_token':'a'*32})
    assert response.status_code==422


@pytest.mark.parametrize("scoped", [True, False])
def test_single_symbol_host_routes_share_the_sixth_cell_inspector_and_poll_without_resending(console_app, monkeypatch, scoped):
    from contextlib import asynccontextmanager
    from controller import tv_cdp

    calls, activations = [], []
    address = "https://cn.tradingview.com/chart/test/"
    wanted = ["CAPITALCOM:NAS100", "CAPITALCOM:SPX500", "NASDAQ:AMD",
              "NASDAQ:NVDA", "NASDAQ:MSFT", "NYSE:UNH"]

    class Chart:
        def __init__(self,*args,**kwargs):
            if kwargs:assert kwargs['target_id']=='eight'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def evaluate(self, script):
            if script == "location.origin + location.pathname":
                return address
            tail = script.rsplit("\nreturn inspectTradingViewGrid(", 1)[1].removesuffix(");})()")
            args = json.loads("[" + tail + "]")
            calls.append(args)
            return {"ok": len(calls) == 2, "pending": len(calls) == 1,
                    "cell": 5, "shown": "NYSE:UNH" if len(calls) == 2 else "NASDAQ:AAPL",
                    "panes": [{"symbol": symbol} for symbol in wanted]}

    @asynccontextmanager
    async def same_window(endpoint, origin, token):
        assert origin == "http://testserver" and token == "a" * 32
        yield Chart(), address

    async def ready():
        return None

    async def activate():
        activations.append(True)

    monkeypatch.setattr(window_chart, "same_window_chart", same_window)
    monkeypatch.setattr(tv_cdp, "TradingViewCDPController", Chart)
    monkeypatch.setattr(console_app.desktop,'_TV_CHART_URL',address)
    monkeypatch.setattr(window_chart,'_pages',lambda _:[{'id':'four','url':'https://cn.tradingview.com/chart/focus/'},{'id':'eight','url':address}])
    from controller import browser_workspace
    monkeypatch.setattr(browser_workspace,'activate_page',lambda endpoint,target: activations.append(target=='eight'))
    monkeypatch.setattr(console_app.desktop, "_tv_ensure_browser", ready)
    monkeypatch.setattr(console_app.desktop, "_tv_activate_window", activate)
    kwargs = {"origin": "http://testserver", "source_token": "a" * 32} if scoped else {}
    result = run(console_app.desktop._tv_show_symbol("nyse:unh", **kwargs))
    assert result["ok"] and result["cell"] == 5 and result["shown"] == "NYSE:UNH"
    assert calls == [["NYSE:UNH", address, True, None], ["NYSE:UNH", address, False, wanted]]
    assert activations == ([] if scoped else [True])


def test_host_sixth_cell_returns_layout_error_without_a_fallback_write(console_app, monkeypatch):
    from contextlib import asynccontextmanager
    calls = []

    class Chart:
        async def evaluate(self, script):
            calls.append(script)
            return {"ok": False, "reason": "请将当前 TradingView 布局设为两列三行六宫格"}

    @asynccontextmanager
    async def same_window(*_):
        yield Chart(), "https://cn.tradingview.com/chart/test/"

    monkeypatch.setattr(window_chart, "same_window_chart", same_window)
    result = run(console_app.desktop._tv_show_symbol("NASDAQ:AMD", origin="http://testserver", source_token="a" * 32))
    assert not result["ok"] and "六宫格" in result["reason"]
    assert len(calls) == 1


def test_explicit_chart_binding_is_scoped_to_its_console_and_never_falls_back(monkeypatch):
    origin='http://127.0.0.1:8000';address='https://cn.tradingview.com/chart/test/'
    pages=[{'id':'console-a','url':origin+'/'},{'id':'console-b','url':origin+'/'},
           {'id':'tv-bound','url':address},{'id':'tv-other','url':address}]
    windows={'console-a':1,'console-b':3,'tv-bound':2,'tv-other':1}
    class Controller:
        def __init__(self, endpoint, *, target_id): self.id=target_id
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        async def window_id(self): return windows[self.id]
        async def evaluate(self, script):
            if script=='location.origin + location.pathname': return address
            if script.startswith('location.origin'): return self.id=='console-a'
            return True
    monkeypatch.setattr(window_chart,'_pages',lambda _:pages)
    monkeypatch.setattr(window_chart,'TradingViewCDPController',Controller)
    bound=run(window_chart.bind_console_chart('http://localhost:9222',origin,'a'*32,'tv-bound'))
    assert bound=={'source_id':'console-a','chart_id':'tv-bound','address':address}
    bindings={'console-a':{'chart_id':'tv-bound','address':address}}
    async def resolve(mapping):
        async with window_chart.same_window_chart('http://localhost:9222',origin,'a'*32,bindings=mapping) as (tv,_):
            return tv.id
    assert run(resolve(bindings))=='tv-bound'
    assert run(resolve({}))=='tv-other'
    pages[:]=[p for p in pages if p['id']!='tv-bound']
    with pytest.raises(RuntimeError,match='已绑定'):
        run(resolve(bindings))


def test_chart_binding_persists_in_isolated_paths_and_rejects_remote_clients(console_app,monkeypatch):
    from console.services.desktop import DesktopService
    calls=[]
    async def bind(endpoint,origin,token,target):
        calls.append(target)
        return {'source_id':'console','chart_id':target,'address':'https://cn.tradingview.com/chart/test/'}
    monkeypatch.setattr(window_chart,'bind_console_chart',bind)
    master=TestClient(console_app.app,client=('127.0.0.1',5000))
    remote=TestClient(console_app.app,client=('192.0.2.10',5000))
    token=remote.post('/api/clients/register',json={'name':'binding-client'}).json()['token']
    assert master.post(f'/api/clients/{token}/approve').status_code==200
    body={'source_token':'a'*32,'target_id':'tv-chart'}
    assert remote.post('/api/tv/bind',json=body,headers={'X-Trader-Token':token}).status_code==403
    assert calls==[]
    assert master.post('/api/tv/bind',json=body).json()['ok']
    restored=DesktopService(console_app)
    assert restored._tv_bindings=={'console':{'chart_id':'tv-chart','address':'https://cn.tradingview.com/chart/test/'}}


def test_named_layout_selection_refuses_missing_or_duplicate_and_does_not_use_the_other_window(monkeypatch):
    origin='http://127.0.0.1:8000';eight='https://cn.tradingview.com/chart/eight/';four='https://cn.tradingview.com/chart/four/'
    pages=[{'id':'source','url':origin+'/'},{'id':'eight','url':eight},{'id':'four','url':four}]
    visited=[]
    class Page:
        def __init__(self,endpoint,*,target_id):self.id=target_id
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def window_id(self):return {'source':1,'eight':2,'four':3}.get(self.id,4)
        async def evaluate(self,expression):
            visited.append(self.id)
            return next(p['url'] for p in pages if p['id']==self.id) if expression=='location.origin + location.pathname' else self.id=='source'
    monkeypatch.setattr(window_chart,'_pages',lambda _:pages)
    monkeypatch.setattr(window_chart,'TradingViewCDPController',Page)
    async def resolve():
        async with window_chart.layout_chart('http://localhost:9222',origin,'a'*32,four) as (chart,address):
            return chart.id,address
    assert run(resolve())==('four',four);assert 'eight' not in visited
    pages.append({'id':'duplicate','url':four})
    with pytest.raises(RuntimeError,match='多个副本'):run(resolve())
    pages[:]=[p for p in pages if p['url']!=four]
    with pytest.raises(RuntimeError,match='指定TV布局未打开'):run(resolve())


def test_focus_grid_uses_exact_six_layout_and_validates_cell_count_before_access(console_app,monkeypatch):
    from contextlib import asynccontextmanager
    calls=[]
    class Chart:
        async def evaluate(self,script):calls.append(script);return {'ok':True,'panes':[{'ok':True}]*6}
    @asynccontextmanager
    async def layout(endpoint,origin,token,address):
        assert address==console_app.desktop._TV_FOCUS_CHART_URL
        yield Chart(),address
    monkeypatch.setattr(window_chart,'layout_chart',layout)
    client=TestClient(console_app.app,client=('127.0.0.1',5000))
    body={'layout':'focus','symbols':['NASDAQ:NVDA','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NYSE:UNH','NASDAQ:QQQ','AMEX:SPY'],'source_token':'a'*32}
    response=client.post('/api/tv/grid',json=body)
    assert response.status_code==200 and response.json()['ok'] and len(calls)==1
    assert client.post('/api/tv/grid',json={**body,'layout':'candidates'}).status_code==422
    assert client.post('/api/tv/grid',json={**body,'symbols':body['symbols'][:4]}).status_code==422
    assert ',null,"focus")' in calls[0]
    assert len(calls)==1
