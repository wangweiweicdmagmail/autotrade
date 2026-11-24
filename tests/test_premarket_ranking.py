"""Premarket observation cohorts cannot loosen regular-session entry admission."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from console.services.rhythm import MarketRhythmReader
from execution.entry_admission import candidate_snapshot, ET
from .test_ranking_snapshot import raw, put, SYM, OTHER

PRE = int(datetime(2026, 11, 23, 9, 15, tzinfo=ET).timestamp())
OPEN = int(datetime(2026, 11, 23, 9, 30, tzinfo=ET).timestamp())


def pre(at=PRE, complete=False):
    frame=raw(at,complete=complete,with_beta=False)
    frame.update(day='2026-11-23',generated_at=at+10,universe=[SYM,OTHER],
                 ranking_session={'version':1,'phase':'premarket','cohort':'available','ready_after':at+10})
    frame['coverage']['received']=len(frame['series'])
    frame['ranks'][0]['ready']=True
    return frame


def test_partial_premarket_accepts_ready_members_and_lists_missing_without_enabling_orders(tmp_path):
    path=tmp_path/'frame.json';f=pre();f['scoring']['pending']=[{'symbol':OTHER,'reason':'ATR'}];put(path,f)
    reader=MarketRhythmReader(path,symbols=[SYM,OTHER]);out=reader.read(now=PRE+11)
    assert out['status']=='ready' and out['ranking_session']['phase']=='premarket'
    assert out['coverage']['missing']==[OTHER] and out['ranks'][0]['symbol']==SYM
    assert not out['batch_progress']['waiting']
    assert not candidate_snapshot(out,PRE+11)['ready']
    assert MarketRhythmReader(path,symbols=[SYM,OTHER]).read(now=PRE+12)['ranking_snapshot']==out['ranking_snapshot']


def test_premarket_coalesces_and_freezes_the_same_minute(tmp_path):
    path=tmp_path/'frame.json';f=pre();put(path,f);reader=MarketRhythmReader(path,symbols=[SYM,OTHER])
    assert reader.read(now=PRE+9)['status']=='waiting'
    first=reader.read(now=PRE+10)
    late=pre(complete=True);late['ranks'][0]['symbol']=OTHER;put(path,late)
    assert reader.read(now=PRE+30)['ranking_snapshot']==first['ranking_snapshot']
    assert reader.read(now=PRE+30)['ranks']==first['ranks']
    next_frame=pre(PRE+60,complete=True);next_frame['ranks'][0]['symbol']=OTHER;put(path,next_frame)
    next_out=reader.read(now=PRE+70)
    assert next_out['ranking_snapshot']['id']!=first['ranking_snapshot']['id']
    assert next_out['ranks'][0]['symbol']==OTHER


def test_open_rejects_even_fresh_complete_premarket_cohort_then_waits_for_regular_full_batch(tmp_path):
    path=tmp_path/'frame.json';put(path,pre(OPEN,complete=True))
    reader=MarketRhythmReader(path,symbols=[SYM,OTHER]);before=reader.read(now=OPEN+10)
    admitted=candidate_snapshot(before,OPEN+10)
    assert not admitted['ready'] and '09:45' in admitted['reason']
    regular=raw(OPEN+60,complete=False,with_beta=False);regular.update(day='2026-11-23',universe=[SYM,OTHER],
        ranking_session={'version':1,'phase':'regular','cohort':'complete','ready_after':OPEN+60})
    put(path,regular);waiting=reader.read(now=OPEN+75)
    assert waiting['ranking_snapshot']==before['ranking_snapshot'] and waiting['batch_progress']['waiting']
    put(path,{**regular,**raw(OPEN+60,with_beta=False), 'day':'2026-11-23','universe':[SYM,OTHER],
        'ranking_session':regular['ranking_session']})
    after=reader.read(now=OPEN+76)
    assert after['ranking_session']['phase']=='regular'
    assert after['ranking_snapshot']['id']!=before['ranking_snapshot']['id']
    assert after['coverage']['aligned']==after['coverage']['total']


@pytest.mark.parametrize('change',['wrong_clock','unknown_symbol','missing_list','bad_alignment','pending_rank','not_ready_rank'])
def test_premarket_metadata_cannot_hide_missing_or_unready_members(tmp_path,change):
    f=pre()
    if change=='wrong_clock':f['as_of']=OPEN+300;f['ranking_session']['ready_after']=OPEN+310
    elif change=='unknown_symbol':f['universe']=['UNEXPECTED',OTHER]
    elif change=='missing_list':f['coverage']['missing']=[]
    elif change=='bad_alignment':f['coverage']['aligned']=2
    elif change=='pending_rank':f['scoring']['pending']=[{'symbol':SYM}]
    else:f['ranks'][0]['ready']=False
    path=tmp_path/'frame.json';put(path,f)
    assert MarketRhythmReader(path,symbols=[SYM,OTHER]).read(now=OPEN+600)['status']=='waiting'


def test_archive_persists_premarket_cohort_but_never_accepts_regular_partial(tmp_path):
    from engine.research_store import ResearchStore
    from engine.research_archive import envelope
    store=ResearchStore(tmp_path/'research.sqlite3')
    f=pre();payload={k:f[k] for k in ('day','as_of','coverage','scoring','ranks','ranking_session','universe')}
    payload.update(batch_id='pre-batch',published_at=PRE+10,
        context={SYM:{'point':f['series'][SYM]['points'][-1]}})
    store.apply(envelope('ranking',payload,observed_at=PRE+11))
    assert store.rankings('2026-11-23')[0]['ranking_session']['phase']=='premarket'
    payload=deepcopy(payload);payload['batch_id']='bad-regular';payload['ranking_session']['phase']='regular'
    with pytest.raises(ValueError,match='完整'):store.apply(envelope('ranking',payload,observed_at=PRE+12))
