import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from engine.research_methods import ResearchNotes
from engine.research_review import ResearchReview, actor
from execution.state_store import ExecutionStateStore
from datetime import datetime
from engine.research_calendar import ET
OPEN = int(datetime(2026,11,20,9,30,tzinfo=ET).timestamp())


def ledger(tmp_path, rows):
    store=ExecutionStateStore(tmp_path/'lots_state.json')
    store.save({'lots':rows,'pyramid_groups':{}})
    return store


def lot(key='one',**values):
    return {'lot_id':key,'symbol':'NASDAQ:TEST','side':'long','qty':100,'avg_fill':100,
            'entry_time':OPEN+30,'closed_at':OPEN+360,'state':'CLOSED','init_stop_px':99,
            'entry_cumulative_qty':40,'realized_pnl':20,'mfe':30,'mae':-10,
            'exit_px':100.5,'exit_reason':'manual','signal_snapshot':{},**values}


def test_notes_are_durable_deduplicate_questions_and_preserve_answers(tmp_path):
    notes=ResearchNotes(tmp_path/'notes')
    def create(i):return notes.question(trade_id='g1',action_id='a1',facts={'price':100},prompt='为什么？')
    with ThreadPoolExecutor(max_workers=4) as pool:keys=list(pool.map(create,range(12)))
    assert len(set(keys))==1
    assert len(notes.questions())==1
    notes.update_question(keys[0],status='asked')
    assert len(notes.questions('pending'))==1
    with pytest.raises(ValueError):notes.update_question(keys[0],status='answered',text='看大盘',source=None)
    notes.update_question(keys[0],status='answered',text='大盘支撑，个股支撑',source='user message 1')
    restored=ResearchNotes(tmp_path/'notes')
    assert not restored.questions('pending')
    assert restored.questions()[0]['answer']=='大盘支撑，个股支撑'
    with pytest.raises(ValueError):restored.update_question(keys[0],status='asked')
    with pytest.raises(ValueError):restored.method(rule='未经验证',source='agent',question_ids=[],status='validated')
    restored.method(rule='双重支撑',source='user message 1',question_ids=[keys[0]],status='user_confirmed')
    restored.method(rule='仅趋势明确时双重支撑',source='user message 2',question_ids=[keys[0]],method_id=restored.methods()[0]['method_id'])
    assert [r['version'] for r in restored.methods()]==[1,2]


def test_projection_fill_identity_questions_and_partial_quantity_risk(tmp_path):
    ledger(tmp_path,[lot()])
    path=tmp_path/'events-20990101.jsonl' # File name must not determine event trading day.
    events=[{'event_id':'a','ts':OPEN+60,'type':'manual_breakeven','lot_id':'one'},
            {'event_id':'b','ts':OPEN+70,'type':'candidate_order_protection','lot_id':'one'},
            {'event_id':'c','ts':OPEN+80,'type':'broker_order','action':'fill','lot_id':'one','exec_id':'exec1','fill_account':'member1','fill_qty':40},
            {'event_id':'d','ts':OPEN+81,'type':'broker_order','action':'fill','lot_id':'one','exec_id':'exec1','fill_account':'member1','fill_qty':40},
            {'event_id':'e','ts':OPEN+82,'type':'broker_order','action':'fill','lot_id':'one','exec_id':'exec1','fill_account':'member2','fill_qty':40}]
    path.write_text(''.join(json.dumps(r)+'\n' for r in events))
    before=(tmp_path/'lots_state.sqlite3').read_bytes()
    review=ResearchReview(tmp_path);review.sync();review.sync()
    result=review.trade('one',generate_questions=True)
    assert len(result['events'])==5 and len(result['fills'])==2
    assert result['metrics']['initial_actual_risk']==40
    assert result['metrics']['gross_r']==.5
    assert result['metrics']['net_r'] is None
    assert len(result['question_ids'])==3 # Entry, manual breakeven, manual exit.
    review.trade('one',generate_questions=True)
    assert len(review.notes.questions())==3
    assert (tmp_path/'lots_state.sqlite3').read_bytes()==before
    report=review.daily('2026-11-20')
    assert report['complete_trade_groups']==1
    assert review.daily('2026-11-20')['changed']==[]
    question=review.notes.questions()[0]
    review.notes.update_question(question['question_id'],status='answered',text='支撑买入',source='user1')
    assert review.daily('2026-11-20')['changed']==['one']


