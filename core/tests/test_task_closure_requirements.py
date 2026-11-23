"""Outcome regressions use private fixtures; no live records or model calls."""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from activity_mvp import task_closure as c


class RequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'activity.sqlite'
        sqlite3.connect(self.database).close()
        self.store = c.Store(self.database)
        self.session = 'source-bound-test'
        self.turn = 'original'
        self.prompt = 'Generate A.txt containing alpha and B.txt containing beta. C is optional.'
        self.source = self.observe(self.turn, self.prompt)
        self.a = self.root / 'A.txt'; self.a.write_text('alpha')
        self.b = self.root / 'B.txt'; self.b.write_text('beta')
        self.spec = {'version': 1, 'sources': [self.source], 'items': [self.item('A', 'alpha'), self.item('B', 'beta')]}

    def tearDown(self):
        self.store.conn.close(); self.temp.cleanup()

    def observe(self, turn, prompt):
        identifier = c.digest(['codex', self.session, turn])[:32]
        sha = hashlib.sha256(prompt.encode()).hexdigest()
        self.store.observe_closure_request(identifier, 'codex', self.session, turn, sha)
        return {'task_id': identifier, 'prompt_sha256': sha}

    def item(self, name, contains, **extra):
        return {'id': name, 'source_task_id': self.source['task_id'], 'condition': 'Create correct ' + name,
                'required': True, 'verification': {'kind': 'file', 'path': str(self.root / (name + '.txt')), 'contains': [contains]}, **extra}

    def begin(self, spec=None, **extra):
        payload = {'platform': 'codex', 'session': self.session, 'turn': self.turn, 'objective': self.prompt,
                   'scope_root': str(self.root), 'complex': False, 'closure_mode': 'full', 'requirements': spec or self.spec, **extra}
        result = self.store.begin(payload); self.task = result['task_id']
        return result

    def issue(self, name):
        return self.store.register({'task_id': self.task, 'category': 'main', 'key': name,
            'summary': 'Required outcome ' + name + ' remains', 'target_root': str(self.root),
            'evidence': [{'path': str(self.a)}], 'authority': {'source': 'isolated request', 'scope': str(self.root),
                'reversible': True, 'cost': False, 'new_permission': False, 'external': False, 'personal_choice': False},
            'recommendation': 'finish actual outcome', 'acceptance_rule': 'actual result matches original requirement'})['issue']

    def review_payload(self, outcomes=None, completion='complete', omissions=None, **goal_extra):
        spec = c.goals.contract(self.store, self.task)
        goal = {'contract_sha256': spec['sha256'], 'sources': spec['value']['sources'],
                'source_checks': [{'task_id': s['task_id'],
                    'requirement_ids': [x['id'] for x in spec['value']['items'] if x['source_task_id'] == s['task_id']],
                    'assessment': 'Original synthetic request read; conjunctive A and B preserved; C is optional.'}
                    for s in spec['value']['sources']],
                'outcomes': outcomes if outcomes is not None else [{'id': x['id'], 'state': 'passed'} for x in spec['value']['items'] if x['required']],
                'omissions': omissions or [], **goal_extra}
        final = self.root / 'final.txt'; final.write_text('本次修复全部完成。' if completion == 'complete' else '完成一部分。仍有必达结果等待原负责人核验。')
        sha = hashlib.sha256(final.read_bytes()).hexdigest()
        review = self.root / 'review.json'
        review.write_text(json.dumps({'final_response_sha256': sha, 'wording_approved': True, 'goal_review_sha256': c.digest(goal)}))
        remaining = self.store.task_status(self.task)['remaining']
        return {'task_id': self.task, 'evidence': [{'path': str(self.a)}], 'independent': False,
            'reviewer': 'synthetic executor', 'wording_artifact': {'path': str(review)}, 'wording_approved': True,
            'review': {k: 'original source and actual fixtures checked' for k in ('main', 'related', 'discovered', 'failed_steps', 'verification_gaps', 'downstream')},
            'goal_review': goal,
            'final_response': {'path': str(final), 'sha256': sha, 'completion': completion, 'scope': '本次修复',
                'remaining': [{'scope': 'in_scope', 'issue_id': r['id'], 'summary': r['summary'], 'blocker': 'actual fixture limitation',
                    'owner': r['owner'], 'next_step': 'original owner verifies actual outcome', 'continuation': 'no background created'} for r in remaining],
                'user_action': {'required': False, 'scope': '当前检查', 'reason': '当前检查由执行者完成'}}}

    def test_native_new_full_cannot_omit_contract_or_fall_back_to_legacy(self):
        for mode in ('full', None):
            with self.assertRaisesRegex(ValueError, 'source_bound_requirements_required'):
                self.begin(requirements=None, closure_mode=mode)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM closure_tasks').fetchone()[0], 0)

    def test_source_hash_must_match_native_receipt(self):
        self.spec['sources'][0] = {**self.source, 'prompt_sha256': 'a' * 64}
        with self.assertRaisesRegex(ValueError, 'requirement_source_not_native'): self.begin()

    def test_only_a_registered_does_not_hide_b_missing_from_actual_results(self):
        self.begin(); item = self.issue('A')
        self.store.accept({'issue_id': item['id'], 'revision': 1, 'verified': True, 'reviewer': 'fixture', 'review': 'A read back', 'evidence': [{'path': str(self.a)}]})
        self.b.unlink()
        with self.assertRaisesRegex(ValueError, 'requirement_result_failed:B'): self.store.audit(self.review_payload())
        state = self.store.task_status(self.task)
        self.assertFalse(state['goal_complete']); self.assertFalse(state['delivery_ready'])
        self.assertEqual(state['remaining'], [])
        self.assertTrue(state['registered_items_complete'])
        self.assertFalse(state['audit_valid']); self.assertFalse(state['all_complete'])

    def test_closed_registered_items_without_audit_are_not_goal_acceptance(self):
        self.begin(); item = self.issue('A')
        self.store.accept({'issue_id': item['id'], 'revision': 1, 'verified': True, 'reviewer': 'fixture',
            'review': 'A read back', 'evidence': [{'path': str(self.a)}]})
        state = self.store.task_status(self.task)
        self.assertEqual(state['remaining'], [])
        self.assertTrue(state['registered_items_complete'])
        for key in ('audit_valid', 'goal_complete', 'all_complete', 'delivery_ready'): self.assertFalse(state[key])

    def test_closed_ledger_cannot_replace_high_risk_independent_qualification(self):
        self.begin(complex=True)
        payload = self.review_payload()
        with self.assertRaises(ValueError): self.store.audit(payload)
        payload['independent'] = True
        payload['independent_artifact'] = payload.pop('wording_artifact')
        self.assertTrue(self.store.audit(payload)['goal_complete'])
        # An isolated historical/incomplete audit row must not gain independent
        # qualification merely because its known items are all closed.
        self.store.conn.execute('UPDATE closure_audits SET independent=0 WHERE task_id=?', (self.task,))
        state = self.store.task_status(self.task)
        self.assertTrue(state['registered_items_complete'])
        for key in ('goal_complete', 'all_complete', 'delivery_ready'): self.assertFalse(state[key])

    def test_permission_expiry_during_status_clears_both_completion_claims(self):
        self.begin(); self.store.audit(self.review_payload())
        with patch.object(c, 'manual_closure_allowed', side_effect=lambda task, item=None, paths=(): item is None):
            state = self.store.task_status(self.task, allow_manual=True)
        for key in ('registered_items_complete', 'goal_complete', 'all_complete', 'delivery_ready'):
            self.assertFalse(state[key])

    def test_outcome_omission_is_rejected_even_when_b_exists(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, 'required_outcomes_missing'):
            self.store.audit(self.review_payload([{'id': 'A', 'state': 'passed'}]))

    def test_b_wrong_content_rejected(self):
        self.begin(); self.b.write_text('wrong')
        with self.assertRaisesRegex(ValueError, 'requirement_result_failed:B'): self.store.audit(self.review_payload())

    def test_normal_completion_and_optional_unbuilt_c_pass(self):
        self.spec['items'].append(self.item('C', 'optional', required=False, optional_reason='Original request expressly calls C optional'))
        self.begin(); result = self.store.audit(self.review_payload())
        self.assertTrue(result['registered_items_complete']); self.assertTrue(result['goal_complete'])
        self.assertTrue(result['delivery_ready']); self.assertEqual(result['stop_decision']['decision'], 'complete')
        self.assertFalse((self.root / 'C.txt').exists())

    def test_changed_result_invalidates_current_completion_without_new_audit(self):
        self.begin(); self.store.audit(self.review_payload()); self.b.write_text('beta but changed')
        result = self.store.task_status(self.task)
        self.assertTrue(result['registered_items_complete']); self.assertFalse(result['goal_complete'])
        self.assertFalse(result['wording_valid']); self.assertFalse(result['delivery_ready'])
        self.assertEqual(result['stop_decision']['decision'], 'continue_now')

    def test_json_receipt_tests_are_not_installation(self):
        receipt = self.root / 'receipt.json'; receipt.write_text(json.dumps({'tests': True, 'installed': False}))
        self.spec['items'][1]['verification'] = {'kind': 'json', 'path': str(receipt), 'expect': {'installed': True}}
        self.begin()
        with self.assertRaisesRegex(ValueError, 'requirement_result_failed:B'): self.store.audit(self.review_payload())
        receipt.write_text(json.dumps({'tests': True, 'installed': True}))
        self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])

    def timed_receipt(self, observed, expires, *, kind='json', max_age=None):
        path = self.root / 'runtime.json'
        path.write_text(json.dumps({'running': True, 'observed_at': observed, 'expires_at': expires}))
        freshness = {'path': str(path), 'observed_at_key': 'observed_at', 'expires_at_key': 'expires_at'}
        if max_age is not None: freshness['max_age_seconds'] = max_age
        self.spec['items'][1]['verification'] = ({'kind': 'json', 'path': str(path), 'expect': {'running': True}}
            if kind == 'json' else {'kind': 'review', 'method': 'Read the actual running-path observation', 'stage': kind})
        self.spec['items'][1]['verification']['freshness'] = freshness
        return path

    def test_expired_original_receipt_cannot_pass_or_be_refreshed_by_reaudit(self):
        now = time.time(); self.timed_receipt(now - 200, now - 100); self.begin()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'):
                self.store.audit(self.review_payload())
        self.assertFalse(self.store.task_status(self.task)['goal_complete'])

    def test_expiry_after_review_invalidates_then_new_observation_needs_reaudit(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60); self.begin()
        self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])
        with patch.object(c.goals.time, 'time', return_value=now + 61):
            status = self.store.task_status(self.task)
            self.assertTrue(status['registered_items_complete']); self.assertFalse(status['delivery_ready'])
            self.assertIn('outcome_evidence_expired', [x['reason'] for x in status['unmet_requirements']])
            with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'):
                self.store.audit(self.review_payload())
            receipt.write_text(json.dumps({'running': True, 'observed_at': now + 60, 'expires_at': now + 120}))
            self.assertFalse(self.store.task_status(self.task)['goal_complete'])
            self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])

    def test_runtime_review_uses_original_observation_deadline(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60, kind='runtime'); self.begin()
        outcomes = [{'id': 'A', 'state': 'passed'}, {'id': 'B', 'state': 'passed',
                    'observation': 'The specific running path was observed in the dated receipt', 'evidence': [{'path': str(receipt)}]}]
        self.assertTrue(self.store.audit(self.review_payload(outcomes))['goal_complete'])
        with patch.object(c.goals.time, 'time', return_value=now + 61):
            self.assertFalse(self.store.task_status(self.task)['goal_complete'])
            with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'):
                self.store.audit(self.review_payload(outcomes))

    def test_live_review_without_explicit_original_time_limit_is_rejected(self):
        for stage in ('runtime', 'natural'):
            self.spec['items'][1]['verification'] = {'kind': 'review', 'stage': stage, 'method': 'Check actual observation'}
            with self.assertRaisesRegex(ValueError, 'live_observation_freshness_required'): self.begin()

    def test_bad_missing_future_and_nonfinite_original_times_are_rejected(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60); self.begin()
        for bad in (None, True, 'old', -1, float('inf'), float('nan'), now + 30):
            with self.subTest(bad=bad):
                receipt.write_text(json.dumps({'running': True, 'observed_at': bad, 'expires_at': now + 60}))
                with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'): self.store.audit(self.review_payload())
        receipt.write_text(json.dumps({'running': True, 'expires_at': now + 60}))
        with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'): self.store.audit(self.review_payload())

    def test_freshness_limit_cannot_be_removed_or_extended_without_user_change(self):
        now = time.time(); self.timed_receipt(now - 1, now + 600, max_age=120); self.begin()
        check = self.spec['items'][1]['verification']; original = json.loads(json.dumps(check))
        for weaker in ({k: v for k, v in original.items() if k != 'freshness'},
                       {**original, 'freshness': {**original['freshness'], 'max_age_seconds': 240}}):
            self.spec['items'][1]['verification'] = weaker
            with self.assertRaisesRegex(ValueError, 'required_condition_changed_without_user_basis'): self.begin()
        self.spec['items'][1]['verification'] = {**original, 'freshness': {**original['freshness'], 'max_age_seconds': 60}}
        self.begin()

    def test_file_result_can_have_observation_maximum_age_and_static_files_need_no_expiry(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 600, max_age=30)
        timing = self.spec['items'][1]['verification']['freshness']; timing.pop('expires_at_key')
        self.spec['items'][1]['verification'] = {**self.item('B', 'beta')['verification'], 'freshness': timing}
        self.begin(); self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])
        with patch.object(c.goals.time, 'time', return_value=now + 31):
            self.assertFalse(self.store.task_status(self.task)['goal_complete'])
        self.assertEqual(receipt.read_text(), json.dumps({'running': True, 'observed_at': now - 1, 'expires_at': now + 600}))

    def test_static_artifact_acceptance_does_not_expire_only_because_time_passes(self):
        self.begin(); self.store.audit(self.review_payload())
        with patch.object(c.goals.time, 'time', return_value=time.time() + 365 * 86400):
            self.assertTrue(self.store.task_status(self.task)['goal_complete'])

    def test_old_observation_cannot_gain_time_by_editing_deadline_or_waiting_review(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60); self.begin()
        self.store.audit(self.review_payload())
        issue = self.issue('new observation required')
        self.store.transition(issue['id'], 'blocked', 'requires natural observation', 1, 'original owner observes again')
        self.store.audit(self.review_payload([{'id': 'A', 'state': 'passed'},
            {'id': 'B', 'state': 'waiting', 'issue_id': issue['id']}], 'partial'))
        with patch.object(c.goals.time, 'time', return_value=now + 61):
            receipt.write_text(json.dumps({'running': True, 'observed_at': now - 1, 'expires_at': now + 3600}))
            with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'):
                self.store.audit(self.review_payload(completion='partial'))
            self.assertFalse(self.store.task_status(self.task)['goal_complete'])

    def test_continuation_cannot_renew_same_observation_deadline(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60); self.begin()
        self.store.audit(self.review_payload()); parent = self.task
        self.turn = 'continued'; self.spec['sources'].append(self.observe(self.turn, 'Continue the original task'))
        self.spec['continuation_of'] = parent; self.begin()
        receipt.write_text(json.dumps({'running': True, 'observed_at': now - 1, 'expires_at': now + 3600}))
        with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'):
            self.store.audit(self.review_payload())

    def test_equal_integer_and_float_time_cannot_renew_observation_in_either_direction(self):
        now = int(time.time())
        for original, changed in ((now - 1, float(now - 1)), (float(now - 2), now - 2)):
            with self.subTest(original_type=type(original).__name__):
                receipt = self.timed_receipt(original, now + 60); self.begin()
                self.store.audit(self.review_payload())
                receipt.write_text(json.dumps({'running': True, 'observed_at': changed, 'expires_at': now + 3600}))
                with self.assertRaisesRegex(ValueError, 'requirement_freshness_failed:B'):
                    self.store.audit(self.review_payload())

    def test_time_evidence_retirement_checked_before_audit_or_status_reads(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60)
        timing = self.spec['items'][1]['verification']['freshness']
        self.spec['items'][1]['verification'] = {**self.item('B', 'beta')['verification'], 'freshness': timing}
        self.begin(); payload = self.review_payload(); self.store.audit(payload)
        real_read = c.read_delivery_file
        def guarded_read(path, *args, **kwargs):
            self.assertNotEqual(str(path), str(receipt), 'retired observation must not be opened')
            return real_read(path, *args, **kwargs)
        with patch.object(c.personal_scope, 'company_stopped', return_value=True), \
             patch.object(c.personal_scope, 'classify', side_effect=lambda row: 'company' if row['root'] == str(receipt) else 'personal'), \
             patch.object(c, 'manual_closure_allowed', return_value=False), \
             patch.object(c, 'read_delivery_file', side_effect=guarded_read):
            self.assertTrue(c.owner_retired({'scope_root': str(self.root), 'requirements': self.spec}))
            with self.assertRaisesRegex(ValueError, 'goal_evidence_scope_not_allowed'): self.store.audit(payload)
            self.assertFalse(self.store.task_status(self.task)['goal_complete'])
            denied = self.store.task_status(self.task, allow_manual=True)
            self.assertFalse(denied['goal_complete']); self.assertFalse(denied['registered_items_complete'])

    def test_temporary_permit_for_result_does_not_cover_observation_path(self):
        now = time.time(); receipt = self.timed_receipt(now - 1, now + 60)
        timing = self.spec['items'][1]['verification']['freshness']
        self.spec['items'][1]['verification'] = {**self.item('B', 'beta')['verification'], 'freshness': timing}
        self.begin(); payload = self.review_payload()
        def permit(task, item=None, paths=()):
            return str(receipt) not in [*c.metadata_paths(task), *c.metadata_paths(item), *paths]
        with patch.object(c.personal_scope, 'company_stopped', return_value=True), \
             patch.object(c.personal_scope, 'classify', side_effect=lambda row: 'company' if row['root'] == str(receipt) else 'personal'), \
             patch.object(c, 'manual_closure_allowed', side_effect=permit), \
             patch.object(c.goals, 'freshness_probe', side_effect=AssertionError('uncovered observation read')):
            with self.assertRaisesRegex(ValueError, 'goal_evidence_scope_not_allowed'): self.store.audit(payload)

    def test_semantic_omission_report_blocks_until_requirement_added(self):
        # This tests consumption of a real review finding, NOT automated semantics.
        self.spec['items'] = self.spec['items'][:1]
        self.begin(); missing = self.issue('B omitted from checklist')
        omission = {'source_task_id': self.source['task_id'], 'condition': 'Original request also requires B containing beta', 'issue_id': missing['id']}
        result = self.store.audit(self.review_payload(completion='partial', omissions=[omission]))
        self.assertFalse(result['goal_complete'])
        self.assertIn('original_requirement_omitted', [x['reason'] for x in result['unmet_requirements']])
        with self.assertRaisesRegex(ValueError, 'user_goal_not_verified'):
            self.store.audit(self.review_payload(omissions=[omission]))

    def test_established_wait_keeps_partial_without_new_choice_or_background(self):
        self.begin(); issue = self.issue('natural observation')
        self.store.transition(issue['id'], 'blocked', 'requires natural elapsed time', 1, 'original owner checks after natural sample')
        result = self.store.audit(self.review_payload([{'id': 'A', 'state': 'passed'}, {'id': 'B', 'state': 'waiting', 'issue_id': issue['id']}], 'partial'))
        self.assertFalse(result['goal_complete']); self.assertTrue(result['wording_valid'])
        self.assertEqual(result['stop_decision']['decision'], 'waiting')
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM closure_choices').fetchone()[0], 0)

    def test_self_declared_wait_without_real_block_rejected(self):
        self.begin(); issue = self.issue('B')
        with self.assertRaisesRegex(ValueError, 'requirement_wait_not_established'):
            self.store.audit(self.review_payload([{'id': 'A', 'state': 'passed'}, {'id': 'B', 'state': 'waiting', 'issue_id': issue['id']}], 'partial'))

    def test_continuation_cannot_silently_drop_or_downgrade_b(self):
        self.begin(); parent = self.task
        self.turn = 'human-followup'; src = self.observe(self.turn, 'Continue the same task')
        self.spec['sources'].append(src); self.spec['continuation_of'] = parent
        for change in ('drop', 'downgrade'):
            spec = json.loads(json.dumps(self.spec))
            if change == 'drop': spec['items'] = spec['items'][:1]
            else: spec['items'][1].update(required=False, optional_reason='executor suggestion')
            with self.assertRaisesRegex(ValueError, 'required_condition_changed_without_user_basis'): self.begin(spec)
        self.assertTrue(self.begin()['goal_contract_sha256'])

    def test_light_and_legacy_keep_separate_qualification(self):
        self.begin(requirements=None, closure_mode='light', artifact_paths=[])
        self.assertFalse(self.store.task_status(self.task)['closure_required'])
        self.assertEqual(self.store.task_status(self.task)['stop_decision']['decision'], 'not_applicable')
        legacy = self.store.begin({'platform': 'claude', 'session': 'old', 'turn': 'old', 'objective': 'legacy', 'scope_root': str(self.root), 'complex': False})
        self.assertIsNone(self.store.task_status(legacy['task_id'])['goal_complete'])

    def test_same_original_message_cannot_fake_user_downgrade(self):
        self.begin()
        self.spec['items'][1].update(required=False, optional_reason='executor alleges changed goal')
        self.spec['changes'] = [{'id': 'B', 'source_task_id': self.task, 'reason': 'alleged user change'}]
        with self.assertRaisesRegex(ValueError, 'same_user_source_cannot_weaken_requirements'): self.begin()
        self.assertTrue(c.goals.contract(self.store, self.task)['value']['items'][1]['required'])

    def test_unknown_raw_fields_in_review_are_not_retained(self):
        self.begin(); payload = self.review_payload()
        payload['goal_review']['source_checks'][0]['unused_private_original'] = 'SYNTHETIC PRIVATE RAW BODY'
        review = Path(payload['wording_artifact']['path']); doc = json.loads(review.read_text())
        doc['goal_review_sha256'] = c.digest(payload['goal_review']); review.write_text(json.dumps(doc))
        self.store.audit(payload)
        stored = self.store.conn.execute('SELECT review FROM closure_audits WHERE task_id=?', (self.task,)).fetchone()[0]
        self.assertNotIn('SYNTHETIC PRIVATE RAW BODY', stored); self.assertNotIn('unused_private_original', stored)

    def test_new_codex_without_native_source_cannot_become_legacy(self):
        self.turn = 'source-not-observed'
        with self.assertRaisesRegex(ValueError, 'source_bound_requirements_required'):
            self.begin(requirements=None)

    def test_new_light_cannot_upgrade_to_full_as_legacy_without_source(self):
        self.turn = 'source-not-observed'
        self.begin(requirements=None, closure_mode='light', artifact_paths=[])
        with self.assertRaisesRegex(ValueError, 'source_bound_requirements_required'):
            self.begin(requirements=None, closure_mode='full')
        self.store.observe_closure_operation(self.task, operational=True)
        self.assertFalse(self.store.task_status(self.task)['goal_complete'])

    def test_known_omission_cannot_disappear_after_accepting_only_a(self):
        self.spec['items'] = self.spec['items'][:1]
        self.begin(); issue = self.issue('B semantic omission')
        finding = {'source_task_id': self.source['task_id'], 'condition': 'B containing beta is required', 'issue_id': issue['id']}
        self.store.audit(self.review_payload(completion='partial', omissions=[finding]))
        self.store.accept({'issue_id': issue['id'], 'revision': 1, 'verified': True, 'reviewer': 'bad self review', 'review': 'only A checked', 'evidence': [{'path': str(self.a)}]})
        with self.assertRaisesRegex(ValueError, 'source_omission_cannot_disappear'):
            self.store.audit(self.review_payload())
        with self.assertRaisesRegex(ValueError, 'source_omission_still_unverified'):
            self.store.audit(self.review_payload(resolved_omissions=[{'issue_id': issue['id'], 'kind': 'verified_requirement', 'requirement_id': 'A'}]))
        self.spec['items'].append(self.item('B', 'beta', condition=finding['condition'])); self.begin()
        result = self.store.audit(self.review_payload(resolved_omissions=[{'issue_id': issue['id'], 'kind': 'verified_requirement', 'requirement_id': 'B'}]))
        self.assertTrue(result['goal_complete'])
        self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])

    def test_continuation_inherits_unresolved_issues_and_semantic_findings(self):
        self.spec['items'] = self.spec['items'][:1]
        self.begin(); parent = self.task; issue = self.issue('omitted B')
        finding = {'source_task_id': self.source['task_id'], 'condition': 'B required', 'issue_id': issue['id']}
        self.store.audit(self.review_payload(completion='partial', omissions=[finding]))
        self.turn = 'continue'; current = self.observe(self.turn, 'continue same task')
        self.spec['sources'].append(current); self.spec['continuation_of'] = parent; self.begin()
        self.assertEqual(self.store.task_status(self.task)['remaining'][0]['id'], issue['id'])
        without_parent = {k: v for k, v in self.spec.items() if k != 'continuation_of'}
        with self.assertRaisesRegex(ValueError, 'requirement_lineage_immutable'):
            self.begin(without_parent)
        with self.assertRaisesRegex(ValueError, 'source_omission_cannot_disappear'):
            self.store.audit(self.review_payload(completion='partial'))

    def test_strengthening_checks_is_allowed_but_old_acceptance_expires(self):
        self.spec['items'][1]['verification']['contains'] = []
        self.begin(); self.store.audit(self.review_payload())
        self.spec['items'][1]['verification']['contains'] = ['beta']; self.begin()
        self.assertFalse(self.store.task_status(self.task)['goal_complete'])
        self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])

    def test_concurrent_semantic_finding_cannot_be_overwritten_by_stale_audit(self):
        self.spec['items'] = self.spec['items'][:1]
        self.begin(); self.b.unlink()
        outer = self.review_payload()
        final = self.root / 'outer-final.txt'; final.write_bytes(Path(outer['final_response']['path']).read_bytes())
        artifact = self.root / 'outer-review.json'; artifact.write_bytes(Path(outer['wording_artifact']['path']).read_bytes())
        outer['final_response']['path'] = str(final); outer['wording_artifact']['path'] = str(artifact)
        real_status = c.goals.status
        fired = False
        def interleaved(*args, **kwargs):
            nonlocal fired
            value = real_status(*args, **kwargs)
            if not fired:
                fired = True
                issue = self.issue('concurrent B finding')
                finding = {'issue_id': issue['id'], 'source_task_id': self.task, 'condition': 'B required'}
                self.store.audit(self.review_payload(completion='partial', omissions=[finding]))
                self.store.accept({'issue_id': issue['id'], 'revision': 1, 'verified': True, 'reviewer': 'bad self review',
                    'review': 'only A checked', 'evidence': [{'path': str(self.a)}]})
            return value
        with patch.object(c.goals, 'status', side_effect=interleaved):
            with self.assertRaisesRegex(ValueError, 'omission_history_changed_during_audit'):
                self.store.audit(outer)
        self.assertFalse(self.store.task_status(self.task)['goal_complete'])

    def test_parent_new_requirement_invalidates_already_completed_child(self):
        self.spec['items'] = self.spec['items'][:1]
        self.begin(); parent = self.task
        parent_spec = json.loads(json.dumps(self.spec))
        self.turn = 'child'; current = self.observe(self.turn, 'Continue original task')
        self.spec['sources'].append(current); self.spec['continuation_of'] = parent
        self.begin(); child = self.task; self.store.audit(self.review_payload())
        self.assertTrue(self.store.task_status(child)['goal_complete'])
        parent_spec['items'].append(self.item('B', 'beta')); self.b.unlink()
        self.store.begin({'platform':'codex','session':self.session,'turn':'original','objective':self.prompt,
            'scope_root':str(self.root),'complex':False,'closure_mode':'full','requirements':parent_spec})
        self.assertFalse(self.store.task_status(child)['goal_complete'])
        with self.assertRaisesRegex(ValueError, 'inherited_requirement_contract_changed'):
            self.store.audit(self.review_payload())
        self.spec['items'].append(self.item('B', 'beta')); self.b.write_text('beta'); self.begin()
        self.assertTrue(self.store.audit(self.review_payload())['goal_complete'])

    def test_parent_new_issue_invalidates_completed_child_until_refreshed(self):
        self.begin(); parent = self.task
        self.turn = 'child'; current = self.observe(self.turn, 'Continue original task')
        self.spec['sources'].append(current); self.spec['continuation_of'] = parent
        self.begin(); child = self.task; self.store.audit(self.review_payload())
        self.task = parent; issue = self.issue('new related necessary item'); self.task = child
        self.assertFalse(self.store.task_status(child)['goal_complete'])
        self.begin()
        self.assertEqual(self.store.task_status(child)['remaining'][0]['id'], issue['id'])

    def test_alias_or_path_escape_is_not_a_valid_result(self):
        self.b.unlink(); self.b.symlink_to(self.a)
        with self.assertRaisesRegex(ValueError, 'authority_scope_alias'): self.begin()


if __name__ == '__main__': unittest.main()
