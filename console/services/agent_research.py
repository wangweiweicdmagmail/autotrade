"""Research workbench: reads execution facts, writes only the existing notes journal."""
from datetime import datetime
import threading

from engine.research_calendar import ET
from engine.research_review import ResearchReview


def day_of(stamp):
    return datetime.fromtimestamp(stamp, ET).date().isoformat() if stamp else None


def validate_day(day):
    if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
        raise ValueError('日期必须为YYYY-MM-DD')
    return day


class AgentResearchService:
    def __init__(self, context):
        self.review = ResearchReview(context.paths.data_dir)
        self.lock = threading.RLock()

    def refresh(self):
        with self.lock:
            result = self.review.sync()
            return {**result, **self.days()}

    def _groups(self):
        if not self.review.store.path.exists():
            return {}, {}
        groups, lot_groups = {}, {}
        for row in self.review.trades():
            key = (row.get('signal_snapshot') or {}).get('group_id') or row['lot_id']
            group = groups.setdefault(key, {'trade_id': key, 'lots': [], 'days': set()})
            group['lots'].append(row)
            lot_groups[row['lot_id']] = key
            for field in ('created_at', 'entry_broker_time', 'entry_time', 'closed_at'):
                if row.get(field):
                    group['days'].add(day_of(row[field]))
        with self.review.store.read() as db:
            for lot_id, stamp in db.execute('SELECT lot_id,MIN(ts) FROM trade_events WHERE lot_id IS NOT NULL GROUP BY lot_id,CAST(ts/3600 AS INTEGER)'):
                if lot_id in lot_groups:
                    groups[lot_groups[lot_id]]['days'].add(day_of(stamp))
        questions = {}
        for row in self.review.notes.questions():
            questions.setdefault(row['trade_id'], []).append(row)
        return groups, questions

    def days(self):
        groups, questions = self._groups()
        days = {}
        for key, group in groups.items():
            for day in group['days']:
                row = days.setdefault(day, {'day': day, 'trades': 0, 'unanswered': 0})
                row['trades'] += 1
                row['unanswered'] += sum(q['status'] in ('pending', 'asked') for q in questions.get(key, []))
        return {'days': sorted(days.values(), key=lambda r: r['day'], reverse=True)}

    def day(self, day):
        validate_day(day)
        groups, questions = self._groups()
        rows = []
        for key, group in groups.items():
            if day not in group['days']:
                continue
            lots = group['lots']
            entry = min((r.get('entry_broker_time') or r.get('entry_time') for r in lots
                         if r.get('entry_broker_time') or r.get('entry_time')), default=None)
            ended = all(r.get('closed_at') for r in lots)
            qs = questions.get(key, [])
            rows.append({'trade_id': key, 'symbols': list(dict.fromkeys(r['symbol'] for r in lots)),
                'side': lots[0]['side'] if len({r['side'] for r in lots}) == 1 else 'mixed',
                'entry_at': entry, 'created_at': min(r.get('created_at') or r.get('entry_time') or 0 for r in lots),
                'exit_at': max((r.get('closed_at') or 0 for r in lots), default=0) if ended else None,
                'states': sorted({r['state'] for r in lots}), 'lots': len(lots),
                'metrics': self.review._metrics(lots), 'unanswered': sum(q['status'] in ('pending', 'asked') for q in qs),
                'answered': sum(bool(q.get('answer')) for q in qs), 'cross_day': len(group['days']) > 1})
        rows.sort(key=lambda r: (r['entry_at'] or r['created_at'], r['trade_id']))
        return {'day': day, 'trades': rows, 'summary': {'trades': len(rows),
            'realized_pnl': sum(r['metrics']['realized_pnl'] for r in rows),
            'unanswered': sum(r['unanswered'] for r in rows), 'answered': sum(r['answered'] for r in rows)},
            'pnl_scope': 'whole_trade_groups; cross-day groups are shown on each activity day, not daily-attributed PnL'}

    def detail(self, identity):
        with self.lock:
            result = self.review.trade(identity, generate_questions=True, include_context=False)
            result['questions'] = [q for q in self.review.notes.questions() if q['trade_id'] == result['trade_id']]
            fields = ('lot_id','symbol','side','state','avg_fill','init_stop_px','stop_px','qty','entry_cumulative_qty',
                      'entry_time','entry_broker_time','closed_at','exit_px','exit_reason','realized_pnl','trader')
            result['lots'] = [{k:r.get(k) for k in fields} for r in result['lots']]
            # Keep the UI payload local and bounded; full audit evidence remains available via the offline CLI.
            result.pop('legacy_decision_frames', None)
            result.pop('market_context', None)
            for action in result['actions']:
                facts = action['facts']
                fields = ('avg_fill','stop','stop_px','exit_px','reason','exit_reason','qty','filled_qty','side',
                          'action','status','order_id','perm_id','parent_id','client_id','fill_qty','fill_px','error')
                action['facts'] = {k:facts[k] for k in fields if k in facts}
            result.pop('events', None)
            result.pop('fills', None)
            result['ranking_available'] = bool(result.pop('ranking_at_entry', None) or result.get('used_ranking_batches'))
            result.pop('used_ranking_batches', None)
            # The question identity and revision suffice for editing; don't duplicate snapshot trees.
            for question in result['questions']:
                question['action_time'] = question.get('facts', {}).get('ts')
                question['action_kind'] = question.get('facts', {}).get('kind') or 'trade_reflection'
                question.pop('facts', None)
            return result

    def prepare_day(self, day):
        with self.lock:
            report = self.day(day)
            for row in report['trades']:
                self.review.trade(row['trade_id'], generate_questions=True, include_context=False)
            return self.day(day)

    def answer(self, identity, question_id, text, revision, confirmed=False):
        with self.lock:
            questions = {q['question_id']:q for q in self.review.notes.questions()}
            question = questions.get(question_id)
            if not question or question['trade_id'] != identity:
                raise KeyError('这笔交易中没有该问题')
            status = 'confirmed' if confirmed else 'revised' if question.get('answer') else 'answered'
            row = self.review.notes.update_question(question_id, status=status, text=text,
                source='console:master', expected_id=revision)
            return {k:v for k,v in row.items() if k != 'facts'}