def test_cross_day_exit_reopens_review_and_incomplete_groups_are_not_counted(tmp_path):
    store=ledger(tmp_path,[lot('first',closed_at=0,signal_snapshot={'group_id':'g'}),
                           lot('second',closed_at=0,signal_snapshot={'group_id':'g'})])
    review=ResearchReview(tmp_path);review.sync()
    assert review.daily('2026-11-20')['complete_trade_groups']==0
    later=OPEN+3*86400
    store.save({'lots':[lot('first',closed_at=later,signal_snapshot={'group_id':'g'}),
                        lot('second',closed_at=later+1,signal_snapshot={'group_id':'g'})],'pyramid_groups':{}})
    review.sync();result=review.daily()
    assert result['day']=='2026-11-23'
    assert result['complete_trade_groups']==1
    assert len(result['reviews'])==1
    assert len(result['reviews'][0]['lots'])==2
    previous=len(review.notes.questions())
    assert review.trade('first',generate_questions=True)['trade_id']=='g'
    assert len(review.notes.questions())==previous


def test_automatic_stop_exit_is_not_learned_as_a_manual_exit(tmp_path):
    ledger(tmp_path,[lot(exit_reason='stop')])
    review=ResearchReview(tmp_path);review.sync()
    review.trade('one',generate_questions=True)
    assert [q['action_id'] for q in review.notes.questions()]==['entry:one']


def test_missing_or_corrupt_ledger_cannot_be_reported_as_no_trades(tmp_path):
    review=ResearchReview(tmp_path)
    with pytest.raises(FileNotFoundError):review.sync()
    (tmp_path/'lots_state.sqlite3').write_bytes(b'corrupt')
    with pytest.raises(Exception):review.sync()


def test_event_tail_waits_for_completion_and_invalid_lines_remain_visible(tmp_path):
    ledger(tmp_path,[lot()])
    file=tmp_path/'events-20261120.jsonl'
    event=json.dumps({'event_id':'x','lot_id':'one','type':'manual_stop_changed','ts':OPEN+120})
    file.write_text(event[:20])
    review=ResearchReview(tmp_path);assert review.sync()['indexed_lines']==0
    with file.open('a') as f:f.write(event[20:]+'\ninvalid\n')
    assert review.sync()['indexed_lines']==1
    assert review.store.status()['issues']==1
    assert review.sync()['indexed_lines']==0


def test_pre_submission_decisions_are_linked_only_by_exact_trace_identity(tmp_path):
    store=ledger(tmp_path,[])
    path=tmp_path/'events-20261120.jsonl'
    path.write_text(json.dumps({'event_id':'d','research_decision_id':'trace-one','type':'manual_entry_decision','ts':OPEN,'symbol':'NASDAQ:TEST'})+'\n')
    review=ResearchReview(tmp_path);review.sync()
    store.save({'lots':[lot(signal_snapshot={'research_decision_id':'trace-one'})],'pyramid_groups':{}})
    review.sync()
    result=review.trade('one')
    assert result['events'][0]['research_decision_id']=='trace-one'


def test_interprocess_note_writers_do_not_overwrite_each_other(tmp_path):
    import subprocess,sys
    root=tmp_path/'notes'
    script="from engine.research_methods import ResearchNotes; import sys; n=ResearchNotes(sys.argv[1]); [n.question(trade_id='t',action_id=sys.argv[2]+str(i),facts={},prompt='why') for i in range(8)]"
    children=[subprocess.Popen([sys.executable,'-c',script,str(root),str(i)]) for i in range(2)]
    for child in children:assert child.wait(timeout=10)==0
    assert len(ResearchNotes(root).questions())==16


