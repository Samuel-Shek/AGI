"""A display read can reuse exact text, but never mutable input or new policy."""
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
from activity_mvp.presentation_scope import DisplayScope, POLICY_FILE, display_scope, read_view
from activity_mvp import presentation_scope as presentation


class PresentationRequestCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.policy=self.root/POLICY_FILE
        self.write_policy('FormerOrg')

    def write_policy(self,name):
        self.policy.write_text(json.dumps({'version':1,'enabled':True,'retired_names':[name],
            'retired_paths':['/offline/company'],'retired_root_refs':['rootref_retired']}))
        self.policy.chmod(0o600)

    def test_repeated_fields_in_large_collection_keep_same_visible_counts(self):
        scope=DisplayScope(self.root)
        with patch.object(scope,'_hidden_text',wraps=scope._hidden_text) as checks:
            value=scope.project({'items':[{'title':'Personal note','state':'pending'} for _ in range(1200)]})
            self.assertEqual(len(value['items']),1200)
            self.assertLess(checks.call_count,10)

    def test_cached_hidden_text_remains_hidden_in_details_and_lists(self):
        scope=DisplayScope(self.root)
        for text in ('FORMERORG meeting','/offline/company/note','rootref_retired'):
            self.assertTrue(scope.hidden(text));self.assertTrue(scope.hidden(text))
        self.assertEqual(scope.project({'item':{'title':'FORMERORG meeting'}})['status'],'unavailable')

    def test_mutating_a_container_cannot_borrow_old_personal_result(self):
        scope=DisplayScope(self.root);row={'title':'Personal note'}
        self.assertFalse(scope.hidden(row));row['title']='FormerOrg meeting'
        self.assertTrue(scope.hidden(row));row['title']='Personal note'
        self.assertFalse(scope.hidden(row))

    def test_escaped_nested_json_cannot_borrow_plain_personal_text_result(self):
        scope=DisplayScope(self.root);self.assertFalse(scope.hidden('Personal note'))
        encoded=json.dumps({'title':'FormerOrg'},ensure_ascii=True).replace('FormerOrg','\\u0046ormerOrg')
        self.assertTrue(scope.hidden(encoded));self.assertTrue(scope.hidden(encoded))

    def test_policy_change_withdraws_a_previously_cached_projection(self):
        scope=DisplayScope(self.root);self.assertFalse(scope.hidden('NewOrganization'))
        self.write_policy('NewOrganization')
        self.assertEqual(scope.project({'items':[{'title':'NewOrganization'}]})['status'],'unavailable')
        self.assertTrue(DisplayScope(self.root).hidden('NewOrganization'))

    def test_nested_reads_load_same_policy_once_and_next_refresh_is_fresh(self):
        original=DisplayScope
        with patch('activity_mvp.presentation_scope.DisplayScope',wraps=original) as loads:
            with display_scope(self.root) as first:
                with display_scope(self.root) as nested:self.assertIs(first,nested)
            with display_scope(self.root) as next_read:self.assertIsNot(first,next_read)
            self.assertEqual(loads.call_count,2)

    def test_nested_other_runtime_cannot_borrow_the_policy(self):
        with display_scope(self.root) as first:
            with display_scope(self.root/'different') as other:self.assertIsNot(first,other)
            with display_scope(self.root) as again:self.assertIs(first,again)

    def test_shared_policy_is_checked_once_per_call_and_again_on_next_call(self):
        scope = DisplayScope(self.root)
        policy = Mock(); policy.unchanged.return_value = True
        scope.source_policy = scope.memory_policy = policy
        self.assertTrue(scope.memory_unchanged())
        self.assertEqual(policy.unchanged.call_count, 1)
        policy.unchanged.return_value = False
        self.assertFalse(scope.memory_unchanged())
        self.assertEqual(policy.unchanged.call_count, 2)

    def test_distinct_policies_are_both_checked_and_mid_projection_revocation_withholds_result(self):
        scope = DisplayScope(self.root)
        first, second = Mock(), Mock()
        first.unchanged.return_value = second.unchanged.return_value = True
        scope.source_policy, scope.memory_policy = first, second
        self.assertTrue(scope.memory_unchanged())
        first.unchanged.assert_called_once(); second.unchanged.assert_called_once()
        second.unchanged.return_value = False
        self.assertFalse(scope.memory_unchanged())
        scope.memory_policy = scope.source_policy
        first.unchanged.side_effect = [True, False]
        self.assertEqual(scope.project({"items": [{"title": "Personal"}]})["status"], "unavailable")

    def test_actual_sql_views_share_only_exact_long_row_text_within_one_scope(self):
        scope = DisplayScope(self.root)
        long_text = "个人笔记" * 1500
        connections = [sqlite3.connect(":memory:") for _ in range(2)]
        for conn in connections:
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE notes(id TEXT,body TEXT)")
            conn.execute("INSERT INTO notes VALUES('same',?)", (long_text,))
        with patch.object(scope, "_hidden_text", wraps=scope._hidden_text) as checks:
            for conn in connections:
                view = read_view(conn, scope, ("notes",))
                self.assertEqual(view.execute("SELECT body FROM notes").fetchall(), [(long_text,)])
            self.assertEqual(sum(call.args == (long_text,) for call in checks.call_args_list), 1)
            connections[1].execute("UPDATE notes SET body=?", (long_text + " FormerOrg",))
            self.assertEqual(view.execute("SELECT body FROM notes").fetchall(), [])
        self.assertNotEqual(scope.visible_row("id", "body"), scope.visible_row("body", "FormerOrg"))

    def test_sql_cache_does_not_survive_scope_or_policy_change(self):
        conn = sqlite3.connect(":memory:"); self.addCleanup(conn.close)
        conn.execute("CREATE TABLE notes(body TEXT)")
        conn.execute("INSERT INTO notes VALUES('FutureOrganization')")
        first = DisplayScope(self.root)
        self.assertEqual(read_view(conn, first, ("notes",)).execute("SELECT body FROM notes").fetchall(), [("FutureOrganization",)])
        self.write_policy("FutureOrganization")
        self.assertEqual(first.project({"items": [{"title": "FutureOrganization"}]})["status"], "unavailable")
        second = DisplayScope(self.root)
        self.assertFalse(second._row_visibility)
        self.assertEqual(read_view(conn, second, ("notes",)).execute("SELECT body FROM notes").fetchall(), [])

    def test_row_cache_evicts_with_unicode_byte_bounds_and_skips_oversize_rows(self):
        scope = DisplayScope(self.root)
        with patch.object(presentation, "_ROW_CACHE_ENTRIES", 2), patch.object(presentation, "_ROW_CACHE_BYTES", 30):
            self.assertEqual(scope.visible_row("甲" * 4), 1)
            scope.visible_row("乙" * 4); scope.visible_row("甲" * 4)
            scope.visible_row("丙" * 4)
            self.assertNotIn(("乙" * 4,), scope._row_visibility)
            self.assertEqual(scope._row_visibility_bytes, 24)
            self.assertEqual(scope.visible_row("FormerOrg" + "丁" * 20), 0)
            self.assertEqual(len(scope._row_visibility), 2)
            self.assertEqual(scope._row_visibility_bytes, 24)

    def test_cache_stays_bounded_and_large_or_late_text_is_still_checked(self):
        scope=DisplayScope(self.root)
        for n in range(50005):self.assertFalse(scope.hidden('Personal '+str(n)))
        self.assertLessEqual(len(scope._text_visibility),50000)
        self.assertLessEqual(scope._text_visibility_bytes,8*1024*1024)
        self.assertTrue(scope.hidden('x'*5000+'FormerOrg'))
        self.assertFalse(scope.hidden('x'*5000))
        self.assertTrue(scope.hidden('Late FormerOrg company'))

    def test_saturated_byte_budget_reuses_late_text_and_evicts_oldest(self):
        scope=DisplayScope(self.root)
        for n in range(2300):scope.hidden(str(n)+'x'*3900)
        remaining=8*1024*1024-scope._text_visibility_bytes
        self.assertLess(remaining,4096)
        scope.hidden('~'*remaining)
        self.assertEqual(scope._text_visibility_bytes,8*1024*1024)
        self.assertNotIn('0'+'x'*3900,scope._text_visibility)
        with patch.object(scope,'_hidden_text',wraps=scope._hidden_text) as checks:
            for _ in range(100):self.assertTrue(scope.hidden('Late FormerOrg company'))
            self.assertEqual(checks.call_count,1)
        self.assertLessEqual(scope._text_visibility_bytes,8*1024*1024)

    def test_recent_hit_survives_entry_eviction_and_unicode_bytes_are_bounded(self):
        scope=DisplayScope(self.root)
        scope.hidden('Keep recent')
        for n in range(49999):scope.hidden(str(n))
        scope.hidden('Keep recent');scope.hidden('new entry')
        self.assertIn('Keep recent',scope._text_visibility)
        self.assertNotIn('0',scope._text_visibility)
        for n in range(3000):scope.hidden(str(n)+'个人'*500)
        self.assertLessEqual(len(scope._text_visibility),50000)
        self.assertEqual(scope._text_visibility_bytes,
            sum(len(x.encode('utf-8',errors='surrogatepass')) for x in scope._text_visibility))
        self.assertLessEqual(scope._text_visibility_bytes,8*1024*1024)
