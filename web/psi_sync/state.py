"""Map the allowlisted attention-center contract to decision-center UI fields."""
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import re
import threading

from .core_reader import ReadError, valid_id, decode
from .storage import now
from .reviews import RESPONSIBILITY, PROGRESS
from .triage import closure_question, grounded_prompt, human_requirement, CLOSURE_TECHNICAL, local_option, recommendation_choice
from .presentation import recommendation_excerpt, short_title, clauses, readable

NOTICE = '保存笔记仅在本机；点击在 Codex 中继续会交接当前事项'
LABELS = {'attention': '需你处理', 'automatic': '系统处理中', 'waiting': '等待条件', 'closed': '已结束'}
ACTIONS = {'resolve_plan', 'defer', 'continue_automatic', 'write_memory', 'answer_choice'}
SOURCES = {'activity': '活动记录', 'closure': '闭环事项', 'memory': '记忆', 'system': '系统', 'reference': '资料'}
ACTOR_ID = re.compile(r'<?\b(agent|codex):[A-Za-z0-9_.-]+>?', re.I)
UUID = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.I)


def display(value):
    """Clean display text only. Routing keys and persisted source stay intact."""
    value = ACTOR_ID.sub(lambda m: 'Codex' if m[1].lower() == 'codex' else '助手', value)
    value = UUID.sub('事项标识已省略', value)
    enums = {**SOURCES, 'decision': '待决定', 'verification': '核验中', 'pending': '待处理',
             'blocked': '等待条件', 'running': '运行中', 'idle': '空闲', 'complete': '已结束',
             'local_pending': '已在本机记录，待同步', 'historical_questions': '历史问题',
             'occurred': '是', 'not_occurred': '没有', 'defer': '暂缓',
             'user_confirmation': '本人确认', 'user_intent': '本人意愿'}
    return re.sub(r'\b(?:' + '|'.join(enums) + r')\b', lambda m: enums[m[0]], value)


def display_record(record):
    return {key: words(value) if key in {'content', 'message'} else value for key, value in record.items()}


def words(value):
    if isinstance(value, str):
        return display(value)
    if isinstance(value, list):
        return '\n'.join(filter(None, map(words, value)))
    if isinstance(value, dict):
        labels = {'phase': '阶段', 'status': '状态', 'reason': '原因', 'owner': '负责人'}
        return '\n'.join((labels.get(k, k) + '：' if k in labels or re.search(r'[\u4e00-\u9fff]', str(k)) else '') + words(v) for k, v in value.items() if words(v))
    return '' if value is None else str(value)


def walk(rows):
    for row in rows:
        yield row
        yield from walk(row.get('children', []))


def version(item):
    basis = {"id": item["id"], "revision": item["revision"]} if item.get("revision") else item
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def category(item, local=None):
    if local or item.get('state') in {'complete', 'completed', 'closed', 'resolved', 'archived', 'ended', 'done'}:
        return 'closed'
    if item.get('requires_decision'):
        if item.get('kind') == 'closure':
            if closure_question(item):
                return 'attention'
            return 'automatic' if technical_closure(item) else 'waiting'
        if item.get('kind') == 'activity':
            return 'attention'
    if item.get('kind') == 'closure' and item.get('state') == 'verification':
        return 'automatic'
    return 'waiting'


def technical_closure(item):
    text = item.get('title', '') + item.get('recommendation', '')
    return bool(CLOSURE_TECHNICAL.search(text) or re.search(r'助手|系统|技术|Codex|agent|脚本|自动', text, re.I))


def personal_question(item):
    return question_content(item)[1]


def question_content(item):
    """Return the subject line and question together so context cannot be lost."""
    if category(item) == 'attention':
        title = item.get('title', '')
        if item.get('kind') == 'activity':
            return '', words(item.get('card', {}).get('display_question') or title)
        headline = short_title(title)
        if title.rstrip().endswith(('？', '?')) or headline.endswith(('？', '?')):
            return '', words(title)
        parts = clauses(title)
        prompt = grounded_prompt(item, headline)
        if prompt and parts and headline == parts[0] and len(headline) <= 60 and readable(headline):
            return words(headline), words(prompt)
        return words(title), '这件事需要你怎么定？'
    return '', ''


