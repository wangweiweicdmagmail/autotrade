"""TV first-run login, isolated cookies/CDP and temporary credential files only."""

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from controller import tv_credentials as credentials
from console.services import tv_login
from .conftest import run


SESSION = {'sessionid': 'test-session-value', 'sessionid_sign': 'test-signature-value'}


def cookies(domain='.tradingview.com', **extra):
    return [dict(name=name, value=value, domain=domain, path='/', expires=-1, **extra)
            for name, value in SESSION.items()]


@pytest.mark.parametrize('value', [None, {}, {'sessionid': 'one'},
    {'sessionid': 123, 'sessionid_sign': 'two'},
    {'sessionid': 'line\nbreak', 'sessionid_sign': 'two'}])
def test_incomplete_or_unsafe_values_are_not_credentials(value):
    assert not credentials.valid_credentials(value)


def test_cookie_pairs_require_real_tv_scope_and_complete_unexpired_session():
    assert credentials.credentials_from_cookies(cookies(), now=100) == SESSION
    assert credentials.credentials_from_cookies(cookies('tradingview.com.evil.test')) is None
    assert credentials.credentials_from_cookies(cookies('eviltradingview.com')) is None
    assert credentials.credentials_from_cookies(cookies()[:1]) is None
    assert credentials.credentials_from_cookies([cookies()[0], cookies('cn.tradingview.com')[1]]) is None
    for expiry in (0, 99, 100, float('nan'), True):
        values = cookies()
        values[0]['expires'] = expiry
        assert credentials.credentials_from_cookies(values, now=100) is None
    assert credentials.credentials_from_cookies(cookies(partitionKey={'topLevelSite': 'https://other.test'})) is None
    assert credentials.credentials_from_cookies(None) is None
    conflicting = cookies('cn.tradingview.com')
    conflicting[0]['value'] = 'another-account'
    assert credentials.credentials_from_cookies(cookies() + conflicting) is None


def test_atomic_private_save_and_failed_replace_preserve_previous_file(tmp_path, monkeypatch):
    path = tmp_path / 'creds.json'
    assert not credentials.credentials_present(path)
    credentials.save_credentials(path, SESSION)
    assert credentials.credentials_present(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == SESSION
    original = path.read_bytes()

    def fail(source, target):
        assert os.stat(source).st_mode & 0o777 == 0o600
        raise OSError('write failed')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        credentials.save_credentials(path, dict(SESSION, sessionid='replacement'))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('.tv-session-*'))


def test_malformed_credentials_do_not_count_as_configured(tmp_path):
    path = tmp_path / 'creds.json'
    for value in ('invalid JSON', '{}', '[]', '\ufffd'):
        path.write_text(value)
        assert not credentials.credentials_present(path)


def test_permission_failure_closes_private_temporary_file(tmp_path, monkeypatch):
    descriptors = []
    def fail(descriptor, mode):
        descriptors.append(descriptor)
        raise PermissionError('permission failed')
    monkeypatch.setattr(os, 'fchmod', fail)
    with pytest.raises(PermissionError):
        credentials.save_credentials(tmp_path / 'creds.json', SESSION)
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not list(tmp_path.iterdir())


def test_cdp_only_reads_tv_cookies_from_a_tv_page(monkeypatch):
    from controller import window_chart, tv_cdp
    commands = []
    monkeypatch.setattr(window_chart, '_pages', lambda _: [
        {'id': 'evil', 'url': 'https://tradingview.com.evil.test/chart/'},
        {'id': 'console', 'url': 'http://127.0.0.1:8000/'},
        {'id': 'tv', 'url': credentials.LOGIN_URL},
    ])
    class Chrome:
        def __init__(self, endpoint, *, target_id):
            assert target_id == 'tv'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def _send_cdp(self, method, params):
            commands.append((method, params))
            return {'cookies': cookies()}
    monkeypatch.setattr(tv_cdp, 'TradingViewCDPController', Chrome)
    assert run(credentials.read_browser_credentials('http://localhost:9222')) == SESSION
    assert commands == [('Network.getCookies', {'urls': [
        'https://www.tradingview.com/', 'https://cn.tradingview.com/']})]
    monkeypatch.setattr(window_chart, '_pages', lambda _: [{'id': 'other', 'url': 'https://example.org'}])
    assert run(credentials.read_browser_credentials('http://localhost:9222')) is None


