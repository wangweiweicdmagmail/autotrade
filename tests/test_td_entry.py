"""Retired TD-board entry cannot silently fall back to a trend order."""
import pytest
from pydantic import ValidationError
from console.schemas import LimitStopOrderRequest
from execution.entry_admission import candidate_snapshot,check_candidate
from .test_candidate_entry_admission import frame,market,NOW,SYM
from .test_risk_exposure import make_mgr
from .conftest import run


@pytest.mark.parametrize('ranking',['td','unknown'])
def test_removed_ranking_source_is_rejected_by_http_contract(ranking):
    with pytest.raises(ValidationError):
        LimitStopOrderRequest(symbol=SYM,limit_px=100,stop_px=99,candidate_ranking=ranking)


def test_removed_td_board_cannot_get_an_admission():
    snapshot=candidate_snapshot(frame(),NOW,market(),ranking='td')
    assert not snapshot['ready'] and not check_candidate(snapshot,SYM,'long')['ok']


@pytest.mark.parametrize('manual',[True,False])
def test_order_manager_rejects_stale_td_board_clients_before_any_gateway_call(tmp_path,manual):
    mgr=make_mgr(tmp_path)
    mgr.entry_admission=lambda *a,**kw:pytest.fail('Retired source must be rejected first')
    lot,reason=run(mgr.place_limit_stop_lot(SYM,'long',1,100,99,entry_order_type='MKT',
        manual_entry=manual,signal_snapshot={'candidate_ranking':'td'}))
    assert lot is None and 'TD榜已移除' in reason
