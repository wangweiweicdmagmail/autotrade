import json
from datetime import datetime

from starlette.testclient import TestClient

from engine.research_calendar import ET
from execution.state_store import ExecutionStateStore

OPEN = int(datetime(2026, 11, 20, 9, 30, tzinfo=ET).timestamp())


def seed(context):
    rows = [
        {'lot_id':'nvda','symbol':'NASDAQ:NVDA','side':'short','qty':10,'avg_fill':100,
         'entry_time':OPEN+60,'created_at':OPEN,'closed_at':OPEN+600,'state':'CLOSED',
         'init_stop_px':102,'entry_cumulative_qty':10,'realized_pnl':15,'exit_px':98.5,
         'exit_reason':'manual','signal_snapshot':{}},
        {'lot_id':'cancel','symbol':'NASDAQ:AMD','side':'long','qty':10,'avg_fill':0,
         'entry_time':0,'created_at':OPEN+5,'closed_at':0,'state':'CANCELLED',
         'init_stop_px':99,'entry_cumulative_qty':0,'signal_snapshot':{}},
    ]
    ExecutionStateStore(context.paths.data_dir/'lots_state.json').save({'lots':rows,'pyramid_groups':{}})
    events=[
        {'event_id':'half','lot_id':'nvda','type':'order_close_half','ts':OPEN+300,'qty':5},
        {'event_id':'auto','lot_id':'nvda','type':'auto_breakeven','ts':OPEN+240},
        {'event_id':'broker','lot_id':'nvda','type':'broker_order','action':'fill','ts':OPEN+301,
         'fill_account':'account','exec_id':'exact','fill_qty':5,'fill_px':99},
    ]
    (context.paths.data_dir/'events-20261120.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    return rows


def test_workbench_end_to_end_notes_roundtrip_leaves_execution_unchanged(console_app):
    seed(console_app)
    path=console_app.paths.data_dir/'lots_state.sqlite3'
    before=path.read_bytes()
    client=TestClient(console_app.app,client=('127.0.0.1',123))
    assert client.post('/api/research/workbench/refresh',json={}).json()['days'][0]['day']=='2026-11-20'
    day=client.post('/api/research/workbench/day?day=2026-11-20',json={}).json()
    assert len(day['trades'])==2
    detail=client.post('/api/research/workbench/trades/nvda',json={}).json()
    assert {a['kind'] for a in detail['actions']} >= {'entry','exit','order_close_half','auto_breakeven','broker_order'}
    assert len(detail['questions'])==3
    assert 'market_context' not in detail and 'legacy_decision_frames' not in detail
    assert all('facts' not in q and q['action_time'] for q in detail['questions'])
    question=next(q for q in detail['questions'] if q['action_kind']=='entry')
    endpoint='/api/research/workbench/trades/nvda/questions/'+question['question_id']
    saved=client.post(endpoint,json={'text':'大盘在压力位，NVDA先转弱。','revision':question['id']})
    assert saved.status_code==200
    answer=saved.json()
    assert answer['answer_source']=='console:master' and answer['status']=='answered'
    assert client.post(endpoint,json={'text':'旧页面不能覆盖','revision':question['id']}).status_code==409
    confirmed=client.post(endpoint,json={'text':'大盘在压力位，NVDA先转弱。','revision':answer['id'],'confirmed':True})
    assert confirmed.status_code==200 and confirmed.json()['status']=='confirmed'
    revised=client.post(endpoint,json={'text':'修订：先看大盘，再比较个股。','revision':confirmed.json()['id']})
    assert revised.json()['status']=='revised'
    restored=client.post('/api/research/workbench/trades/nvda',json={}).json()
    assert next(q for q in restored['questions'] if q['question_id']==question['question_id'])['answer']=='修订：先看大盘，再比较个股。'
    day=client.get('/api/research/workbench/day?day=2026-11-20').json()
    assert day['summary']['answered']==1 and day['summary']['unanswered']==3
    assert path.read_bytes()==before
    assert not (console_app.paths.data_dir/'agent_research'/'methods.jsonl').exists()
    history=[json.loads(line) for line in (console_app.paths.data_dir/'agent_research'/'questions.jsonl').read_text().splitlines()]
    assert len([q for q in history if q['question_id']==question['question_id']])==4


def test_workbench_write_scope_origin_input_and_question_ownership(console_app):
    seed(console_app)
    local=TestClient(console_app.app,client=('127.0.0.1',123))
    remote=TestClient(console_app.app,client=('100.64.0.5',123))
    root='/api/research/workbench'
    assert remote.get(root+'/days').status_code==403
    assert remote.post(root+'/refresh',json={}).status_code==403
    assert local.post(root+'/refresh',json={},headers={'Origin':'https://elsewhere.invalid'}).status_code==403
    assert local.post(root+'/refresh',json={},headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert local.post(root+'/refresh',content='{}',headers={'Content-Type':'text/plain'}).status_code==415
    assert local.post(root+'/refresh',json={},headers={'Origin':'http://testserver'}).status_code==200
    assert local.get(root+'/day?day=2026-99-99').status_code==400
    assert local.post(root+'/trades/absent',json={}).status_code==404
    q=local.post(root+'/trades/nvda',json={}).json()['questions'][0]
    endpoint=root+'/trades/nvda/questions/'+q['question_id']
    assert local.post(endpoint,json={'text':' ','revision':q['id']}).status_code==422
    assert local.post(endpoint,json={'text':'x'*20001,'revision':q['id']}).status_code==422
    assert local.post(endpoint,json={'text':'reason','revision':q['id'],'order_id':1}).status_code==422
    assert local.post(root+'/trades/cancel/questions/'+q['question_id'],json={'text':'wrong trade','revision':q['id']}).status_code==404
    assert local.post('/api/research/orders',json={'symbol':'NVDA'}).status_code==404


def test_workbench_cross_day_events_and_groups_are_not_duplicated(console_app):
    rows=seed(console_app)
    rows[0]['signal_snapshot']={'group_id':'group'}
    rows.append({**rows[0],'lot_id':'second','entry_time':OPEN+120,'closed_at':OPEN+86400*3})
    store=ExecutionStateStore(console_app.paths.data_dir/'lots_state.json')
    store.load()
    store.save({'lots':rows,'pyramid_groups':{}})
    service=console_app.agent_research
    service.refresh()
    days={r['day']:r for r in service.days()['days']}
    assert days['2026-11-23']['trades']==1
    report=service.day('2026-11-20')
    assert len(report['trades'])==2
    group=next(r for r in report['trades'] if r['trade_id']=='group')
    assert group['lots']==2 and group['cross_day']
    assert len(service.detail('second')['lots'])==2
    assert service.detail('second')['trade_id']=='group'


def test_workbench_empty_projection_and_corrupt_state_fail_visibly(console_app):
    local=TestClient(console_app.app,client=('127.0.0.1',123))
    assert local.get('/api/research/workbench/days').json()=={'days':[]}
    assert local.post('/api/research/workbench/refresh',json={}).status_code==503
    (console_app.paths.data_dir/'lots_state.sqlite3').write_bytes(b'broken')
    assert local.post('/api/research/workbench/refresh',json={}).status_code==503