@pytest.mark.parametrize('endpoint', ['http://192.168.1.2:9222', 'https://localhost:9222',
                                    'http://localhost:9222/other', 'http://user@localhost:9222'])
def test_credentials_never_read_a_remote_debug_browser(endpoint):
    with pytest.raises(ValueError):
        run(credentials.read_browser_credentials(endpoint))


def test_open_login_reuses_login_page_without_navigating_a_chart(monkeypatch):
    from controller import window_chart, tv_cdp
    commands = []
    pages = [{'id': 'chart', 'url': 'https://cn.tradingview.com/chart/test/'},
             {'id': 'login', 'url': credentials.LOGIN_URL}]
    monkeypatch.setattr(window_chart, '_pages', lambda _: pages)
    class Chrome:
        def __init__(self, endpoint, *, target_id): self.target_id = target_id
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def _send_cdp(self, method, params): commands.append((method, params))
    monkeypatch.setattr(tv_cdp, 'TradingViewCDPController', Chrome)
    run(credentials.open_login_page('http://localhost:9222'))
    assert commands == [('Target.activateTarget', {'targetId': 'login'})]
    pages.pop()
    run(credentials.open_login_page('http://localhost:9222'))
    assert commands[-1] == ('Target.createTarget', {'url': credentials.LOGIN_URL})


@pytest.fixture
def service(console_app, monkeypatch):
    queued = []
    monkeypatch.setattr(console_app.tasks, 'start', lambda name, factory: queued.append((name, factory)))
    service = console_app.tv_login
    assert service.credentials_path.is_relative_to(console_app.paths.data_dir)
    return service


def test_missing_login_does_not_launch_daemon_and_hides_false_market_wait(service, monkeypatch):
    async def forbidden(): raise AssertionError('must not launch without login')
    monkeypatch.setattr(service.context.collector, 'start', forbidden)
    run(service.start())
    assert service.waiting
    payload = service.context.lifecycle._startup_status()
    assert payload['tv_login']['required']
    assert not payload['workspace_ready']
    assert payload['market_pending'] == []
    assert '行情订阅未完成' not in payload['error']
    market = next(row for row in payload['stages'] if row['key'] == 'market')
    assert '等待 TradingView 登录' in market['detail']
    assert '0/' not in market['detail']


def test_valid_saved_credentials_start_normally_once(service, monkeypatch):
    started = []
    credentials.save_credentials(service.credentials_path, SESSION)
    async def start(): started.append(True)
    monkeypatch.setattr(service.context.collector, 'start', start)
    async def exercise():
        await service.start()
        await service.start()
    run(exercise())
    assert started == [True]
    assert service.state == 'configured' and not service.waiting


def test_successful_login_saves_then_starts_market_once_without_exposing_secret(service, monkeypatch):
    calls = []
    async def capture(_):
        calls.append('capture')
        return SESSION
    async def start():
        assert credentials.credentials_present(service.credentials_path)
        calls.append('start')
    monkeypatch.setattr(tv_login, 'read_browser_credentials', capture)
    monkeypatch.setattr(service.context.collector, 'start', start)
    async def exercise():
        await service.start()
        assert await service._step()
        assert await service._step()
    run(exercise())
    assert calls == ['capture', 'start']
    assert not service.waiting
    public = json.dumps(service.context.lifecycle._startup_status())
    assert all(secret not in public for secret in SESSION.values())
    assert service.context.collector.credentials_path == service.credentials_path


