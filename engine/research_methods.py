"""Append-only human explanations and method versions, with interprocess locking."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from engine.research_store import encode, digest


class ResearchNotes:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / '.notes.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self, name):
        path = self.root / name
        if not path.exists():
            return []
        raw = path.read_bytes()
        if raw and not raw.endswith(b'\n'):
            raise ValueError('研究记录尾部不完整，需恢复，不能覆盖: '+name)
        return [json.loads(line) for line in raw.splitlines() if line]

    def _append(self, name, record):
        fd = os.open(self.root/name, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
        with os.fdopen(fd,'a',encoding='utf-8') as handle:
            handle.write(encode(record)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def question(self, *, trade_id, action_id, facts, prompt):
        key = digest({'trade': trade_id, 'action': action_id})[:24]
        with self.locked():
            if not any(r['question_id'] == key for r in self._read('questions.jsonl')):
                self._append('questions.jsonl', {'id': str(uuid.uuid4()), 'question_id': key, 'trade_id': trade_id,
                             'action_id': action_id, 'facts': facts, 'prompt': prompt,
                             'status': 'pending', 'recorded_at': time.time()})
        return key

    def questions(self, status=None):
        with self.locked():
            latest = {}
            for record in self._read('questions.jsonl'):
                latest[record['question_id']] = {**latest.get(record['question_id'], {}), **record}
        return [r for r in latest.values() if status is None or r['status'] == status
                or (status == 'pending' and r['status'] in ('pending', 'asked'))]

    def update_question(self, question_id, *, status, text=None, source=None):
        if status not in ('asked', 'answered', 'confirmed', 'revised'):
            raise ValueError('问题状态无效')
        if status in ('answered', 'confirmed', 'revised') and (not text or not source):
            raise ValueError('用户解释和答复来源不能为空')
        with self.locked():
            records = [r for r in self._read('questions.jsonl') if r['question_id'] == question_id]
            if not records:
                raise ValueError('问题不存在')
            allowed = {'pending': {'asked','answered','confirmed'}, 'asked': {'asked','answered','confirmed'},
                       'answered': {'answered','confirmed','revised'}, 'confirmed': {'confirmed','revised'},
                       'revised': {'revised','answered','confirmed'}}
            if status not in allowed.get(records[-1]['status'], set()):
                raise ValueError('不能将已回答的问题退回已问状态')
            if records[-1]['status']==status and (text is None or (records[-1].get('answer')==text and records[-1].get('answer_source')==source)):
                return records[-1]
            record = {**records[-1], 'id': str(uuid.uuid4()), 'status': status, 'recorded_at': time.time()}
            if text is not None:
                record.update(answer=text, answer_source=source)
            self._append('questions.jsonl', record)
        return record

    def method(self, *, rule, source, question_ids, status='hypothesis', method_id=None, counterexamples=None):
        if not rule or not source or status not in ('hypothesis', 'user_confirmed', 'validated'):
            raise ValueError('方法内容、来源或状态无效')
        if status == 'validated':
            raise ValueError('M3实验验证尚未接入，不能直接标记已验证')
        with self.locked():
            questions = {r['question_id']:r for r in self._read('questions.jsonl')}
            if any(key not in questions for key in question_ids):
                raise ValueError('方法关联的问题不存在')
            if status=='user_confirmed' and any(questions[key]['status'] in ('pending','asked') for key in question_ids):
                raise ValueError('尚未收到用户解释，不能标记为已确认方法')
            versions = self._read('methods.jsonl')
            key = method_id or str(uuid.uuid4())
            version = 1+max((r['version'] for r in versions if r['method_id'] == key), default=0)
            record = {'method_id': key, 'version': version, 'rule': rule, 'source': source, 'status': status,
                      'question_ids': question_ids, 'counterexamples': counterexamples or [], 'recorded_at': time.time()}
            self._append('methods.jsonl', record)
        return record

    def methods(self):
        with self.locked():
            return self._read('methods.jsonl')

    def progress(self, updates=None, source_updates=None):
        with self.locked():
            path = self.root / 'progress.json'
            value = json.loads(path.read_text()) if path.exists() else {'version': 1, 'reviewed': {}}
            if value.get('version') != 1 or not isinstance(value.get('reviewed'),dict):
                raise ValueError('复盘进度版本或内容无效')
            if updates is not None:
                value['reviewed'].update(updates)
                value.setdefault('source_revisions',{}).update(source_updates or {})
                from execution.state_store import atomic_write
                atomic_write(path, encode(value))
            return value
