"""Read-only execution projection and per-trade research. No trading calls."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

from engine.research_store import ResearchStore, digest, encode, number
from engine.research_calendar import ET
from engine.research_methods import ResearchNotes
from execution.state_store import ExecutionStateStore

HUMAN = {'manual_breakeven', 'manual_stop_changed', 'order_close_requested', 'order_close_half', 'order_cancelled'}
AUTOMATIC = {'candidate_order_protection', 'candidate_order_protection_failed', 'auto_breakeven', 'pyramid_breakeven'}


def actor(event):
    kind = event.get('type', '')
    if kind in HUMAN:
        return 'human'
    if kind == 'broker_order':
        return 'broker_report'
    if kind == 'execution_entry_stage':
        return 'system_execution'
    if 'external' in kind:
        return 'external_guard'
    if kind in AUTOMATIC or kind.startswith(('candidate_', 'auto_', 'pyramid_', 'strategy_')):
        return 'automatic'
    if any(word in kind for word in ('warning','notice','alert')):
        return 'notice'
    if kind == 'lot_closed':
        reason = str(event.get('exit_reason') or event.get('reason') or '')
        if reason == 'stop':
            return 'protective_stop'
    return 'unknown'


class ResearchReview:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.store = ResearchStore(self.data_dir / 'research' / 'archive.sqlite3')
        self.notes = ResearchNotes(self.data_dir / 'agent_research')

    def sync(self):
        path = self.data_dir / 'lots_state.json'
        if not path.with_suffix('.sqlite3').exists() and not path.exists():
            raise FileNotFoundError('执行账本缺失，不能解释为无交易')
        ledger = ExecutionStateStore(path)
        current = ledger.load()
        orders = {}
        for row in sorted(ledger.archive_records(), key=lambda r: r.get('closed_at', 0)):
            if row.get('lot_id'):
                orders[row['lot_id']] = row
        for row in current['lots']:
            orders[row['lot_id']] = row
        decisions = {r.get('signal_snapshot',{}).get('research_decision_id'):r['lot_id'] for r in orders.values()
                     if r.get('signal_snapshot',{}).get('research_decision_id')}
        with self.store.write() as db:
            for key, row in orders.items():
                stamp = row.get('entry_broker_time') or row.get('entry_time') or row.get('created_at') or 0
                day = datetime.fromtimestamp(stamp, ET).date().isoformat()
                db.execute('INSERT OR REPLACE INTO order_records VALUES(?,?,?)', (key, day, encode(row)))
            for event_id, payload in db.execute('SELECT id,payload FROM trade_events WHERE lot_id IS NULL').fetchall():
                linked = decisions.get(json.loads(payload).get('research_decision_id'))
                if linked:
                    db.execute('UPDATE trade_events SET lot_id=? WHERE id=?',(linked,event_id))
        imported = 0
        for file in sorted(self.data_dir.glob('events-*.jsonl')):
            stat = file.stat()
            signature = f'{stat.st_dev}:{stat.st_ino}'
            source = 'events:'+file.name
            offset, old = self.store.cursor(source)
            if (old and old != signature) or stat.st_size < offset:
                # Restart from zero, with content identities retaining previous facts.
                offset = 0
                self.store.issue(source, '事件文件轮转或截断，重新索引，原记录保留')
            with file.open('rb') as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline(2*1024*1024+1)
                    if not line or not line.endswith(b'\n'):
                        break
                    next_offset = stream.tell()
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or not number(event.get('ts')):
                            raise ValueError('事件时间无效')
                        # New events have IDs; legacy exact duplicates share a content identity.
                        event_id = str(event.get('event_id') or digest(event))
                        lot_id = event.get('lot_id') or decisions.get(event.get('research_decision_id'))
                        with self.store.write() as db:
                            db.execute('INSERT INTO trade_events VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET lot_id=COALESCE(excluded.lot_id,trade_events.lot_id)',
                                       (event_id, lot_id, event['ts'], encode(event)))
                            if event.get('exec_id'):
                                account = event.get('fill_account') or event.get('account')
                                fill_id = str(account or 'unknown_account')+':'+str(event['exec_id'])
                                payload = {**event, 'identity_quality': 'exact' if account else 'account_unknown'}
                                db.execute('INSERT OR IGNORE INTO fills VALUES(?,?,?,?)', (fill_id, lot_id, event['ts'], encode(payload)))
                            db.execute('INSERT OR REPLACE INTO import_offsets VALUES(?,?,?)', (source, next_offset, signature))
                        imported += 1
                    except (ValueError, TypeError, UnicodeError) as exc:
                        self.store.issue(source+':'+str(offset), '事件行无效: '+str(exc))
                        with self.store.write() as db:
                            db.execute('INSERT OR REPLACE INTO import_offsets VALUES(?,?,?)', (source, next_offset, signature))
                    offset = next_offset
        return {'orders': len(orders), 'indexed_lines': imported}

    def trades(self, day=None):
        with self.store.read() as db:
            rows = db.execute('SELECT payload FROM order_records'+(' WHERE day=?' if day else '')+' ORDER BY day,identity', (day,) if day else ()).fetchall()
        return [json.loads(row[0]) for row in rows]

    def trade(self, identity, *, generate_questions=False):
        all_rows = self.trades()
        lots = [r for r in all_rows if r['lot_id'] == identity or (r.get('signal_snapshot') or {}).get('group_id') == identity]
        if not lots:
            raise ValueError('交易组或批次不存在')
        identity = (lots[0].get('signal_snapshot') or {}).get('group_id') or lots[0]['lot_id']
        lots = [r for r in all_rows if ((r.get('signal_snapshot') or {}).get('group_id') or r['lot_id'])==identity]
        ids = [r['lot_id'] for r in lots]
        with self.store.read() as db:
            rows = db.execute('SELECT id,payload FROM trade_events WHERE lot_id IN ('+','.join('?' for _ in ids)+') ORDER BY ts,id', ids).fetchall()
            fills = db.execute('SELECT payload FROM fills WHERE lot_id IN ('+','.join('?' for _ in ids)+') ORDER BY ts,identity', ids).fetchall()
        events = [{'id': key, **json.loads(payload), 'actor': actor(json.loads(payload))} for key, payload in rows]
        actions = []
        for lot in lots:
            key = lot['lot_id']
            if lot.get('entry_broker_time') or lot.get('entry_time'):
                actions.append({'id': 'entry:'+key, 'kind': 'entry', 'lot_id': key,
                            'ts': lot.get('entry_broker_time') or lot.get('entry_time'), 'actor': 'unknown',
                            'facts': {'symbol': lot['symbol'], 'side': lot['side'], 'avg_fill': lot.get('avg_fill'),
                                      'stop': lot.get('init_stop_px'), 'strategy': lot.get('strategy_id'),
                                      'signal_snapshot': lot.get('signal_snapshot')}})
            elif lot.get('created_at'):
                actions.append({'id':'entry_intent:'+key,'kind':'entry_intent','lot_id':key,'ts':lot['created_at'],
                                'actor':'unknown','facts':{'symbol':lot['symbol'],'side':lot['side'],
                                    'requested_qty':lot.get('qty'),'reference_price':lot.get('entry_px'),
                                    'stop':lot.get('init_stop_px'),'state':lot.get('state'),
                                    'interpretation':'unfilled order intent, not an executed entry'}})
            if lot.get('closed_at') and lot.get('avg_fill'):
                exit_actor = actor({'type':'lot_closed','exit_reason':lot.get('exit_reason')})
                if str(lot.get('exit_reason','')).startswith(('candidate_', 'breakout_')):
                    exit_actor = 'automatic'
                actions.append({'id': 'exit:'+key+':'+str(lot['closed_at']), 'kind': 'exit', 'lot_id': key,
                                'ts': lot['closed_at'], 'actor': exit_actor,
                                'facts': {'exit_px': lot.get('exit_px'), 'reason': lot.get('exit_reason')}})
        for event in events:
            if event.get('type') not in ('order_placed', 'lot_closed', 'execution_fill', 'manual_entry_decision'):
                actions.append({'id': event['id'], 'kind': event.get('type'), 'lot_id': event.get('lot_id'),
                                'ts': event['ts'], 'actor': event['actor'], 'facts': event})
        actions.sort(key=lambda r: (r.get('ts') or 0, r['id']))
        question_ids = []
        if generate_questions:
            for action in actions:
                # Broker status confirmations are facts; they are not extra human decisions.
                if action['kind'] == 'broker_order' and action['facts'].get('action') in ('status', 'fill', 'submitted'):
                    continue
                if action['actor'] in ('automatic', 'external_guard', 'protective_stop', 'broker_report', 'system_execution', 'notice'):
                    continue
                question_ids.append(self.notes.question(trade_id=identity, action_id=action['id'], facts=action,
                                    prompt=f"请解释这笔交易在 {action.get('ts')} 的 {action['kind']}：当时观察到什么，为什么采取这个动作？"))
        entry_times = [r.get('entry_broker_time') or r.get('entry_time') for r in lots if r.get('entry_broker_time') or r.get('entry_time')]
        entry = min(entry_times,default=min((r.get('created_at') or 0 for r in lots),default=0))
        day = datetime.fromtimestamp(entry, ET).date().isoformat()
        rankings = self.store.rankings(day, at=entry, as_of=entry)
        used_ids = {((r.get('signal_snapshot') or {}).get('candidate_admission') or {}).get('ranking_evidence',{}).get('batch_id') for r in lots}
        used_ids.discard(None)
        with self.store.read() as db:
            used_batches = {key:json.loads(payload) for key,payload in db.execute('SELECT id,payload FROM ranking_batches WHERE id IN ('+
                ','.join('?' for _ in used_ids)+')',list(used_ids))} if used_ids else {}
        explained = [q for q in self.notes.questions() if q['trade_id'] == identity]
        metrics = self._metrics(lots)
        legacy_frames = []
        legacy_path = self.data_dir/'live_audit.sqlite3'
        if legacy_path.exists():
            from engine.live_audit import LiveMarketAudit
            audit = LiveMarketAudit(legacy_path)
            for lot in lots:
                frame = (lot.get('signal_snapshot') or {}).get('decision_market_frame')
                if frame and str(frame).isdigit():
                    try:
                        found = audit.read(lot['symbol'],frame_id=int(frame),limit=1)
                    except Exception as exc:
                        legacy_frames.append({'lot_id':lot['lot_id'],'error':str(exc),'frame_id':str(frame)})
                        found = []
                    for record in found:
                        legacy_frames.append({**{k:v for k,v in record.items() if k!='data'},'lot_id':lot['lot_id'],
                            'data':{k:v for k,v in record['data'].items() if k!='opening_breakout_history'}})
        windows = []
        for symbol in dict.fromkeys([*[r['symbol'] for r in lots],'CAPITALCOM:NAS100','CAPITALCOM:SPX500']):
            for series in self.store.series(symbol,'1m'):
                before = self.store.bars(series['id'],max(0,entry-15*60),entry,as_of=entry,limit=30)
                after = self.store.bars(series['id'],max(0,entry-5*60),entry+15*60,limit=30)
                windows.append({'series':series,'known_at_entry':before,'research_window_latest':after,
                                'research_window_role':'posthoc_review_only; not decision inputs'})
        return {'trade_id': identity, 'lots': lots, 'events': events, 'fills': [json.loads(r[0]) for r in fills],
                'actions': actions, 'questions': explained, 'question_ids': question_ids,
                'ranking_at_entry': rankings[-1] if rankings else None,
                'ranking_at_entry_role': 'latest observed context; exact decision references are separate',
                'used_ranking_batches': used_batches, 'missing_ranking_batches': sorted(used_ids-set(used_batches)),
                'market_context': windows,
                'legacy_decision_frames': legacy_frames,
                'metrics': metrics, 'version': digest({'lots': lots, 'events': events, 'questions': explained})}

    @staticmethod
    def _metrics(lots):
        total_risk = 0
        for lot in lots:
            if not lot.get('avg_fill'):
                continue
            qty = lot.get('entry_cumulative_qty') or ((lot.get('filled_qty') or 0)+(lot.get('close_filled_qty') or 0))
            price, stop = lot.get('avg_fill'), lot.get('init_stop_px')
            if not all(number(v) and v > 0 for v in (qty, price, stop)):
                total_risk = None
                break
            total_risk += qty*abs(price-stop)
        pnl = sum(r.get('realized_pnl') or 0 for r in lots)
        fees_known = all(number(r.get('commission')) for r in lots)
        fees = sum(r['commission'] for r in lots) if fees_known else None
        return {'initial_actual_risk': total_risk, 'realized_pnl': pnl,
                'gross_r': pnl/total_risk if total_risk else None,
                'net_r': (pnl-fees)/total_risk if total_risk and fees_known else None,
                'fees': fees, 'fees_status': 'known' if fees_known else 'unknown',
                'mfe': [r.get('mfe') for r in lots], 'mae': [r.get('mae') for r in lots],
                'excursion_source': 'recorded_lot_values; no intrabar reconstruction',
                'boundary_ambiguity': 'OHLC cannot attribute entry/exit-minute extremes without recorded path'}

    def daily(self, day=None):
        rows = self.trades()
        progress = self.notes.progress()
        grouped = {}
        for row in rows:
            grouped.setdefault((row.get('signal_snapshot') or {}).get('group_id') or row['lot_id'],[]).append(row)
        with self.store.read() as db:
            event_stats = {key:(at,count) for key,at,count in db.execute('SELECT lot_id,MAX(ts),COUNT(*) FROM trade_events GROUP BY lot_id')}
        explanations = self.notes.questions()
        source_revisions, activity = {}, {}
        for key,members in grouped.items():
            stable = [{k:v for k,v in row.items() if k not in ('last_px','pnl','updated_at')} for row in members]
            answers = [(q['question_id'],q.get('answer'),q.get('answer_source'),q['status'] if q.get('answer') else 'unanswered')
                       for q in explanations if q['trade_id']==key]
            stats = [(r['lot_id'],event_stats.get(r['lot_id'])) for r in members]
            source_revisions[key] = digest({'lots':stable,'events':stats,'answers':[a for a in answers if a[1]]})
            activity[key] = max([0,*[r.get(k) or 0 for r in members for k in ('entry_broker_time','entry_time','closed_at','created_at')],
                                *[event_stats.get(r['lot_id'],(0,0))[0] for r in members]])
        pending = [key for key in grouped if progress.get('source_revisions',{}).get(key)!=source_revisions[key]]
        pending.sort(key=lambda key:(key in progress.get('source_revisions',{}),activity[key]),reverse=True)
        target_groups = None
        if day is None:
            if not pending:
                return {'day':datetime.now(ET).date().isoformat(),'reviews':[],'changed':[],'skipped':True,
                        'pending_questions':self.notes.questions('pending'),'methods':self.notes.methods(),
                        'reason':'没有新增交易、动作或用户解释'}
            day = datetime.fromtimestamp(activity[pending[0]],ET).date().isoformat()
            target_groups = [key for key in pending if datetime.fromtimestamp(activity[key],ET).date().isoformat()==day]
        selected = [r for r in rows if any(number(r.get(k)) and r[k] > 0 and datetime.fromtimestamp(r[k], ET).date().isoformat() == day
                                           for k in ('entry_time', 'entry_broker_time', 'closed_at'))]
        start = datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=ET).timestamp()
        from datetime import timedelta
        end = (datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=ET)+timedelta(days=1)).timestamp()
        with self.store.read() as db:
            touched = {r[0] for r in db.execute('SELECT DISTINCT lot_id FROM trade_events WHERE ts>=? AND ts<?',(start,end))}
        selected = list({r['lot_id']:r for r in [*selected,*[r for r in rows if r['lot_id'] in touched]]}.values())
        groups = sorted({(r.get('signal_snapshot') or {}).get('group_id') or r['lot_id'] for r in selected})
        if target_groups is not None:
            groups = target_groups
        reviews = [self.trade(key, generate_questions=True) for key in groups]
        changed = {r['trade_id']: r['version'] for r in reviews}
        revised = [r for r in reviews if progress['reviewed'].get(r['trade_id']) != r['version']]
        grouped = {}
        for r in rows:
            grouped.setdefault((r.get('signal_snapshot') or {}).get('group_id') or r['lot_id'],[]).append(r)
        complete_groups = {key for key,members in grouped.items() if any(r.get('avg_fill') for r in members)
                           and all(r.get('closed_at') for r in members)}
        result = {'day': day, 'reviews': reviews, 'changed': [r['trade_id'] for r in revised],
                  'complete_trade_groups': len(complete_groups), 'pending_questions': self.notes.questions('pending'),
                  'methods': self.notes.methods(), 'data_status': self.store.manifest(day)}
        result['backlog_groups'] = [key for key in pending if key not in groups]
        all_questions = self.notes.questions()
        result['learning_progress'] = {'complete_trade_groups':len(complete_groups),
            'answered_actions':sum(q['status'] in ('answered','confirmed','revised') for q in all_questions),
            'pending_actions':len(result['pending_questions']), 'first_assessment_due':len(complete_groups)>100}
        report_dir = self.notes.root / 'reviews'
        report_dir.mkdir(parents=True, exist_ok=True)
        from execution.state_store import atomic_write
        atomic_write(report_dir / (day+'.json'), encode(result))
        text = '# '+day+' 订单复盘\n\n'
        for review in reviews:
            text += '## '+review['trade_id']+'\n\n'
            for lot in review['lots']:
                text += f"{lot['symbol']} · {lot['side']} · 成交均价 {lot.get('avg_fill')} · 退出 {lot.get('exit_px')}\n\n"
            text += '```json\n'+json.dumps(review, ensure_ascii=False, indent=2)+'\n```\n\n'
        atomic_write(report_dir / (day+'.md'), text)
        self.notes.progress(changed,{key:source_revisions[key] for key in groups})
        return result