def test_save_failure_keeps_login_waiting_and_does_not_launch_market(service, monkeypatch):
    async def capture(_): return SESSION
    async def forbidden(): raise AssertionError('save failed')
    def fail(*args): raise OSError('private exception must not reach HTTP')
    monkeypatch.setattr(tv_login, 'read_browser_credentials', capture)
    monkeypatch.setattr(tv_login, 'save_credentials', fail)
    monkeypatch.setattr(service.context.collector, 'start', forbidden)
    async def exercise():
        await service.start()
        assert not await service._step()
    run(exercise())
    assert service.state == 'save_error'
    assert 'private exception' not in json.dumps(service.snapshot())
    assert not service.credentials_path.exists()


def test_browser_not_ready_and_incomplete_login_are_recoverable(service, monkeypatch):
    async def missing(_): raise ConnectionError('private connection detail')
    monkeypatch.setattr(tv_login, 'read_browser_credentials', missing)
    async def exercise():
        await service.start()
        assert not await service._step()
        assert service.state == 'browser_wait'
        async def pending(_): return None
        monkeypatch.setattr(tv_login, 'read_browser_credentials', pending)
        assert not await service._step()
        assert service.state == 'login_required'
    run(exercise())
    assert not service.credentials_path.exists()


def test_login_writes_once_if_market_launch_needs_retry(service, monkeypatch):
    attempts = []
    async def capture(_): return SESSION
    async def start():
        attempts.append(True)
        if len(attempts) == 1: raise OSError('Node missing')
    monkeypatch.setattr(tv_login, 'read_browser_credentials', capture)
    monkeypatch.setattr(service.context.collector, 'start', start)
    async def exercise():
        await service.start()
        assert not await service._step()
        assert service.state == 'market_error'
        assert await service._step()
    run(exercise())
    assert len(attempts) == 2
    assert service.state == 'configured'


def test_login_longer_than_startup_timeout_does_not_report_market_outage(service, monkeypatch):
    context = service.context
    clock = {'time': 0, 'sleeps': 0}
    notices = []
    async def sleep(_):
        clock['time'] += 5
        clock['sleeps'] += 1
        if clock['sleeps'] == 65: service.state = 'configured'
    async def notify(*args): notices.append(args)
    monkeypatch.setattr(context.desktop._tv_startup_check.__globals__['time'], 'time', lambda: clock['time'])
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    monkeypatch.setattr(context.config, 'WATCHLIST', ['NASDAQ:QQQ'])
    monkeypatch.setattr(context.collector, 'latest', lambda: {'NASDAQ:QQQ': {'ts_c': {'trend_state': 1}}})
    monkeypatch.setattr(context.notifier, 'notify', notify)
    async def exercise():
        await service.start()
        await context.desktop._tv_startup_check()
    run(exercise())
    assert clock['time'] > 240
    assert len(notices) == 1 and notices[0][1] == 'info'


def test_expected_login_wait_does_not_emit_data_stale_alert(service, monkeypatch):
    async def exercise():
        await service.start()
        assert await service.context.monitoring._health_check_once(None) is False
    monkeypatch.setattr(service.context.collector, 'data_health', lambda: (_ for _ in ()).throw(AssertionError('no market yet')))
    run(exercise())


def test_only_local_startup_page_can_open_login(service, monkeypatch):
    calls = []
    async def open_login():
        calls.append(True)
        return {'ok': True, 'state': 'login_required'}
    monkeypatch.setattr(service, 'open_login', open_login)
    remote = TestClient(service.context.app)
    local = TestClient(service.context.app, client=('127.0.0.1', 50000))
    headers = {'X-Autotrade-Action': 'tv-login'}
    assert remote.post('/api/startup/tv-login', headers=headers).status_code == 403
    assert local.post('/api/startup/tv-login').status_code == 403
    assert local.post('/api/startup/tv-login', headers=headers).json()['ok']
    assert calls == [True]
    assert remote.get('/api/startup').json()['tv_login']['local_actions'] is False
    assert local.get('/api/startup').json()['tv_login']['local_actions'] is True


