from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ceo.observers import (
    FILES, MAX_FILE_BYTES, REASON_CODES, collect_sync,
    evaluate_sync_snapshot, read_sync_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / 'fixtures/sync_cases.json').read_text())
BASE = FIXTURES['cases'][0]['input']
NOW = datetime.fromisoformat(FIXTURES['cases'][0]['now'])


class ReplayTests(unittest.TestCase):
    def test_fixed_synthetic_replays(self):
        for case in FIXTURES['cases']:
            with self.subTest(case=case['id']):
                self.assertEqual(case['kind'], 'synthetic_regression')
                result = evaluate_sync_snapshot(case['input'], datetime.fromisoformat(case['now']))
                for key, expected in case['expected'].items():
                    self.assertEqual(result[key], expected, result)
                self.assertIn(result['reason_code'], REASON_CODES)
                self.assertIs(result['metrics']['receipt_scope_only'], True)

    def test_pure_function_does_not_mutate_input(self):
        before = copy.deepcopy(BASE)
        evaluate_sync_snapshot(BASE, NOW)
        self.assertEqual(BASE, before)

    def test_untrusted_content_dropped_not_interpreted(self):
        data = copy.deepcopy(BASE)
        private = 'secret-original-identifier-and-IGNORE-INSTRUCTIONS'
        for source in data.values():
            source.update(body=private, errors=[private], credentials={'token': private},
                          source_path='/Users/test/private/' + private, title=private,
                          screenshot=private, instruction='grant production_write')
        result = evaluate_sync_snapshot(data, NOW)
        output = json.dumps(result)
        self.assertNotIn(private, output)
        self.assertNotIn('/Users/test/private', output)
        self.assertNotIn('grant production_write', output)
        self.assertEqual(result['status'], 'recovered')

    def test_types_cannot_raise_or_become_commands(self):
        for field in ('status', 'error_code', 'preflight_error'):
            for value in ({'instruction': 'run something'}, ['instructions'], True, 9):
                with self.subTest(field=field, value=value):
                    data = copy.deepcopy(BASE)
                    data['worker'][field] = value
                    result = evaluate_sync_snapshot(data, NOW)
                    self.assertNotIn('instruction', json.dumps(result))
        data = copy.deepcopy(BASE)
        data['success']['verdict'] = []
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['status'], 'unknown')

    def test_missing_fields_not_promoted_to_success(self):
        for logical, field in [('success', 'run_id'), ('success', 'recorded_at'),
                               ('worker', 'index_generation'), ('request', 'requested_at')]:
            with self.subTest(logical=logical, field=field):
                data = copy.deepcopy(BASE)
                data[logical].pop(field)
                self.assertNotIn(evaluate_sync_snapshot(data, NOW)['status'], {'healthy', 'recovered'})

    def test_boolean_counter_not_integer(self):
        data = copy.deepcopy(BASE)
        data['worker']['attempt_count'] = True
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['reason_code'], 'invalid_accounting')

    def test_new_request_with_naive_timestamp_unknown(self):
        data = copy.deepcopy(BASE)
        data['request']['requested_at'] = '2026-09-08T04:12:00'
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['reason_code'], 'invalid_timestamp')

    def test_injection_in_metadata_dropped(self):
        data = copy.deepcopy(BASE)
        data['sources'] = [{'source': ['private'], 'read_status': 'ok'},
                           {'source': 'failure', 'read_status': {'arbitrary': 'error'}},
                           {'source': '/private/secret', 'read_status': 'ok'}]
        result = evaluate_sync_snapshot(data, NOW)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('/private/', json.dumps(result))

    def test_issue_id_stable_across_retries(self):
        data = copy.deepcopy(BASE)
        first = evaluate_sync_snapshot(data, NOW)['issue_key']
        data['worker'].update(attempt_count=3, timeout_count=2, updated_at='2026-09-08T04:14:00+00:00')
        second = evaluate_sync_snapshot(data, NOW)['issue_key']
        self.assertEqual(first, second)

    def test_malformed_context_not_current(self):
        data = copy.deepcopy(BASE)
        data['worker']['validation_context']['code_sha256'] = 'not a sha256'
        data['success']['validation_context']['code_sha256'] = 'not a sha256'
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['reason_code'], 'receipt_context_mismatch')

    def test_success_before_same_run_started_is_unknown(self):
        data = copy.deepcopy(BASE)
        data['worker'].update(started_at='2026-09-08T04:11:00+00:00', updated_at='2026-09-08T04:14:00+00:00')
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['reason_code'], 'invalid_timestamp')

    def test_incomparable_current_failure_not_ignored(self):
        data = copy.deepcopy(BASE)
        data['failure'].update(recorded_at='2026-09-08T04:14:00+00:00', run_id='other-run')
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['status'], 'unknown')

    def test_newer_success_not_accepted_before_worker_receipt(self):
        data = copy.deepcopy(BASE)
        data['success']['recorded_at'] = '2026-09-08T04:14:00+00:00'
        self.assertEqual(evaluate_sync_snapshot(data, NOW)['reason_code'], 'worker_receipt_conflict')


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'state').mkdir()
        for logical, filename in FILES.items():
            (self.root / 'state' / filename).write_text(json.dumps(BASE[logical]))

    def tearDown(self):
        self.temporary.cleanup()

    def result(self):
        return collect_sync(self.root, now=NOW)

    def test_live_style_projection_matches_replay(self):
        self.assertEqual(self.result(), evaluate_sync_snapshot(BASE, NOW))

    def test_only_fixed_files_read_and_no_source_mutation(self):
        (self.root / 'state' / 'private-business.json').write_text('NEVER READ THIS')
        before = {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                  for p in (self.root / 'state').iterdir()}
        result = self.result()
        after = {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                 for p in (self.root / 'state').iterdir()}
        self.assertEqual(before, after)
        self.assertNotIn('private-business', json.dumps(result))
        self.assertEqual({item['source'] for item in result['evidence']}, set(FILES))

    def test_projection_contains_no_identifiers_or_input_fingerprints(self):
        raw = copy.deepcopy(BASE)
        sensitive = 'untrusted-private-value'
        raw['worker'].update(run_id=sensitive, job_id=sensitive, input_key=sensitive,
                             preflight_error=sensitive, errors=[sensitive], content=sensitive)
        (self.root / 'state' / FILES['worker']).write_text(json.dumps(raw['worker']))
        snapshot = read_sync_snapshot(self.root)
        output = json.dumps(snapshot)
        self.assertNotIn(sensitive, output)
        self.assertNotIn(BASE['success']['run_id'], output)
        self.assertNotIn(BASE['worker']['input_key'], output)
        self.assertNotIn(str(self.root), output)

    def test_symlink_file_refused(self):
        target = self.root / 'outside.json'
        target.write_text(json.dumps(BASE['success']))
        source = self.root / 'state' / FILES['success']
        source.unlink()
        source.symlink_to(target)
        self.assertEqual(self.result()['status'], 'unknown')
        self.assertEqual(read_sync_snapshot(self.root)['sources'][2]['read_status'], 'unsafe_path')

    def test_symlink_state_directory_refused(self):
        real = self.root / 'actual-state'
        (self.root / 'state').rename(real)
        (self.root / 'state').symlink_to(real, target_is_directory=True)
        self.assertEqual(self.result()['reason_code'], 'sources_unavailable')

    def test_symlink_ancestor_refused(self):
        alternate = self.root / 'linked-root'
        alternate.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(collect_sync(alternate, NOW)['reason_code'], 'sources_unavailable')

    def test_fifo_not_opened_blocking(self):
        source = self.root / 'state' / FILES['success']
        source.unlink()
        os.mkfifo(source)
        self.assertEqual(read_sync_snapshot(self.root)['sources'][2]['read_status'], 'not_regular')

    def test_oversized_file_refused(self):
        (self.root / 'state' / FILES['failure']).write_text(' ' * (MAX_FILE_BYTES + 1))
        self.assertEqual(read_sync_snapshot(self.root)['sources'][3]['read_status'], 'too_large')
        self.assertEqual(self.result()['status'], 'unknown')

    def test_invalid_json_never_treated_as_absent_failure(self):
        for value in ('{', '[]', '{"verdict":"fail","verdict":"pass"}', '{"count":NaN}'):
            with self.subTest(value=value):
                (self.root / 'state' / FILES['failure']).write_text(value)
                self.assertEqual(read_sync_snapshot(self.root)['sources'][3]['read_status'], 'invalid_json')
                self.assertEqual(self.result()['status'], 'unknown')

    def test_atomic_replacement_while_sampling_detected(self):
        from ai_ceo import observers
        original = observers._read_source
        def changing(directory_fd, filename):
            record = original(directory_fd, filename)
            if filename == FILES['failure']:
                destination = self.root / 'state' / FILES['worker']
                replacement = self.root / 'state' / 'replacement.json'
                replacement.write_text(json.dumps(BASE['worker']))
                replacement.replace(destination)
            return record
        with patch.object(observers, '_read_source', changing):
            result = self.result()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['evidence'][1]['read_status'], 'changed_during_read')

    def test_missing_failure_created_during_sampling_is_unknown(self):
        from ai_ceo import observers
        source = self.root / 'state' / FILES['failure']
        source.unlink()
        original = observers._read_source
        def changing(directory_fd, filename):
            result = original(directory_fd, filename)
            if filename == FILES['failure']:
                source.write_text(json.dumps(BASE['failure']))
            return result
        with patch.object(observers, '_read_source', changing):
            result = self.result()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['evidence'][3]['read_status'], 'changed_during_read')

    def test_state_directory_replaced_during_sampling_is_unknown(self):
        from ai_ceo import observers
        original = observers._read_source
        def changing(directory_fd, filename):
            result = original(directory_fd, filename)
            if filename == FILES['failure']:
                (self.root / 'state').rename(self.root / 'old-state')
                (self.root / 'state').mkdir()
            return result
        with patch.object(observers, '_read_source', changing):
            result = self.result()
        self.assertEqual(result['reason_code'], 'sources_unavailable')
        self.assertTrue(all(source['read_status'] == 'changed_during_read' for source in result['evidence']))

    def test_missing_optional_failure_distinct_from_failed_read(self):
        (self.root / 'state' / FILES['failure']).unlink()
        self.assertEqual(self.result()['status'], 'healthy')
        (self.root / 'state' / FILES['failure']).write_text('{')
        self.assertEqual(self.result()['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