def answer_options(item):
    if category(item) != 'attention':
        return []
    if item.get('kind') == 'closure':
        raw = item.get('recommendation', '')
        label = recommendation_choice(raw)
        return [{**local_option(label), 'label': words(label), 'full': words(raw)}] if label else []
    options = []
    for row in item.get('card', {}).get('answer_options', []):
        raw = row if isinstance(row, str) else row.get('label') or row.get('title', '')
        label = words(raw).strip()
        if label and label not in {o['label'] for o in options}:
            options.append({**local_option(raw), 'label': label, 'full': label})
    return options


def mapped(item, local=None):
    status = category(item, local)
    card = item.get('card', {})
    title = words((card.get('display_title') if item.get('kind') == 'activity' else '') or item.get('title') or '未命名事项')
    system_side = status == 'automatic' and item.get('requires_decision') and item.get('kind') == 'closure'
    question_subject, question = question_content(item)
    return {'id': item['id'], 'case_id': item['id'], 'revision': version(item),
            'native_revision': item.get('revision', ''),
            'codex_thread_id': item.get('codex_thread_id'),
            'codex_reply_available': item.get('codex_reply_available', False),
            'codex_handoff_available': item.get('codex_handoff_available', item.get('kind') != 'closure'),
            'title': title, 'subject': title,
            'kind': item.get('kind', ''), 'status': status, 'state_label': LABELS[status],
            'source_label': SOURCES.get(item.get('kind'), 'PSI'),
            'reason': '系统/助手侧事项，无需你决定' if system_side else words(item.get('reason', '')),
            'summary': words(item.get('summary', '')),
            'background': words(item.get('background') or item.get('summary', '')),
            'evidence_summary': words(card.get('display_evidence') or item.get('evidence') or item.get('current_verification')),
            'completion_rule': words(item.get('completion_rule')), 'recommendation': words(item.get('recommendation')),
            'question': question, 'question_subject': question_subject,
            'answer_options': answer_options(item), 'runtime': words(item.get('runtime')),
            'recovery': words(item.get('recovery')), 'local_answer': display_record(local) if local else None,
            'explanation': '系统/助手侧事项，无需你决定' if system_side else '',
            'group_key': item.get('group_key') or item['id'],
            'children': [mapped(child) for child in item.get('children', [])]}