def test_background_login_continues_loading_and_enters_console_without_manual_script(console_app, monkeypatch):
    context = console_app
    seen = []
    original_sleep = asyncio.sleep
    async def fast_sleep(_): await original_sleep(0)
    async def capture(_):
        seen.append('check')
        return SESSION if len(seen) >= 3 else None
    async def start():
        assert credentials.credentials_present(context.tv_login.credentials_path)
        from types import SimpleNamespace
        context.collector._running = True
        context.collector._proc = SimpleNamespace(returncode=None)
        context.collector._snapshot = {'NASDAQ:QQQ': {'ts_c': {'trend_state': 1}}}
    monkeypatch.setattr(tv_login, 'read_browser_credentials', capture)
    monkeypatch.setattr(context.collector, 'start', start)
    monkeypatch.setattr(context.config, 'WATCHLIST', ['NASDAQ:QQQ'])
    monkeypatch.setattr(context.desktop, '_CONSOLE_AUTO_OPEN_BROWSER', False)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    context.lifecycle._startup_state['core_ready'] = True
    async def exercise():
        await context.tv_login.start()
        assert context.tv_login.waiting
        await asyncio.wait_for(context.desktop._startup_prepare(), timeout=1)
        assert context.lifecycle._startup_state['ready']
        await context.tv_login.stop()
    run(exercise())
    assert len(seen) == 3
    assert context.tv_login.state == 'configured'
    assert 'id="app" v-cloak' in TestClient(context.app).get('/').text


def test_live_launcher_does_not_count_login_time_as_market_timeout(monkeypatch, capsys):
    from pathlib import Path
    import io
    import time
    import urllib.request
    clock = {'time': 0, 'polls': 0}
    def open_url(url, timeout):
        if url.endswith('/api/status'):
            payload = {'connected': True, 'equity': 1000, 'execution_enabled': False,
                       'open_symbols': 0, 'total_risk_usd': 0, 'max_total_risk_usd': 2200}
        else:
            clock['polls'] += 1
            payload = {'tv_login': {'required': clock['polls'] <= 3, 'state': 'login_required'},
                       'market_pending': [], 'market_progress': {'confirmed': 1, 'total': 1, 'subscribed': 1}}
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        return Response(json.dumps(payload).encode())
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: type('Opener', (), {'open': staticmethod(open_url)})())
    monkeypatch.setattr(time, 'time', lambda: clock['time'])
    monkeypatch.setattr(time, 'sleep', lambda _: clock.update(time=clock['time'] + 180))
    script = (Path(__file__).resolve().parents[1] / 'restart_live.sh').read_text()
    code = script.split('"$PYTHON_BIN" - <<\'EOF\'\n', 1)[1].split('\nEOF', 1)[0]
    with pytest.raises(SystemExit) as exit:
        exec(compile(code, 'restart_live.sh:readiness', 'exec'), {})
    assert exit.value.code == 0
    assert clock['time'] > 300 and clock['polls'] == 4
    assert '等待 TradingView 登录' in capsys.readouterr().out


def test_shutdown_cancels_login_detection_without_writing_credentials(console_app, monkeypatch):
    async def exercise():
        detecting = asyncio.Event()
        async def capture(_):
            detecting.set()
            await asyncio.Future()
        monkeypatch.setattr(tv_login, 'read_browser_credentials', capture)
        await console_app.tv_login.start()
        task = console_app.tv_login._task
        await asyncio.wait_for(detecting.wait(), timeout=1)
        await console_app.tv_login.stop()
        assert task.cancelled()
        assert not console_app.tv_login.credentials_path.exists()
    run(exercise())
