"""Behavioral reproduction: progress must not masquerade as a functional win."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from activity_mvp.feedback import task_maintenance as tasks, evolution_notices as notices

class ProgressNotificationRegression(unittest.TestCase):
    def test_progress_record_is_retained_locally_without_external_send(self):
        with tempfile.TemporaryDirectory() as directory, tasks.Ledger(Path(directory).resolve()/'state') as ledger:
            ledger.event({'kind':'baseline','identity_restored':True,'paired_installed':True})
            class Client:
                def preflight(self,target):return {}
                def send(self,*args):return 'unexpected-message'
                def get_message(self,*args):return {}
                def matches(self,*args):return True
            with patch.object(notices,'target_for',return_value={'chat_id':'synthetic'}):
                result=notices.deliver_pending(ledger,Client())
            self.assertEqual(result['status'],'quiet','A progress record is not a verified functional improvement')
            self.assertEqual(ledger.db.execute('SELECT state FROM events').fetchone()[0],'local_only')
