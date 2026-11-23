import contextlib
import io
import json
import sqlite3
import hashlib
import unittest
from unittest.mock import patch, MagicMock

from activity_mvp import task_closure as closure, closure_trace, attention_center
from activity_mvp import retirement_snapshot as reader
from activity_mvp import attention_native as native
from tests import test_task_closure as fixtures
from tests import test_task_closure_requirements as requirement_fixtures


class ClosureTraceTests(unittest.TestCase):
    setUp = fixtures.ClosureTests.setUp
    tearDown = fixtures.ClosureTests.tearDown
    begin = fixtures.ClosureTests.begin
    issue = fixtures.ClosureTests.issue
    transport = fixtures.ClosureTests.transport

    def trace(self, item):
        return closure_trace.trace(self.db, item["id"])

    def rows(self):
        return {r[0]: [tuple(x) for x in self.store.conn.execute('SELECT * FROM "'+r[0]+'"')]
                for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def accepted(self):
        item = self.issue(category="related")
        return self.store.accept({"issue_id":item["id"], "revision":item["revision"], "verified":True,
            "reviewer":"isolated fixture", "review":"actual fixture checked", "evidence":[{"path":str(self.source)}]})

    def test_trace_cli_and_detail_are_readonly_and_do_not_dispatch(self):
        item = self.accepted(); before = self.rows()
        with patch.object(closure.Store,"dispatch",side_effect=AssertionError("no dispatch")), \
             patch.object(closure,"_notify_pending",side_effect=AssertionError("no notification")), \
             patch.object(closure.Store,"observe_runtime",side_effect=AssertionError("no IPC")):
            trace = self.trace(item)
            with sqlite3.connect(self.db) as activity:
                detail = attention_center.detail(activity, "closure:"+item["id"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                closure._main(["trace","--id",item["id"],"--db",str(self.db)])
        self.assertEqual(trace["state"],"verified")
        self.assertEqual(detail["item"]["closure_trace"]["id"],trace["id"])
        self.assertEqual(json.loads(output.getvalue())["state"],"verified")
        self.assertIsNotNone(trace["metrics"]["registration_to_acceptance_seconds"])
        self.assertEqual(before,self.rows())
        self.assertIsNone(trace["metrics"]["user_reminder_count"])
        self.assertIsNone(trace["metrics"]["notification_seen_count"])
        self.assertIsNone(trace["metrics"]["cost"])

    def test_page_handoff_reads_never_create_migrate_write_or_send(self):
        from activity_mvp import attention_handoff as handoff
        item = self.issue(); identifier = "closure:"+item["id"]
        root = self.db.parent/"attention-center"
        def snapshot():
            return {str(p.relative_to(self.db.parent)):(p.stat().st_mode, hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in self.db.parent.rglob("*") if p.is_file()}
        with patch.object(handoff.Queue,"wake",side_effect=AssertionError("no worker")), \
             patch.object(handoff.Queue,"check_result",side_effect=AssertionError("no write verification")), \
             patch.object(native,"AppServer",side_effect=AssertionError("no model or chat read")):
            # Missing queue remains missing; a plain page read doesn't initialize it.
            before = snapshot()
            for _ in range(2):
                result = handoff.command(self.db,"handoff-status",identifier,{})
                self.assertEqual(result["status"],"ok"); self.assertIsNone(result["handoff"])
                self.assertEqual(result["queue_state"],"absent"); self.assertTrue(result["history_unknown"])
            self.assertFalse((root/"handoff.sqlite").exists()); self.assertEqual(snapshot(),before)
            # Existing delivered receipt is rechecked in memory, without refreshing
            # the persisted verification or accepting the issue.
            handoff.command(self.db,"draft",identifier,{"content":"explicit local edit","draft_revision":1})
            self.assertTrue((root/"handoff.sqlite").is_file())
            self.assertEqual(handoff.command(self.db,"handoff-status",identifier,{})["draft"],"explicit local edit")
            queue = handoff.Queue(self.db)
            with queue.db() as db:
                db.execute("INSERT INTO problems(id,title) VALUES(?,?)",(identifier,"fixture"))
                db.execute("INSERT INTO submissions(id,problem_id,content,prompt,status,receipt,created,updated,source,verification) VALUES(?,?,?,?,'delivered',?,1,1,?,?)",
                           ("fixture",identifier,"minimal note","not read by trace",'{}',json.dumps({"id":identifier,"kind":"closure"}),'{"state":"resolved","checked_at":1}'))
            before = snapshot()
            result = handoff.command(self.db,"handoff-status",identifier,{})
            self.assertEqual(result["handoff"]["verification"]["state"],"pending")
            self.assertEqual(result["queue_state"],"present"); self.assertFalse(result["history_unknown"])
            with queue.db() as db:
                db.execute("UPDATE submissions SET source=?",(json.dumps({"kind":"memory","id":identifier}),))
            before=snapshot()
            with patch.object(handoff,"verify_resolution",side_effect=AssertionError("no unrelated source read")):
                self.assertEqual(handoff.command(self.db,"handoff-status",identifier,{})["handoff"]["verification"]["state"],"unavailable")
            self.assertEqual(snapshot(),before)
            # Readonly mode refuses writes even if accidentally reused.
            readonly = handoff.Queue(self.db,readonly=True)
            with self.assertRaises(sqlite3.OperationalError):
                with readonly.db() as db: db.execute("DELETE FROM drafts")
            self.assertEqual(snapshot(),before)
            # An old schema is unknown; viewing it does not migrate it.
            path=root/"handoff.sqlite"; path.unlink()
            with sqlite3.connect(path) as db: db.execute("CREATE TABLE drafts(problem_id TEXT PRIMARY KEY,content TEXT)")
            path.chmod(0o600); before=snapshot()
            self.assertEqual(handoff.command(self.db,"handoff-status",identifier,{})["status"],"unavailable")
            self.assertEqual(snapshot(),before)
            path.chmod(0o644); before=snapshot()
            self.assertEqual(handoff.command(self.db,"handoff-status",identifier,{})["status"],"unavailable")
            self.assertEqual(snapshot(),before)
            path.chmod(0o600)
            target=root/"original.sqlite"; path.rename(target); path.symlink_to(target)
            before=snapshot()
            self.assertEqual(handoff.command(self.db,"handoff-status",identifier,{})["status"],"unavailable")
            self.assertEqual(snapshot(),before)

    def test_real_continuation_is_not_a_duplicate_registration(self):
        fixture = requirement_fixtures.RequirementsTests()
        fixture.setUp()
        try:
            fixture.begin(); item = fixture.issue("A")
            def read():
                before = {r[0]:[tuple(x) for x in fixture.store.conn.execute('SELECT * FROM "'+r[0]+'"')]
                          for r in fixture.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                result = closure_trace.trace(fixture.database, item["id"])
                after = {r[0]:[tuple(x) for x in fixture.store.conn.execute('SELECT * FROM "'+r[0]+'"')]
                         for r in fixture.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(before, after)
                for private in ("source_task", "explicit_requirements_continuation"):
                    self.assertNotIn(private, json.dumps(result))
                return result
            self.assertEqual(read()["metrics"]["duplicate_registration_count"], 0)
            for level in range(2):
                parent = fixture.task
                fixture.turn = "continuation-"+str(level)
                fixture.spec["sources"].append(fixture.observe(fixture.turn, "continue same isolated outcome"))
                fixture.spec["continuation_of"] = parent
                fixture.begin()
                traced = read()
                self.assertEqual(traced["metrics"]["duplicate_registration_count"], level)
                self.assertEqual(sum(e["label"] == "已续接原事项" for e in traced["timeline"]), level+1)
                # Saving exactly the same native continuation is idempotent.
                fixture.begin()
                saved = read()
                self.assertEqual(saved["metrics"], traced["metrics"])
                self.assertEqual(saved["timeline"], traced["timeline"])
                duplicate = fixture.issue("A")
                self.assertEqual(duplicate["id"], item["id"])
                self.assertEqual(read()["metrics"]["duplicate_registration_count"], level+1)
        finally:
            fixture.tearDown()

    def test_duplicate_and_digest_count_have_distinct_units_and_no_private_data(self):
        item = self.issue(); self.issue()
        self.store.event("explicit_choice", {"choice":"keep_waiting","token":"SECRET TOKEN"}, issue=item["id"],task=self.task)
        self.store.event("state_changed", {"private":"PRIVATE BODY"}, issue=item["id"],task=self.task,notify=True)
        self.store.event("state_changed", {"private":"PRIVATE BODY"}, issue=item["id"],task=self.task,notify=True)
        sequences=[r[0] for r in self.store.conn.execute("SELECT event_seq FROM closure_notifications")]
        self.store.conn.execute("UPDATE closure_notifications SET requested=1")
        self.store.event("notification_digest_requested",{"event_seqs":sequences,"requested":True})
        result=self.trace(item); metrics=result["metrics"]
        self.assertEqual(metrics["duplicate_registration_count"],1)
        self.assertEqual(metrics["explicit_decision_count"],1)
        self.assertEqual(metrics["notification_request_count"],1)
        self.assertEqual(metrics["notification_request_records"],2)
        text=json.dumps(result)
        for secret in ("SECRET TOKEN","PRIVATE BODY",str(self.source),"prompt","authority"):
            self.assertNotIn(secret,text)
        # A mixed historical/new set still has an unknown total, not just the
        # known one-batch lower bound.
        self.store.event("state_changed",{},issue=item["id"],task=self.task,notify=True)
        self.store.conn.execute("UPDATE closure_notifications SET requested=1")
        self.assertIsNone(self.trace(item)["metrics"]["notification_request_count"])

    def test_received_does_not_mean_verified_and_uncertain_does_not_resend(self):
        item=self.issue()
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='received' WHERE id=?",(item["id"],))
        self.assertEqual(self.trace(item)["state"],"received")
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='send_uncertain' WHERE id=?",(item["id"],))
        before=self.rows()
        self.assertEqual(self.trace(item)["state"],"unknown")
        self.assertEqual(self.rows(),before)

    def test_notification_uncertainty_is_unknown_without_replay(self):
        item=self.issue()
        self.store.event("state_changed",{},issue=item["id"],task=self.task,notify=True)
        seq=self.store.conn.execute("SELECT max(event_seq) FROM closure_notifications").fetchone()[0]
        self.store.conn.execute("UPDATE closure_notifications SET error='request_uncertain' WHERE event_seq=?",(seq,))
        before=self.rows()
        self.assertIsNone(self.trace(item)["metrics"]["notification_request_count"])
        self.assertEqual(before,self.rows())
        self.store.event("state_changed",{},issue=item["id"],task=self.task,notify=True)
        confirmed=self.store.conn.execute("SELECT max(event_seq) FROM closure_notifications").fetchone()[0]
        self.store.conn.execute("UPDATE closure_notifications SET requested=1 WHERE event_seq=?",(confirmed,))
        self.store.event("notification_digest_requested",{"event_seqs":[confirmed],"requested":True})
        self.assertIsNone(self.trace(item)["metrics"]["notification_request_count"])
        rejected=self.issue(key="rejected-notification")
        self.store.event("state_changed",{},issue=rejected["id"],task=self.task,notify=True)
        denied=self.store.conn.execute("SELECT max(event_seq) FROM closure_notifications").fetchone()[0]
        self.store.conn.execute("UPDATE closure_notifications SET error='notification_rejected' WHERE event_seq=?",(denied,))
        self.assertEqual(self.trace(rejected)["metrics"]["notification_request_count"],0)

    def test_real_dispatch_and_reconcile_path_has_visible_events(self):
        item=self.issue(); server,thread=self.transport()
        ipc=MagicMock(); ipc.submit.return_value={"received":True}
        ipc.snapshot.return_value={"source":"desktop_owner","activity":"idle","turn_status":"completed"}
        with patch.object(native,"AppServer") as factory, patch.object(native,"DesktopIPC") as ipc_factory, \
             patch.object(native,"open_thread"), patch.object(native,"wait_owner",return_value="owner"), \
             patch.object(native,"receipt_in_thread",return_value={"turn_id":"fixture"}):
            factory.return_value.__enter__.return_value=server
            ipc_factory.return_value.__enter__.return_value=ipc
            self.store.dispatch(item["id"])
        result=self.trace(item)
        self.assertEqual(result["codex_thread_id"],thread)
        self.assertEqual(result["state"],"verification")
        self.assertEqual(ipc.submit.call_count,1)
        labels={row["label"] for row in result["timeline"]}
        self.assertIn("已开始原任务派发",labels)
        self.assertIn("已确认原任务接收",labels)
        self.assertIn("已核对原回执",labels)

    def test_waiting_and_cancel_stay_real_and_preview_is_not_called(self):
        item=self.issue(authority={"source":"fixture","scope":str(self.root),"personal_choice":True})
        with patch.object(closure.Store,"choice_preview",side_effect=AssertionError("no choice tokens")):
            self.assertEqual(self.trace(item)["state"],"waiting_decision")
            self.assertEqual(self.trace(item)["metrics"]["explicit_decision_count"],0)
        preview=self.store.choice_preview(item["id"])
        self.store.decide({"token":preview["token"],"issue_id":item["id"],"revision":item["revision"],"choice":"keep_waiting"})
        self.assertEqual(self.trace(item)["state"],"waiting_decision")
        item=self.store.get(item["id"]); preview=self.store.choice_preview(item["id"])
        self.store.decide({"token":preview["token"],"issue_id":item["id"],"revision":item["revision"],"choice":"cancel"})
        self.assertEqual(self.trace(item)["state"],"canceled")
        self.assertEqual(self.issue()["state"],"canceled")

    def test_old_acceptance_or_unreadable_evidence_cannot_show_verified(self):
        item=self.accepted()
        with patch.object(reader._EvidenceReader,"read",side_effect=reader.EvidenceReadUnavailable("unavailable")):
            result=self.trace(item)
        self.assertEqual(result["state"],"unknown")
        self.assertIsNone(result["metrics"]["registration_to_acceptance_seconds"])
        self.source.write_text("changed")
        self.assertEqual(self.trace(item)["state"],"verification")
        self.assertEqual(self.store.get(item["id"])["state"],"resolved")

    def test_concurrent_revision_change_rejects_mixed_trace(self):
        item=self.issue(); original=closure._projection
        def racing(*args):
            values=original(*args)
            self.store.transition(item["id"],"blocked","fixture race",item["revision"],recovery="fixture wait")
            return values
        with patch.object(closure,"_projection",side_effect=racing):
            with self.assertRaisesRegex(ValueError,"trace_issue_changed"):
                self.trace(item)

    def test_acceptance_evidence_changes_after_projection(self):
        item=self.accepted(); original=closure._projection
        def racing(*args):
            result=original(*args)
            self.source.write_text("changed after first projection")
            return result
        with patch.object(closure,"_projection",side_effect=racing):
            result=closure_trace.detail(self.db,item["id"])
        self.assertEqual(result["item"]["state"],"verification")
        self.assertEqual(result["trace"]["state"],"verification")
        self.assertFalse(result["trace"]["evidence"]["current_acceptance"])
        self.assertIsNone(result["trace"]["metrics"]["registration_to_acceptance_seconds"])

    def test_timeline_is_bounded_but_counts_include_all_events(self):
        item=self.issue()
        for _ in range(205): self.store.event("duplicate_linked",{},issue=item["id"],task=self.task)
        result=self.trace(item)
        self.assertTrue(result["timeline_truncated"])
        self.assertEqual(len(result["timeline"]),200)
        self.assertEqual(result["metrics"]["duplicate_registration_count"],205)

    def test_policy_change_rejects_trace(self):
        item=self.accepted()
        with patch.object(reader.Snapshot,"unchanged",return_value=False):
            with self.assertRaisesRegex(ValueError,"policy_changed"):
                self.trace(item)

    def test_concurrent_notification_batch_has_one_read_snapshot(self):
        self.store.conn.execute("PRAGMA journal_mode=WAL")
        item=self.issue()
        self.store.event("state_changed",{},issue=item["id"],task=self.task,notify=True)
        sequences=[r[0] for r in self.store.conn.execute("SELECT event_seq FROM closure_notifications")]
        original=closure.Store.__init__; sent=[]
        def instrument(store,*args,**kwargs):
            original(store,*args,**kwargs)
            if store.readonly:
                def callback(sql):
                    if "count(DISTINCT d.seq)" in sql and not sent:
                        sent.append(True)
                        with self.store.transaction():
                            self.store.conn.execute("UPDATE closure_notifications SET requested=1")
                            self.store.event("notification_digest_requested",{"event_seqs":sequences,"requested":True})
                store.conn.set_trace_callback(callback)
        with patch.object(closure.Store,"__init__",instrument):
            first=self.trace(item)["metrics"]
        self.assertEqual(first["notification_request_count"],0)
        self.assertEqual(first["notification_request_records"],0)
        second=self.trace(item)["metrics"]
        self.assertEqual(second["notification_request_count"],1)
        self.assertEqual(second["notification_request_records"],1)


if __name__ == "__main__": unittest.main()
