import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from activity_mvp.feedback import sources,memory_operator
from activity_mvp.feedback.store import digest
from tests.personal_scope_fixtures import register_personal

class SeedReadBoundTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name).resolve()/'seed.md';self.path.write_text('original\n');sources._SEED_READ_FAILURES.clear()
  register_personal(self, self.path.parent, source_ids=['seed'])
  self.message={'source_id':'seed','reference':json.dumps({'path':str(self.path),'sha256':digest('original\n')})}
 def tearDown(self):self.tmp.cleanup();sources._SEED_READ_FAILURES.clear()
 def test_current_bytes_still_required(self):
  self.assertTrue(sources.reference_is_current(self.message));self.path.write_text('edited\n');self.assertFalse(sources.reference_is_current(self.message))
 def test_timeout_blocks_only_that_source_without_repeated_reads(self):
  with patch.object(memory_operator,'bounded',side_effect=ValueError('source_read_timeout')) as read:
   self.assertFalse(sources.reference_is_current(self.message));self.assertFalse(sources.reference_is_current(self.message));self.assertEqual(read.call_count,1)
  other=self.path.with_name('other.md');other.write_text('original\n');message={**self.message,'reference':json.dumps({'path':str(other),'sha256':digest('original\n')})}
  self.assertTrue(sources.reference_is_current(message))
 def test_changed_file_rechecks_after_failed_read(self):
  with patch.object(memory_operator,'bounded',side_effect=ValueError('source_read_timeout')):self.assertFalse(sources.reference_is_current(self.message))
  self.path.write_text('new\n')
  with patch.object(memory_operator,'bounded',return_value=b'new\n') as read:
   self.assertFalse(sources.reference_is_current(self.message));self.assertEqual(read.call_count,1)
 def test_failed_read_expires_and_can_recover(self):
  with patch.object(sources.time,'monotonic',return_value=10),patch.object(memory_operator,'bounded',side_effect=ValueError('source_read_timeout')):self.assertFalse(sources.reference_is_current(self.message))
  with patch.object(sources.time,'monotonic',return_value=71):self.assertTrue(sources.reference_is_current(self.message))
 def test_success_is_never_cached(self):
  with patch.object(memory_operator,'bounded',side_effect=[b'original\n',b'different\n']) as read:
   self.assertTrue(sources.reference_is_current(self.message));self.assertFalse(sources.reference_is_current(self.message));self.assertEqual(read.call_count,2)
 def test_change_during_read_does_not_validate_old_bytes(self):
  def read(_):self.path.write_text('new\n');return b'original\n'
  with patch.object(memory_operator,'bounded',side_effect=read):self.assertFalse(sources.reference_is_current(self.message))
 def test_universal_newlines_match_original_reader(self):
  self.path.write_bytes(b'original\r\n');self.assertTrue(sources.reference_is_current(self.message))
