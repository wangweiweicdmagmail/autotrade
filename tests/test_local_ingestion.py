"""Retired cloud collectors cannot open connections or silently use mock prices."""
import pytest
import config
from engine.cloud_collector import CloudDataCollector
from engine.data_collector import TradingViewDataCollector, build_collector, MockDataCollector


@pytest.mark.parametrize('collector', [CloudDataCollector, TradingViewDataCollector])
def test_retired_collectors_fail_before_starting_any_io(collector):
    with pytest.raises(RuntimeError, match='退役'):
        collector(['NASDAQ:QQQ'])


@pytest.mark.parametrize('source', ['cloud', 'ws', 'retired', 'unknown'])
def test_legacy_entry_does_not_silently_fall_back_to_mock(source, monkeypatch):
    monkeypatch.setattr(config, 'DATA_SOURCE', source)
    with pytest.raises(RuntimeError, match='退役'):
        build_collector(['NASDAQ:QQQ'])


def test_only_explicit_mock_mode_is_allowed_in_legacy_demo(monkeypatch):
    monkeypatch.setattr(config, 'DATA_SOURCE', 'mock')
    assert isinstance(build_collector(['NASDAQ:QQQ']), MockDataCollector)


def test_historical_research_preserves_default_session_symbols(monkeypatch):
    import asyncio
    import json
    from pathlib import Path
    from types import SimpleNamespace
    import backtest_trend
    commands = []
    rows = [{'time': 1700000000, 'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1}]
    async def start(*args, **kwargs):
        commands.append(args)
        Path(args[args.index('--out') + 1]).write_text(json.dumps({'CAPITALCOM:NAS100': rows}))
        async def communicate(): return b'', b''
        return SimpleNamespace(returncode=0, communicate=communicate)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    assert asyncio.run(backtest_trend.fetch_history('CAPITALCOM:NAS100', 2, '2026-11-20')) == rows
    args = commands[0]
    defaults = args[args.index('--default-session-symbols') + 1].split(',')
    assert set(defaults) == set(config.TV_DEFAULT_SESSION_SYMBOLS)
    assert 'CAPITALCOM:NAS100' in defaults
