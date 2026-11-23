import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import local_web as web
import web_agent as agent


class WebAgentTests(unittest.TestCase):
    def test_plist_runs_foreground_and_recovers_after_failure(self):
        config = agent.configuration('/python', '/local_web.py')
        self.assertEqual(config['ProgramArguments'], ['/python', '-B', '/local_web.py', 'serve'])
        self.assertTrue(config['RunAtLoad'])
        self.assertIs(config['KeepAlive'], True)
        self.assertGreaterEqual(config['ThrottleInterval'], 10)
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, 'PLIST', Path(tmp) / 'job.plist'):
            agent.publish(config)
            with self.assertRaises(FileExistsError):
                agent.publish(config)
            self.assertTrue(agent.installed(config))
            with self.assertRaises(ValueError):
                agent.installed({'Label': 'other'})

    def test_stop_disables_before_unloading_and_absent_job_still_disables(self):
        for active in (True, False):
            with patch.object(agent, 'job_pid', return_value=42 if active else None), patch.object(agent, 'ctl', return_value=subprocess.CompletedProcess([], 0)) as ctl:
                agent.pause()
                calls = [call('disable', agent.TARGET)]
                if active:
                    calls.append(call('bootout', agent.TARGET, check=False))
                self.assertEqual(ctl.call_args_list, calls)

    def test_resume_registers_or_kickstarts_existing_job_without_replacing_it(self):
        for active in (True, False):
            with patch.object(agent, 'job_pid', return_value=42 if active else None), patch.object(agent, 'ctl', return_value=subprocess.CompletedProcess([], 0)) as ctl:
                agent.enable()
                action = call('kickstart', agent.TARGET) if active else call('bootstrap', agent.DOMAIN, str(agent.PLIST))
                self.assertEqual(ctl.call_args_list, [call('enable', agent.TARGET), action])

    def test_foreign_service_is_left_alone(self):
        with patch.object(agent, 'ctl', return_value=subprocess.CompletedProcess([], 0, '\tpath = /other\n', '')) as ctl:
            with self.assertRaises(ValueError):
                agent.pause()
            ctl.assert_called_once_with('print', agent.TARGET, check=False)

    def test_install_keeps_explicit_pause_and_refuses_foreign_port(self):
        with patch.object(agent, 'disabled', return_value=True), patch.object(agent, 'installed', return_value=True), patch.object(agent, 'job_pid', return_value=None), \
                patch.object(web, 'current', return_value=None), patch.object(agent, 'enable') as enable:
            self.assertNotEqual(web.managed('install'), 0)
            enable.assert_not_called()
        with patch.object(agent, 'installed', return_value=False), patch.object(agent, 'job_pid', return_value=None), \
                patch.object(web, 'current', return_value=None), patch.object(web, 'available_port', return_value=False), \
                patch.object(agent, 'publish') as publish:
            with self.assertRaises(ValueError):
                web.managed('install')
            publish.assert_not_called()

    def test_foreground_service_records_its_own_pid_after_binding(self):
        captured = []
        def server(argv, *, on_bound, allow_shutdown):
            self.assertFalse(allow_shutdown)
            self.assertEqual(captured, [])
            on_bound()
            self.assertEqual(argv[argv.index('--instance') + 1], captured[0]['instance'])
            return 1
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'DIRECTORY', Path(tmp).resolve()), \
                patch.object(agent, 'job_pid', return_value=os.getpid()), patch.object(web, 'save_receipt', side_effect=captured.append), patch('psi_sync_center.main', side_effect=server):
            self.assertEqual(web.serve(), 1)
        self.assertEqual(captured[0]['pid'], os.getpid())
        self.assertEqual(captured[0]['manager'], agent.LABEL)

    def test_conflicting_health_responses_are_not_treated_as_ours(self):
        for value in (b'[]', b'42', b'null'):
            with patch.object(web, 'private_json', return_value={'instance': 'saved'}), patch.object(web, 'request', return_value=value):
                self.assertIsNone(web.current())

    def test_failed_publication_keeps_the_manual_service_running(self):
        receipt = {'script': str(Path(web.__file__).resolve())}
        with patch.object(agent, 'installed', return_value=False), patch.object(agent, 'job_pid', return_value=None), patch.object(web, 'current', return_value=receipt), patch.object(agent, 'publish', side_effect=PermissionError), patch.object(web, 'stop') as stop:
            with self.assertRaises(PermissionError):
                web.managed('install')
            stop.assert_not_called()

    def test_job_pid_checks_loaded_source_and_waiting_state(self):
        for extra, expected in (('\tpid = 123\n', 123), ('', 0)):
            output = f'\tpath = {agent.PLIST}\n' + extra
            with patch.object(agent, 'ctl', return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertEqual(agent.job_pid(), expected)