def test_local_research_api_rejects_lan_and_cannot_trade(console_app):
    import asyncio,httpx
    async def check():
        for host,code in [('192.0.2.5',403),('127.0.0.1',200)]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=console_app.app,client=(host,1234)),base_url='http://test') as client:
                response=await client.get('/api/research/data-status',params={'day':'2026-11-20'})
                assert response.status_code==code
                assert (await client.post('/api/research/orders',json={'symbol':'TEST'})).status_code==404
    asyncio.run(check())


def test_offline_cli_uses_temp_data_and_does_not_boot_the_console(tmp_path):
    import subprocess,sys
    from pathlib import Path
    ledger(tmp_path,[lot()])
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'research.py'),'--data-dir',str(tmp_path),'daily','--day','2026-11-20'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['complete_trade_groups']==1
    assert (tmp_path/'agent_research'/'reviews'/'2026-11-20.md').exists()
    assert not (tmp_path/'live_audit.sqlite3').exists()


def test_daily_work_queue_advances_history_skips_duplicates_and_reopens_answers(tmp_path):
    ledger(tmp_path,[lot('new'),lot('old',entry_time=OPEN-86400+30,closed_at=OPEN-86400+360)])
    review=ResearchReview(tmp_path);review.sync()
    first=review.daily();assert first['day']=='2026-11-20';assert first['backlog_groups']==['old']
    second=review.daily();assert second['day']=='2026-11-19'
    assert review.daily()['skipped']
    q=next(q for q in review.notes.questions() if q['trade_id']=='old')
    review.notes.update_question(q['question_id'],status='answered',text='先看大盘',source='user')
    assert review.daily()['changed']==['old']


def test_legacy_decision_frame_is_read_only_and_missing_rank_reference_is_explicit(tmp_path):
    from engine.live_audit import LiveMarketAudit
    audit=LiveMarketAudit(tmp_path/'live_audit.sqlite3')
    audit._write([(123,OPEN+30,OPEN+29,{'NASDAQ:TEST':{'bar':{'time':OPEN,'close':100}}})])
    before=audit.path.read_bytes()
    ledger(tmp_path,[lot(signal_snapshot={'decision_market_frame':'123','candidate_admission':{'ranking_evidence':{'batch_id':'missing'}}})])
    review=ResearchReview(tmp_path);review.sync();result=review.trade('one')
    assert result['legacy_decision_frames'][0]['data']['bar']['close']==100
    assert result['missing_ranking_batches']==['missing']
    assert audit.path.read_bytes()==before


def test_research_metadata_failure_cannot_discard_original_trading_event(tmp_path):
    from types import SimpleNamespace
    from console.services.events import EventsService
    collector=SimpleNamespace(market_frame_id='1',latest=lambda:(_ for _ in ()).throw(ValueError('research metadata error')))
    context=SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path),collector=collector)
    EventsService(context).record_event('order_placed',symbol='NASDAQ:TEST',lot_id='one')
    event=json.loads(next(tmp_path.glob('events-*.jsonl')).read_text())
    assert event['type']=='order_placed'
    assert event['research_evidence_error']=='research metadata error'


def test_unfilled_orders_have_intent_questions_but_are_not_counted_as_completed_trades(tmp_path):
    ledger(tmp_path,[lot(entry_time=0,avg_fill=0,entry_cumulative_qty=0,created_at=OPEN,closed_at=0,state='PENDING')])
    review=ResearchReview(tmp_path);review.sync();result=review.daily()
    assert result['complete_trade_groups']==0
    assert result['reviews'][0]['actions'][0]['kind']=='entry_intent'
    assert result['pending_questions'][0]['action_id']=='entry_intent:one'


def test_technical_callbacks_are_not_misclassified_as_human_decisions():
    assert actor({'type':'broker_order','action':'modify_stop'})=='broker_report'
    assert actor({'type':'execution_entry_stage'})=='system_execution'
    assert actor({'type':'st_flip_warning'})=='notice'
    assert actor({'type':'pyramid_add'})=='automatic'
