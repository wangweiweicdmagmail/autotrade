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
