"""Runtime admission and sandbox path agreement, without a model request."""
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from activity_mvp.feedback import model, repair
from activity_mvp.feedback.store import Store


class RuntimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'state')
        self.store.set('config', {'enabled': True})
        self.store.set('model_capability', {'enabled': True})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def configure(self, binary):
        self.store.set('config', {'enabled': True, 'codex_binary': str(binary)})

    def test_missing_explicit_binary_does_not_reserve_or_fallback(self):
        self.configure(self.root / 'missing')
        with patch.object(model.shutil, 'which', return_value='/bin/sh'):
            with self.assertRaises(FileNotFoundError):
                model.invoke(self.store, 'synthetic fixture', {})
        self.assertEqual(self.store.rows('SELECT * FROM budget'), [])
        self.assertFalse((self.store.root / 'runs').exists())

    def test_nonexecutable_does_not_reserve(self):
        executable = self.root / 'codex'
        executable.write_text('synthetic fixture')
        executable.chmod(0o600)
        self.configure(executable)
        with self.assertRaises(PermissionError):
            model.invoke(self.store, 'synthetic fixture', {})
        self.assertEqual(self.store.rows('SELECT * FROM budget'), [])

    def test_directory_rejected_before_reservation(self):
        self.configure(self.root)
        with self.assertRaises(FileNotFoundError):
            model.invoke(self.store, 'synthetic fixture', {})
        self.assertEqual(self.store.rows('SELECT * FROM budget'), [])

    def test_no_path_lookup_when_configured_absolute_binary_exists(self):
        with patch.object(model.shutil, 'which', side_effect=AssertionError('PATH used')):
            self.assertEqual(model.runtime_binary('/bin/sh'), '/bin/sh')

    def test_failed_preflight_keeps_existing_unknown_budget(self):
        key = self.store.reserve('analysis')
        self.store.settle(key, 'unknown')
        before = self.store.rows('SELECT * FROM budget')
        self.configure(self.root / 'missing')
        with self.assertRaises(FileNotFoundError):
            model.invoke(self.store, 'synthetic fixture', {})
        self.assertEqual(self.store.rows('SELECT * FROM budget'), before)

    def test_model_and_sandbox_use_selected_binary_without_path(self):
        executable = self.root / 'selected runtime' / 'codex'
        executable.parent.mkdir()
        executable.write_text('fixture')
        host = executable.with_name('codex-code-mode-host')
        host.write_text('fixture')
        with patch.dict(os.environ, {'PATH': '/nonexistent'}):
            commands = [model.command(executable, self.root, self.root/'schema', self.root/'result'),
                        repair.sandbox_args(str(executable), self.root, ['/bin/echo', 'fixture'])]
        for command in commands:
            raw = next(command[i+1] for i, x in enumerate(command[:-1])
                       if x == '-c' and command[i+1].startswith('permissions='))
            profile = next(iter(tomllib.loads(raw)['permissions'].values()))
            self.assertEqual(profile['filesystem'][str(executable)], 'read')
            self.assertEqual(profile['filesystem'][str(host)], 'read')
            self.assertEqual(profile['filesystem'][':root'], 'deny')
            self.assertFalse(profile['network']['enabled'])
            self.assertNotIn(str(executable.parent), profile['filesystem'])

    def test_ranking_mode_reads_only_agi_and_missing_is_unknown(self):
        import sqlite3
        from activity_mvp.feedback.memory_coverage import ranking_mode
        path = self.root / 'ledger.sqlite'
        self.assertEqual(ranking_mode(path), 'unavailable')
        self.assertFalse(path.exists())
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE scenario_releases(caller,task,mode)')
            db.execute("INSERT INTO scenario_releases VALUES('mail-reliability','analysis_priority','assist')")
            db.execute("INSERT INTO scenario_releases VALUES('agi','analysis_priority','off')")
        self.assertEqual(ranking_mode(path), 'off')


if __name__ == '__main__':
    unittest.main()
