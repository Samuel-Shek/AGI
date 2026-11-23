"""Source-bound outcome checks within the existing closure sidecar.

Native source hashes bind a review, not its semantic correctness. File/JSON
predicates are deterministic; open-ended outcomes remain reviewer judgments.
No subprocess, model call, scheduler or separate database lives here.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path


def api():
    from . import task_closure
    return task_closure


def positive_finite(value):
    try:
        return type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        return False


def request(store, task_id):
    if not store.has_table('closure_request_sources'):
        return None
    row = store.conn.execute('SELECT * FROM closure_request_sources WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None


def contract(store, task_id):
    if not store.has_table('closure_requirement_contracts'):
        return None
    row = store.conn.execute('SELECT * FROM closure_requirement_contracts WHERE task_id=?', (task_id,)).fetchone()
    return {**dict(row), 'value': json.loads(row['value'])} if row else None


def requires_contract(store, task_id):
    return bool((request(store, task_id) or {}).get('policy') == 2 or store.conn.execute(
        "SELECT 1 FROM closure_events WHERE task_id=? AND kind='codex_registered_v2' LIMIT 1", (task_id,)).fetchone())


def omission_history(store, task_id, seen=None):
    """Only this declared lineage, bounded, with no historic chat/body scan."""
    seen = set() if seen is None else seen
    if task_id in seen or len(seen) >= 40: raise ValueError('requirement_lineage_invalid')
    seen.add(task_id)
    known, resolved = {}, {}
    spec = contract(store, task_id)
    parent = spec['value'].get('continuation_of') if spec else None
    if parent: known, resolved = omission_history(store, parent, seen)
    events = store.conn.execute("SELECT data FROM closure_events WHERE task_id=? AND kind='goal_reviewed_v1' ORDER BY seq LIMIT 1001", (task_id,)).fetchall()
    if len(events) > 1000: raise ValueError('goal_review_history_limit')
    for event in events:
        value = json.loads(event['data'])
        for item in value['omissions']: known.setdefault(item['issue_id'], item)
        for item in value['resolved_omissions']: resolved[item['issue_id']] = item
    return known, resolved


def observation_key(item, proof):
    # JSON's integer/float spelling is not a new observation. Valid observation
    # times are finite and no later than now; normalize their numeric identity.
    return api().digest([item['source_task_id'], item['id'], proof['path'], float(proof['observed_at']).hex()])


def observation_history(store, task_id, seen=None):
    """Keep the earliest accepted deadline even after waiting or continuation."""
    seen = set() if seen is None else seen
    if task_id in seen or len(seen) >= 40: raise ValueError('requirement_lineage_invalid')
    seen.add(task_id)
    spec = contract(store, task_id)
    parent = spec['value'].get('continuation_of') if spec else None
    limits = observation_history(store, parent, seen) if parent else {}
    events = store.conn.execute("SELECT data FROM closure_events WHERE task_id=? AND kind='goal_reviewed_v1' ORDER BY seq LIMIT 1001", (task_id,)).fetchall()
    if len(events) > 1000: raise ValueError('goal_review_history_limit')
    for event in events:
        for key, deadline in json.loads(event['data']).get('observation_limits', {}).items():
            limits[key] = min(limits.get(key, deadline), deadline)
    return limits


def review_history(store, task_id):
    return [*omission_history(store, task_id), observation_history(store, task_id)]


def check_observation_limit(item, proof, limits):
    if proof and proof['valid_until'] > limits.get(observation_key(item, proof), proof['valid_until']):
        raise ValueError('original_observation_deadline_extended')


def linked_issue_ids(store, task_id):
    return sorted(row[0] for row in store.conn.execute("SELECT id FROM closure_issues WHERE task_id=? OR id IN (SELECT issue_id FROM closure_events WHERE task_id=? AND kind='duplicate_linked')", (task_id, task_id)))


def lineage_current(store, spec):
    seen = set()
    while spec and spec['value'].get('continuation_of'):
        parent_id = spec['value']['continuation_of']
        if parent_id in seen or len(seen) >= 40: return False
        seen.add(parent_id)
        parent = contract(store, parent_id)
        if (not parent or spec['value'].get('parent_contract_sha256') != parent['sha256']
                or spec['value'].get('parent_issue_ids') != linked_issue_ids(store, parent_id)):
            return False
        spec = parent
    return True


def source(store, value, task):
    a = api()
    identifier = a.bounded_text(value.get('task_id'), 'requirement_source', 100)
    row = request(store, identifier)
    # A current pre-upgrade native root is sufficient to adopt the new contract;
    # it does not retroactively certify any old audit.
    if row is None and store.has_table('closure_hook_roots'):
        root = store.conn.execute('SELECT * FROM closure_hook_roots WHERE task_id=?', (identifier,)).fetchone()
        row = dict(root) if root else None
    if (not row or row['platform'] != task['platform'] or row['session'] != task['session']
            or value.get('prompt_sha256') != row['prompt_hash']):
        raise ValueError('requirement_source_not_native')
    return {'task_id': identifier, 'prompt_sha256': row['prompt_hash']}


def normalize(store, task, value):
    a = api()
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('requirements_version_required')
    sources = value.get('sources')
    items = value.get('items')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 40:
        raise ValueError('requirement_sources_required')
    sources = [source(store, row, task) for row in sources]
    source_ids = {row['task_id'] for row in sources}
    if len(source_ids) != len(sources) or task['id'] not in source_ids:
        raise ValueError('current_requirement_source_required')
    if not isinstance(items, list) or not 1 <= len(items) <= 40:
        raise ValueError('requirement_items_required')
    normalized, ids = [], set()
    for row in items:
        identifier = a.bounded_text(row.get('id'), 'requirement_id', 100)
        if identifier in ids or row.get('source_task_id') not in source_ids:
            raise ValueError('requirement_identity_invalid')
        ids.add(identifier)
        required = row.get('required', True)
        if type(required) is not bool:
            raise ValueError('requirement_required_invalid')
        check = row.get('verification')
        if not isinstance(check, dict) or check.get('kind') not in {'file', 'json', 'review'}:
            raise ValueError('requirement_verification_required')
        kind = check['kind']
        verify = {'kind': kind}
        if kind in {'file', 'json'}:
            path = a.bounded_text(check.get('path'), 'requirement_path', 4000)
            a.authority_scope({'scope': {'files': [path]}}, task['scope_root'])
            verify['path'] = path
            if kind == 'file':
                contains = check.get('contains', [])
                if not isinstance(contains, list) or len(contains) > 32:
                    raise ValueError('requirement_content_invalid')
                verify['contains'] = [a.bounded_text(x, 'required_content', 1000) for x in contains]
            else:
                expect = check.get('expect')
                if not isinstance(expect, dict) or not 1 <= len(expect) <= 32:
                    raise ValueError('requirement_json_expectation_required')
                for key, expected in expect.items():
                    a.bounded_text(key, 'json_key', 200)
                    if type(expected) not in (str, bool, int, float, type(None)):
                        raise ValueError('requirement_json_scalar_required')
                    if isinstance(expected, str): a.bounded_text(expected, 'json_value', 1000)
                verify['expect'] = expect
        else:
            verify['method'] = a.bounded_text(check.get('method'), 'review_method', 1000)
            if check.get('stage') not in {'content', 'installed', 'runtime', 'natural'}:
                raise ValueError('requirement_review_stage_required')
            verify['stage'] = check['stage']
        freshness = check.get('freshness')
        if freshness is not None:
            if not isinstance(freshness, dict): raise ValueError('requirement_freshness_invalid')
            path = a.bounded_text(freshness.get('path'), 'freshness_receipt_path', 4000)
            a.authority_scope({'scope': {'files': [path]}}, task['scope_root'])
            rule = {'path': path, 'observed_at_key': a.bounded_text(freshness.get('observed_at_key'), 'observation_time_key', 200)}
            if 'expires_at_key' in freshness:
                rule['expires_at_key'] = a.bounded_text(freshness['expires_at_key'], 'expiration_time_key', 200)
            if 'max_age_seconds' in freshness:
                age = freshness['max_age_seconds']
                if not positive_finite(age):
                    raise ValueError('requirement_freshness_age_invalid')
                rule['max_age_seconds'] = age
            if not ('expires_at_key' in rule or 'max_age_seconds' in rule):
                raise ValueError('requirement_freshness_limit_required')
            verify['freshness'] = rule
        elif kind == 'review' and verify['stage'] in {'runtime', 'natural'}:
            raise ValueError('live_observation_freshness_required')
        item = {'id': identifier, 'source_task_id': row['source_task_id'],
                'condition': a.bounded_text(row.get('condition'), 'requirement_condition', 1500),
                'required': required, 'verification': verify}
        if not required:
            item['optional_reason'] = a.bounded_text(row.get('optional_reason'), 'optional_reason', 1000)
        normalized.append(item)
    result = {'version': 1, 'sources': sources, 'items': normalized}
    if not any(x['required'] for x in normalized):
        raise ValueError('mandatory_result_required')
    previous = contract(store, task['id'])
    parent_id = value.get('continuation_of')
    if previous:
        if parent_id != previous['value'].get('continuation_of'):
            raise ValueError('requirement_lineage_immutable')
        if not {x['task_id'] for x in previous['value']['sources']} <= source_ids:
            raise ValueError('requirement_previous_sources_missing')
    if parent_id:
        parent = store.conn.execute('SELECT * FROM closure_tasks WHERE id=?', (parent_id,)).fetchone()
        inherited = contract(store, parent_id)
        if (not parent or not inherited or parent_id == task['id']
                or (parent['platform'], parent['session'], parent['scope_root']) !=
                (task['platform'], task['session'], task['scope_root'])):
            raise ValueError('requirement_continuation_invalid')
        result['continuation_of'] = parent_id
        result['parent_contract_sha256'] = inherited['sha256']
        result['parent_issue_ids'] = linked_issue_ids(store, parent_id)
        current_source, parent_source = request(store, task['id']), request(store, parent_id)
        if not current_source or not parent_source or current_source['at'] <= parent_source['at']:
            raise ValueError('requirement_continuation_not_newer')
        if not {x['task_id'] for x in inherited['value']['sources']} <= source_ids:
            raise ValueError('requirement_inherited_sources_missing')
        bases = [inherited['value']]
    else:
        bases = []
    if previous: bases.append(previous['value'])
    changes = value.get('changes', [])
    if not isinstance(changes, list) or len(changes) > 40:
        raise ValueError('requirement_changes_invalid')
    amendments = {}
    for change in changes:
        identifier = a.bounded_text(change.get('id'), 'changed_requirement', 100)
        if change.get('source_task_id') != task['id']:
            raise ValueError('requirement_change_needs_current_user_source')
        amendments[identifier] = {'id': identifier, 'source_task_id': task['id'],
                                 'reason': a.bounded_text(change.get('reason'), 'requirement_change_basis', 1500)}
    new = {x['id']: x for x in normalized}
    for base in bases:
        for old in base['items']:
            if old['required'] and new.get(old['id']) != old and not stronger(old, new.get(old['id'])):
                if old['id'] not in amendments:
                    raise ValueError('required_condition_changed_without_user_basis')
                if task['id'] in {x['task_id'] for x in base['sources']}:
                    raise ValueError('same_user_source_cannot_weaken_requirements')
    if amendments: result['changes'] = list(amendments.values())
    return result


def stronger(old, new):
    """Only mechanically provable predicate additions need no new user goal."""
    if not new or {k: v for k, v in old.items() if k != 'verification'} != {k: v for k, v in new.items() if k != 'verification'}:
        return False
    before, after = old['verification'], new['verification']
    if before.get('kind') != after.get('kind') or before.get('path') != after.get('path'):
        return False
    old_time, new_time = before.get('freshness'), after.get('freshness')
    if old_time:
        if not new_time or any(new_time.get(k) != v for k, v in old_time.items() if k != 'max_age_seconds'):
            return False
        if 'max_age_seconds' in old_time and (new_time.get('max_age_seconds', math.inf) > old_time['max_age_seconds']):
            return False
    if before['kind'] == 'file': return set(before['contains']) <= set(after['contains'])
    if before['kind'] == 'json':
        return all(k in after['expect'] and type(v) == type(after['expect'][k]) and v == after['expect'][k] for k, v in before['expect'].items())
    return ({k: v for k, v in before.items() if k != 'freshness'} ==
            {k: v for k, v in after.items() if k != 'freshness'})


def save(store, task, value):
    a = api()
    value = normalize(store, task, value)
    before = contract(store, task['id'])
    sha = a.digest(value)
    if before and before['sha256'] == sha:
        return before
    revision = before['revision'] + 1 if before else 1
    store.conn.execute('INSERT OR REPLACE INTO closure_requirement_contracts VALUES(?,?,?,?,?)',
                       (task['id'], revision, sha, json.dumps(value), time.time()))
    store.event('requirements_recorded_v1', {'revision': revision, 'sha256': sha,
                'previous': before['value'] if before else None}, task=task['id'])
    parent = value.get('continuation_of')
    if parent:
        linked = store.conn.execute("SELECT id FROM closure_issues WHERE task_id=? OR id IN (SELECT issue_id FROM closure_events WHERE task_id=? AND kind='duplicate_linked')", (parent, parent)).fetchall()
        for row in linked:
            if not store.conn.execute("SELECT 1 FROM closure_events WHERE task_id=? AND issue_id=? AND kind='duplicate_linked'", (task['id'], row['id'])).fetchone():
                store.event('duplicate_linked', {'reason': 'explicit_requirements_continuation'}, issue=row['id'], task=task['id'])
    return contract(store, task['id'])


def snapshot(rows, task):
    """Use the existing no-follow reader, including for reviewer evidence."""
    a = api()
    if not isinstance(rows, list) or not 1 <= len(rows) <= 40:
        raise ValueError('outcome_evidence_required')
    result = []
    for row in rows:
        path = a.bounded_text(row.get('path'), 'outcome_evidence_path', 4000)
        data = a.read_delivery_file(path, task['scope_root'], limit=2 * 1024 * 1024)
        sha = hashlib.sha256(data).hexdigest()
        if row.get('sha256') and row['sha256'] != sha:
            raise ValueError('outcome_evidence_changed')
        result.append({'path': path, 'sha256': sha})
    return result


def probe(item, task):
    a = api()
    check = item['verification']
    data = a.read_delivery_file(check['path'], task['scope_root'], limit=2 * 1024 * 1024)
    if check['kind'] == 'file':
        if check['contains'] and not all(x in data.decode('utf-8') for x in check['contains']):
            raise ValueError('required_content_missing')
    else:
        obj = json.loads(data)
        for key, expected in check['expect'].items():
            observed = obj
            for part in key.split('.'):
                if not isinstance(observed, dict) or part not in observed:
                    raise ValueError('required_receipt_field_missing')
                observed = observed[part]
            if type(observed) != type(expected) or observed != expected:
                raise ValueError('required_receipt_value_mismatch')
    return {'path': check['path'], 'sha256': hashlib.sha256(data).hexdigest()}


def freshness_probe(item, task):
    """Age the original observation, never the audit or filesystem mtime."""
    rule = item['verification'].get('freshness')
    if not rule: return None
    data = api().read_delivery_file(rule['path'], task['scope_root'], limit=2 * 1024 * 1024)
    obj = json.loads(data)
    def timestamp(key):
        value = obj
        for part in key.split('.'):
            if not isinstance(value, dict) or part not in value: raise ValueError('observation_time_missing')
            value = value[part]
        if not positive_finite(value):
            raise ValueError('observation_time_invalid')
        return value
    observed, now = timestamp(rule['observed_at_key']), time.time()
    if observed > now: raise ValueError('observation_time_in_future')
    deadlines = []
    if 'expires_at_key' in rule:
        expires = timestamp(rule['expires_at_key'])
        if expires <= observed: raise ValueError('observation_expiration_invalid')
        deadlines.append(expires)
    if 'max_age_seconds' in rule:
        deadline = observed + rule['max_age_seconds']
        if not math.isfinite(deadline): raise ValueError('observation_expiration_invalid')
        deadlines.append(deadline)
    if now >= min(deadlines): raise ValueError('outcome_evidence_expired')
    return {'path': rule['path'], 'sha256': hashlib.sha256(data).hexdigest(),
            'observed_at': observed, 'valid_until': min(deadlines)}


def prepare_review(store, task, payload, artifact, spec):
    a = api()
    if not spec:
        if requires_contract(store, task['id']):
            raise ValueError('source_bound_requirements_missing')
        return None
    if not lineage_current(store, spec): raise ValueError('inherited_requirement_contract_changed')
    known, prior_resolutions, observation_limits = review_history(store, task['id'])
    history_sha256 = a.digest([known, prior_resolutions, observation_limits])
    accepted_limits = {}
    raw = payload.get('goal_review')
    if not isinstance(raw, dict) or raw.get('contract_sha256') != spec['sha256']:
        raise ValueError('current_goal_review_required')
    if artifact.get('goal_review_sha256') != a.digest(raw):
        raise ValueError('goal_review_not_bound_to_reviewer')
    if raw.get('sources') != spec['value']['sources']:
        raise ValueError('goal_review_original_sources_missing')
    if raw.get('changes', []) != spec['value'].get('changes', []):
        raise ValueError('user_requirement_changes_not_reviewed')
    checked = raw.get('source_checks')
    if not isinstance(checked, list): raise ValueError('source_coverage_required')
    checked_sources = set()
    source_checks = []
    for check in checked:
        source_id = check.get('task_id')
        ids = check.get('requirement_ids')
        if source_id in checked_sources or not isinstance(ids, list): raise ValueError('source_coverage_invalid')
        expected = {x['id'] for x in spec['value']['items'] if x['source_task_id'] == source_id}
        if set(ids) != expected or len(ids) != len(expected): raise ValueError('source_coverage_mismatch')
        assessment = a.bounded_text(check.get('assessment'), 'source_coverage_assessment', 2000)
        source_checks.append({'task_id': source_id, 'requirement_ids': list(ids), 'assessment': assessment})
        checked_sources.add(source_id)
    if checked_sources != {x['task_id'] for x in raw['sources']}:
        raise ValueError('source_coverage_incomplete')
    rows = raw.get('outcomes')
    if not isinstance(rows, list) or len(rows) > 40: raise ValueError('outcomes_required')
    by_id = {x['id']: x for x in spec['value']['items']}
    seen, outcomes = set(), []
    linked = {x['id']: x for x in store.conn.execute("SELECT * FROM closure_issues WHERE task_id=? OR id IN (SELECT issue_id FROM closure_events WHERE task_id=? AND kind='duplicate_linked')", (task['id'], task['id']))}
    for row in rows:
        identifier = row.get('id')
        if identifier not in by_id or identifier in seen: raise ValueError('outcome_identity_invalid')
        seen.add(identifier)
        state = row.get('state')
        result = {'id': identifier, 'state': state}
        if state == 'passed':
            item = by_id[identifier]
            if item['verification']['kind'] in {'file', 'json'}:
                try:
                    result['evidence'] = [probe(item, task)]
                except (OSError, ValueError, UnicodeError, TypeError):
                    raise ValueError('requirement_result_failed:' + identifier) from None
            else:
                result['observation'] = a.bounded_text(row.get('observation'), 'outcome_observation', 2000)
                result['evidence'] = snapshot(row.get('evidence'), task)
            try:
                result['freshness_evidence'] = freshness_probe(item, task)
                check_observation_limit(item, result['freshness_evidence'], observation_limits)
                if result['freshness_evidence']:
                    proof = result['freshness_evidence']
                    accepted_limits[observation_key(item, proof)] = proof['valid_until']
            except (OSError, ValueError, UnicodeError, TypeError, OverflowError):
                raise ValueError('requirement_freshness_failed:' + identifier) from None
        elif state in {'pending', 'waiting'}:
            issue = linked.get(row.get('issue_id'))
            if not issue or issue['state'] in a.TERMINAL:
                raise ValueError('unmet_requirement_needs_open_issue')
            if state == 'waiting' and not (issue['state'] == 'decision' or store.declared_block(issue)):
                raise ValueError('requirement_wait_not_established')
            result['issue_id'] = issue['id']
        else:
            raise ValueError('outcome_state_invalid')
        outcomes.append(result)
    if {x['id'] for x in by_id.values() if x['required']} - seen:
        raise ValueError('required_outcomes_missing')
    omissions = raw.get('omissions', [])
    if not isinstance(omissions, list) or len(omissions) > 40: raise ValueError('source_omissions_invalid')
    findings = []
    for item in omissions:
        issue = linked.get(item.get('issue_id'))
        if not issue or issue['state'] in a.TERMINAL: raise ValueError('source_omission_needs_open_issue')
        if item.get('source_task_id') not in checked_sources: raise ValueError('source_omission_origin_invalid')
        condition = a.bounded_text(item.get('condition'), 'source_omission_condition', 1500)
        original = known.get(issue['id'])
        finding = {'source_task_id': item['source_task_id'], 'issue_id': issue['id'], 'condition': condition}
        if original and any(original[k] != finding[k] for k in finding):
            raise ValueError('source_omission_identity_changed')
        finding['existing_requirement_ids'] = original['existing_requirement_ids'] if original else sorted(by_id)
        findings.append(finding)
    known.update({x['issue_id']: x for x in findings})
    resolutions = raw.get('resolved_omissions', [])
    if not isinstance(resolutions, list) or len(resolutions) > 40: raise ValueError('omission_resolutions_invalid')
    for value in resolutions:
        issue_id = value.get('issue_id')
        if issue_id not in known: raise ValueError('omission_resolution_unknown')
        if value.get('kind') == 'verified_requirement':
            prior_resolutions[issue_id] = {'issue_id': issue_id, 'kind': value['kind'],
                'requirement_id': a.bounded_text(value.get('requirement_id'), 'resolved_requirement', 100)}
        elif value.get('kind') == 'removed_by_user':
            src = request(store, value.get('source_task_id'))
            origin = request(store, known[issue_id]['source_task_id'])
            if (value.get('source_task_id') != task['id'] or not src or not origin or src['at'] <= origin['at']):
                raise ValueError('omission_removal_needs_new_user_source')
            prior_resolutions[issue_id] = {'issue_id': issue_id, 'kind': value['kind'], 'source_task_id': task['id'],
                'reason': a.bounded_text(value.get('reason'), 'omission_user_change', 1500)}
        else: raise ValueError('omission_resolution_invalid')
    unresolved_ids = {x['issue_id'] for x in findings}
    normalized_resolutions = []
    for issue_id, finding in known.items():
        if issue_id in unresolved_ids: continue
        resolution = prior_resolutions.get(issue_id, {})
        if resolution.get('kind') == 'verified_requirement':
            item = by_id.get(resolution['requirement_id'])
            outcome = next((x for x in outcomes if x['id'] == resolution['requirement_id']), {})
            if (not item or not item['required'] or item['source_task_id'] != finding['source_task_id']
                    or item['id'] in finding['existing_requirement_ids'] or item['condition'] != finding['condition']
                    or outcome.get('state') != 'passed'):
                raise ValueError('source_omission_still_unverified')
        elif resolution.get('kind') == 'removed_by_user':
            if resolution['source_task_id'] not in checked_sources: raise ValueError('omission_change_source_missing')
        else: raise ValueError('source_omission_cannot_disappear')
        normalized_resolutions.append(resolution)
    return {'contract_sha256': spec['sha256'], 'review_sha256': a.digest(raw), 'history_sha256': history_sha256,
            'sources': spec['value']['sources'], 'source_checks': source_checks, 'outcomes': outcomes,
            'omissions': findings, 'resolved_omissions': normalized_resolutions,
            'observation_limits': accepted_limits}


def status(store, task, review, *, scope_allowed, spec):
    needed = requires_contract(store, task['id'])
    if not spec:
        return {'goal_contract_sha256': None, 'requirements_verified': False,
                'goal_complete': False if needed else None,
                'unmet_requirements': [{'id': None, 'reason': 'source_bound_requirements_missing'}] if needed else []}
    result = {'goal_contract_sha256': spec['sha256'], 'requirements_verified': False, 'goal_complete': False,
              'unmet_requirements': []}
    if not lineage_current(store, spec):
        result['unmet_requirements'] = [{'id': None, 'reason': 'inherited_requirement_contract_changed'}]
        return result
    accepted = review.get('goal_review', {})
    if not scope_allowed or accepted.get('contract_sha256') != spec['sha256']:
        result['unmet_requirements'] = [{'id': x['id'], 'reason': 'current_goal_review_missing'} for x in spec['value']['items'] if x['required']]
        return result
    known, _ = omission_history(store, task['id'])
    covered = {x['issue_id'] for x in accepted['omissions'] + accepted['resolved_omissions']}
    if set(known) != covered:
        result['unmet_requirements'] = [{'id': None, 'reason': 'source_omission_history_changed'}]
        return result
    result['requirements_verified'] = True
    observation_limits = observation_history(store, task['id'])
    outcomes = {x['id']: x for x in accepted['outcomes']}
    for item in spec['value']['items']:
        if not item['required']: continue
        row = outcomes.get(item['id'], {})
        reason = None
        if row.get('state') != 'passed': reason = row.get('state', 'missing')
        else:
            try:
                current = ([probe(item, task)] if item['verification']['kind'] in {'file', 'json'}
                           else snapshot(row['evidence'], task))
                if current != row['evidence']: reason = 'outcome_version_changed'
                try:
                    proof = freshness_probe(item, task)
                    check_observation_limit(item, proof, observation_limits)
                    if proof != row.get('freshness_evidence'):
                        reason = 'observation_receipt_changed'
                except (OSError, ValueError, UnicodeError, TypeError, OverflowError) as exc:
                    reason = 'outcome_evidence_expired' if str(exc) == 'outcome_evidence_expired' else 'observation_time_missing_invalid_or_future'
            except (OSError, ValueError, KeyError, UnicodeError, TypeError):
                reason = 'outcome_missing_invalid_or_changed'
        if reason: result['unmet_requirements'].append({'id': item['id'], 'reason': reason, **({'issue_id': row['issue_id']} if row.get('issue_id') else {})})
    for item in accepted['omissions']:
        result['unmet_requirements'].append({'id': None, 'reason': 'original_requirement_omitted', 'issue_id': item['issue_id']})
    result['goal_complete'] = not result['unmet_requirements']
    return result