class State:
    def __init__(self, reader, store, reviews=None):
        self.reader, self.store, self.reviews = reader, store, reviews
        self.lock = threading.RLock()
        self.snapshot = None
        self.items = {}
        self.error = ''
        self.read_at = ''
        self.fixture_ui = {}
        if reader.fixture:
            with reader.fixture.open('rb') as stream:
                raw = stream.read(reader.config.output_limit + 1)
            if len(raw) > reader.config.output_limit:
                raise ReadError('output_limit')
            bundle = decode(raw)
            self.fixture_ui = bundle.get('synthetic_ui', {}) if bundle.get('synthetic') is True else {}

    def refresh(self):
        with self.lock:
            try:
                snapshot = self.reader.snapshot()
                items = {}
                for collection in ('resources', 'systems', 'decisions', 'closures'):
                    for item in walk(snapshot[collection]):
                        items[item['id']] = item
                self.snapshot, self.items = snapshot, items
                self.read_at, self.error = now(), ''
            except ReadError as exc:
                # Fail closed: never leave withdrawn or stale IDs writable.
                self.snapshot, self.items, self.error = None, {}, str(exc)
            return self.view()

    def local_for(self, item, records):
        return next((a for a in reversed(records['actions']) if a['id'] == item['id'] and a.get('revision') == version(item)), None)

    def _map(self, item, local=None):
        if self.reviews is None:
            return mapped(item, local)
        row = mapped(item)
        review = self.reviews.project(item)
        progress, responsibility = review['progress'], review['responsibility']
        if item.get('state') in {'canceled', 'dismissed'}:
            progress = 'paused'
        if item.get('state') == 'resolved' and item.get('current_verification') != 'unknown':
            progress = 'resolved'
        status = ('closed' if progress == 'resolved' else 'waiting' if progress in {'waiting', 'paused'}
                  else 'attention' if responsibility in {'user_decision', 'user_action'} else 'automatic')
        record = self.reviews.bundle['reviews'].get(item['id'], {})
        if review['review_valid'] and isinstance(record.get('display_title'), str) and record['display_title'].strip():
            row['title'] = row['subject'] = words(record['display_title'])
        row.update(review, progress=progress, responsibility=responsibility, status=status,
                   state_label=PROGRESS[progress], responsibility_label=RESPONSIBILITY[responsibility],
                   local_answer=display_record(local) if local else None,
                   question=review.get('question', '') if responsibility == 'user_decision' else '',
                   answer_options=[], reason=review['basis'], recommendation=review['next_step'],
                   evidence_summary=review['outcome'] or '依据已核查，原文保留在本机原位置。' if review['review_valid'] else review['basis'])
        baseline = {x['id']: n for n, x in enumerate(self.reviews.baseline(), 1)}
        row['baseline_number'] = baseline.get(item['id'])
        row['is_new'] = item['id'] not in baseline
        return row

    def _rows(self):
        records = self.store.read()
        if self.reviews is not None:
            self.reviews.reload()
        items = list(self.items.values())
        if self.reviews is None:
            items.sort(key=lambda item: (item.get('kind') != 'closure', 0 if human_requirement(item) in {'signing','funds','authorization'} else 1 if human_requirement(item) else 2))
        rows = [self._map(item, self.local_for(item, records)) for item in items
                if item.get('kind') not in {'system', 'reference', 'historical_questions'}]
        if self.reviews is not None:
            present = {x['id'] for x in rows}
            for n, item in enumerate(self.reviews.baseline(), 1):
                if item['id'] not in present:
                    fallback = {'id': item['id'], 'kind': 'closure', 'title': f'原第 {n} 项来源暂不可读取', 'revision': '', 'state': 'unknown', 'current_verification': 'unknown', 'children': []}
                    row = self._map(fallback)
                    row.update(baseline_number=n, is_new=False, source_missing=True,
                               codex_handoff_available=False, question='', progress='blocked',
                               state_label='受阻', basis='当前来源未能读取，保留起始记录，不推断已结束。')
                    rows.append(row)
            rows.sort(key=lambda x: (x.get('baseline_number') is None, x.get('baseline_number') or 0))
        if self.reviews is not None:
            current_ids = {x['id'] for x in self.snapshot.get('decisions', [])} if self.snapshot else set()
            for row in rows:
                row['work_queue'] = bool(row.get('baseline_number') or row['id'] in current_ids)
        return rows

    def counts(self, rows):
        if self.reviews is not None:
            rows = [r for r in rows if r.get('work_queue')]
        complete = self.snapshot and self.snapshot['status'] == 'ok'
        if self.reviews is not None:
            complete = complete and not self.reviews.error and all(r.get('review_valid') for r in rows)
        result = {key: sum(row['status'] == key for row in rows) if complete else None for key in LABELS}
        # A declared unknown total must remain unknown, even with partial rows.
        if self.snapshot and 'decision_count' in self.snapshot['counts_present'] and self.snapshot['counts']['decision_count'] is None:
            result['attention'] = None
        if self.reviews is not None:
            result['automatic'] = sum(r.get('responsibility') == 'ai' and r['progress'] != 'resolved' for r in rows) if complete else None
        result['all'] = len(rows) if complete else None
        if self.reviews is not None:
            result['records'] = sum(row['status'] != 'attention' for row in rows) if complete else None
            result['open'] = sum(r['status'] != 'closed' for r in rows) if complete else None
        return result

    def listing(self, scope=None):
        scope = scope or ('open' if self.reviews is not None else 'attention')
        if scope not in {*LABELS, 'all', 'open', 'records'}:
            raise ValueError('invalid_scope')
        with self.lock:
            rows = self._rows()
            # all includes ended records only on explicit selection.
            if self.reviews is not None:
                rows = [r for r in rows if r.get('work_queue')]
            selected = [row for row in rows if (scope == 'all' or scope == 'records' and row['status'] != 'attention' or scope == 'open' and row['status'] != 'closed'
                        or scope == 'automatic' and self.reviews is not None and row.get('responsibility') == 'ai' and row['progress'] != 'resolved'
                        or row['status'] == scope and not (scope == 'automatic' and self.reviews is not None))]
            summaries = Counter(row['summary'] for row in selected if row['summary'])
            for row in selected:
                row['list_summary'] = (recommendation_excerpt(row['recommendation'])
                                       if summaries[row['summary']] >= 3 else row['reason'] or row['summary'])
            return {'items': selected,
                    'counts': self.counts(rows), 'scope': scope, 'status': 'unavailable' if self.error else ('partial' if self.counts(rows).get('all') is None else 'ok')}

    def view(self):
        with self.lock:
            rows = self._rows()
            snapshot = self.snapshot or {}
            counts = self.counts(rows)
            history = []
            records = self.store.read()
            for item in snapshot.get('resources', []):
                if item.get('kind') == 'historical_questions':
                    history.append({'title': words(item['title']), 'items': [mapped(x, self.local_for(x, records)) for x in walk(item.get('children', []))]})
            other_records = [r for r in rows if self.reviews is not None and not r.get('work_queue')]
            if other_records:
                history.append({'title': '其他跟进记录（未逐项核查，不计为你的待办）', 'items': other_records})
            work_rows = [r for r in rows if self.reviews is None or r.get('work_queue')]
            closed = [r for r in work_rows if r['status'] == 'closed']
            historical_ids = {r['id'] for group in history for r in group['items']}
            history.append({'title': '已结束问题', 'items': [r for r in closed if r['id'] not in historical_ids]})
            return {'status': 'unavailable' if self.error else ('partial' if counts.get('all') is None else snapshot.get('status', 'unavailable')),
                    'generated_at': snapshot.get('generated_at', ''), 'read_at': self.read_at,
                    'counts': counts, 'metrics': {'needs': counts['attention'], 'automatic': counts['automatic'],
                    'resolved': None, 'history': len({row['id'] for group in history for row in group['items']}) if snapshot.get('status') == 'ok' else None},
                    'sources': [mapped(x) for x in snapshot.get('resources', [])],
                    'systems': [mapped(x) for x in snapshot.get('systems', [])],
                    'automatic': [r for r in work_rows if (r.get('responsibility') == 'ai' if self.reviews is not None else r['status'] in {'automatic', 'waiting'})],
                    'history': history, 'recent_resolved': [r for r in closed if not r['local_answer']],
                    'events': [display_record(r) for r in records['actions']], 'synthetic': bool(self.reader.fixture),
                    'notice': NOTICE, 'error': self.error,
                    'review_notice': self.reviews.error if self.reviews else '',
                    'baseline_summary': dict(Counter(r.get('responsibility', 'unknown') for r in rows if r.get('baseline_number'))) if self.reviews else {},
                    'baseline_count': len(self.reviews.baseline()) if self.reviews else 0}

    def registered(self, identifier):
        if not valid_id(identifier) or identifier not in self.items:
            raise ReadError('unregistered_id')
        return self.items[identifier]

    def detail(self, identifier):
        with self.lock:
            if self.reviews is not None and identifier not in self.items:
                self.reviews.reload()
                old = next((x for x in self.reviews.baseline() if x['id'] == identifier), None)
                if old:
                    placeholder = {'id': identifier, 'kind': 'closure', 'title': '原事项来源暂不可读取', 'revision': '', 'state': 'unknown', 'current_verification': 'unknown', 'children': []}
                    return {**self._map(placeholder), 'source_missing': True, 'codex_handoff_available': False}
            self.registered(identifier)
            try:
                item = self.reader.detail(identifier, set(self.items))
            except ReadError as exc:
                if str(exc) == 'scope_withdrawn':
                    self.items, self.snapshot, self.error = {}, None, 'scope_withdrawn'
                raise
            # Register the freshest checked revision for subsequent local answers.
            self.items[identifier] = item
            if self.reviews is not None:
                self.reviews.reload()
            result = self._map(item, self.local_for(item, self.store.read()))
            result['synthetic'] = bool(self.reader.fixture)
            result['synthetic_plan'] = words(self.fixture_ui.get(identifier, {}).get('plan', ''))
            return result

    def conversation(self, identifier):
        with self.lock:
            if self.reviews is not None and identifier not in self.items:
                self.reviews.reload()
                if any(x['id'] == identifier for x in self.reviews.baseline()):
                    return {'messages': [], 'draft': '', 'notice': '原来源暂不可读取；只读保留起始记录。', 'synthetic_plan': '', 'read_only': True}
            self.registered(identifier)
            records = self.store.read()
            example = self.fixture_ui.get(identifier, {})
            return {'messages': [display_record(r) for r in example.get('messages', []) + records['messages'].get(identifier, [])],
                    'draft': records['drafts'].get(identifier, ''), 'notice': NOTICE,
                    'synthetic_plan': words(example.get('plan', '')), 'read_only': False}

    def write(self, identifier, mode, payload):
        if not isinstance(payload, dict):
            raise ValueError('invalid_body')
        with self.lock:
            item = None if mode == 'sync' else self.registered(identifier)
            content = payload.get('content', '')
            if not isinstance(content, str) or len(content) > 4000:
                raise ValueError('invalid_content')
            request_id = payload.get('request_id')
            if mode != 'draft' and (not valid_id(request_id) or len(request_id) > 100):
                raise ValueError('invalid_request_id')
            if mode == 'message' and not content.strip():
                raise ValueError('empty_message')
            action = payload.get('action')
            if mode == 'action':
                if not isinstance(action, str) or action not in ACTIONS or payload.get('revision') != version(item):
                    raise ValueError('invalid_or_stale_action')
                if action == 'resolve_plan' and not (item.get('recommendation') or self.fixture_ui.get(identifier, {}).get('plan')):
                    raise ValueError('no_plan')
                if action == 'answer_choice':
                    option = next((o for o in answer_options(item) if o['value'] == payload.get('choice')), None)
                    if not option or content != option['full']:
                        raise ValueError('invalid_choice')
                if action == 'write_memory' and not content.strip():
                    raise ValueError('empty_candidate')
                if action == 'defer':
                    try:
                        until = datetime.fromisoformat(payload.get('until', ''))
                        if until.tzinfo is None or until <= datetime.now(timezone.utc):
                            raise ValueError()
                    except (TypeError, ValueError) as exc:
                        raise ValueError('invalid_date') from exc
            if mode not in {'message', 'draft', 'action', 'sync'}:
                raise ValueError('invalid_operation')
            def mutate(data):
                if mode == 'draft':
                    data['drafts'][identifier] = content
                    return {'saved': True}
                rows = data['messages'].setdefault(identifier, []) if mode == 'message' else data['actions']
                existing = next((r for r in rows if r['request_id'] == request_id), None)
                if existing:
                    if (mode == 'message' and existing['content'] != content.strip()) or (mode != 'message' and (existing['id'] != identifier or existing['action'] != (action if mode == 'action' else 'sync') or existing['content'] != content or existing.get('choice', '') != payload.get('choice', ''))):
                        raise ValueError('request_id_conflict')
                    return {'saved': True, 'message': '已在本机记录，待同步'}
                if mode == 'message':
                    rows.append({'author': 'user', 'content': content.strip(), 'created_at': now(), 'request_id': request_id})
                    data['drafts'][identifier] = ''
                else:
                    rows.append({'id': identifier, 'action': action if mode == 'action' else 'sync',
                                 'choice': payload.get('choice', '') if action == 'answer_choice' else '',
                                 'revision': version(item) if item else '', 'content': content,
                                 'until': payload.get('until', ''), 'created_at': now(), 'request_id': request_id,
                                 'status': 'local_pending', 'message': '已在本机记录，待同步'})
                return {'saved': True, 'message': '已在本机记录，待同步'}
            return self.store.update(mutate)
