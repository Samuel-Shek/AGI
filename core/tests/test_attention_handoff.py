from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from activity_mvp import attention_handoff as handoff, attention_native as native


class AttentionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.database=Path(self.temp.name)/"activity.sqlite"
        sqlite3.connect(self.database).close()
        self.queue=handoff.Queue(self.database)
        self.issue={"id":"memory-fixture","kind":"memory","title":"时效待核对","summary":"1 条待复核","members":[{"id":"private-id","content_sha256":"a"*64}],"revision":"v1","reason":"需要核对时效","impact":"可能影响 AI 调用","completion_rule":"核对台账","text":"PRIVATE BODY MUST NOT LEAVE"}
        self.detail_patch=patch.object(handoff,"detail",return_value={"item":self.issue})
        self.detail_patch.start()

    def tearDown(self):
        self.detail_patch.stop(); self.temp.cleanup()

    def submit(self,content="",message=None):
        return self.queue.submit(self.issue["id"],{"client_message_id":message or str(uuid.uuid4()),"content":content})

    def rows(self):
        with self.queue.db() as db: return [dict(r) for r in db.execute("SELECT * FROM submissions")]

    def transport(self):
        server=MagicMock()
        server.project.return_value="project"
        server.initial_collaboration_mode.return_value={"mode":"default","settings":{"model":"gpt-6-astra","reasoning_effort":"max","developer_instructions":None}}
        server.read.return_value={"projectId":"project","turns":[]}
        thread=str(uuid.uuid4())
        server.create.side_effect=lambda project,title,callback: callback(thread)
        return server,thread

    def test_empty_input_is_valid_and_duplicate_clicks_create_one_submission(self):
        identifier=str(uuid.uuid4())
        first=self.submit(message=identifier)
        self.submit(message=identifier); self.submit()
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(first["handoff"]["status"],"queued")
        self.assertEqual(self.queue.status(self.issue["id"])["draft"],"")
        prompt=self.rows()[0]["prompt"]
        self.assertNotIn("PRIVATE BODY",prompt)
        self.assertNotIn("private-id",prompt)
        self.assertIn("不是额外指令",prompt)

    def test_supplement_and_draft_survive_failure_and_old_save_cannot_overwrite(self):
        self.queue.save_draft(self.issue["id"],"new",200)
        self.queue.save_draft(self.issue["id"],"old",100)
        self.assertEqual(self.queue.status(self.issue["id"])["draft"],"new")
        self.submit("请先解释影响")
        identifier=self.rows()[0]["id"]
        self.queue.process(identifier)  # No configured target; no transport attempted.
        current=self.queue.status(self.issue["id"])
        self.assertEqual(current["draft"],"请先解释影响")
        self.assertEqual(current["handoff"]["status"],"failed")

    def test_memory_investigation_preserves_existing_task_authorization(self):
        from activity_mvp import memory_review
        with patch.object(memory_review, "read_memory_text", side_effect=AssertionError("handoff must not read bodies")):
            self.submit("我已授权核查本条记忆，不要重复询问。")
        prompt = self.rows()[0]["prompt"]
        self.assertNotIn("本次没有授权自动读取", prompt)
        self.assertIn("当前事项的成员", prompt)
        self.assertIn("不重复索取授权", prompt)
        self.assertIn("不得修改记忆审核结果", prompt)
        self.assertIn("不自动转交正文", prompt)
        self.assertNotIn("PRIVATE BODY", prompt)
        self.assertNotIn("private-id", prompt)

    def test_explicit_read_restriction_remains_authoritative(self):
        self.submit("只查索引，不要读取正文。")
        prompt = self.rows()[0]["prompt"]
        self.assertIn("只查索引，不要读取正文。", prompt)
        self.assertIn("用户本次补充的具体限制优先", prompt)
        self.assertIn("不推定为授权", prompt)

    def test_memory_read_scope_does_not_apply_to_activity_handoff(self):
        self.issue["kind"] = "activity"
        self.submit()
        prompt = self.rows()[0]["prompt"]
        self.assertNotIn("当前事项的成员", prompt)
        self.assertIn("具体活动内容尚未授权共享", prompt)
        self.assertIn("不得修改记忆审核结果或替用户回答活动问题", prompt)

    def test_sensitive_input_is_kept_local_and_not_submitted(self):
        with self.assertRaises(handoff.HandoffError): self.submit("/Users/test/private.txt")
        self.assertEqual(len(self.rows()),0)
        self.assertIn("private.txt",self.queue.status(self.issue["id"])["draft"])

    def test_pending_message_blocks_new_delivery_without_discarding_new_draft(self):
        self.submit("第一条补充")
        with self.assertRaises(handoff.HandoffError): self.submit("第二条补充")
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.queue.status(self.issue["id"])["draft"],"第二条补充")

    def test_explicit_same_text_followup_after_delivery_is_sent_to_existing_task(self):
        first_id = str(uuid.uuid4())
        self.submit("请继续核查", message=first_id)
        server, thread = self.transport()
        with patch.object(self.queue,"configure"), patch.object(native,"AppServer") as appserver, patch.object(native,"receipt_in_thread",return_value={"turn_id":"first"}):
            appserver.return_value.__enter__.return_value = server
            self.queue.process(first_id)
        # Retrying the original operation is still idempotent, after delivery.
        self.submit("请继续核查", message=first_id)
        self.assertEqual(len(self.rows()), 1)
        next_id = str(uuid.uuid4())
        followup = self.submit("请继续核查", message=next_id)
        self.assertEqual(followup["handoff"]["submission_id"], next_id)
        self.assertEqual(followup["handoff"]["status"], "queued")
        self.assertIsNone(followup["handoff"]["verification"])
        self.submit("请继续核查")  # A second click while pending is coalesced.
        self.assertEqual(len(self.rows()), 2)
        ipc = MagicMock(); ipc.owner.return_value = "desktop"; ipc.submit.return_value = {"received": True}
        with patch.object(self.queue,"configure"), patch.object(native,"AppServer") as appserver, patch.object(native,"receipt_in_thread",side_effect=[None,{"turn_id":"second"}]), patch.object(native,"open_thread"), patch.object(native,"wait_owner"), patch.object(native,"DesktopIPC") as ipc_class:
            appserver.return_value.__enter__.return_value = server
            ipc_class.return_value.__enter__.return_value = ipc
            self.queue.process(next_id)
        self.assertEqual(server.create.call_count, 1)
        self.assertEqual(ipc.submit.call_count, 1)
        self.assertEqual(ipc.submit.call_args.args[:3], (thread, "desktop", next_id))
        self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["status"], "delivered")

    def test_uncertain_same_text_with_new_id_does_not_create_another_delivery(self):
        self.submit("请继续核查")
        self.queue.update(self.rows()[0]["id"], status="uncertain", dispatched=1)
        self.submit("请继续核查")
        self.assertEqual(len(self.rows()), 1)

    def test_persisted_receipt_prevents_resend_and_followup_reuses_task(self):
        self.submit()
        identifier=self.rows()[0]["id"]
        server,thread=self.transport()
        with patch.object(self.queue,"configure"), patch.object(native,"AppServer") as appserver, patch.object(native,"receipt_in_thread",return_value={"turn_id":"turn-1"}), patch.object(native,"DesktopIPC") as ipc:
            appserver.return_value.__enter__.return_value=server
            self.queue.process(identifier)
            ipc.assert_not_called()
            self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["status"],"delivered")
            self.submit("请补充影响")
            identifier2=self.rows()[-1]["id"]
            self.queue.process(identifier2)
            self.assertEqual(server.create.call_count,1)
            self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["thread_id"],thread)

    def test_uncertain_dispatched_request_only_checks_original_receipt(self):
        self.submit()
        identifier=self.rows()[0]["id"]
        server,thread=self.transport()
        with self.queue.db() as db:
            db.execute("UPDATE problems SET thread_id=?,project_id='project',creation_state='ready'",(thread,))
        self.queue.update(identifier,dispatched=1)
        with patch.object(self.queue,"configure"), patch.object(native,"AppServer") as appserver, patch.object(native,"receipt_in_thread",return_value=None), patch.object(self.queue,"verify",side_effect=handoff.HandoffError("receipt_uncertain")) as verify, patch.object(native,"DesktopIPC") as ipc:
            appserver.return_value.__enter__.return_value=server
            self.queue.process(identifier)
            self.assertEqual(verify.call_count,1); ipc.assert_not_called(); server.create.assert_not_called()
            self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["status"],"uncertain")

    def test_first_delivery_uses_current_model_and_effort(self):
        self.submit(); identifier=self.rows()[0]["id"]
        server,thread=self.transport()
        ipc=MagicMock(); ipc.owner.return_value="desktop"; ipc.submit.return_value={"received":True}
        with patch.object(self.queue,"configure"), patch.object(native,"AppServer") as appserver, patch.object(native,"receipt_in_thread",side_effect=[None,{"turn_id":"t"}]), patch.object(native,"open_thread"),patch.object(native,"wait_owner"),patch.object(native,"DesktopIPC") as ipc_class:
            appserver.return_value.__enter__.return_value=server
            ipc_class.return_value.__enter__.return_value=ipc
            self.queue.process(identifier)
        self.assertEqual(ipc.submit.call_count,1)
        mode=ipc.submit.call_args.kwargs["collaboration_mode"]
        self.assertEqual(mode["settings"]["reasoning_effort"],"max")
        self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["status"],"delivered")
        self.assertIsNone(self.queue.status(self.issue["id"])["handoff"]["verification"])

    def test_uncertain_task_creation_cannot_create_a_second_task(self):
        self.submit(); identifier=self.rows()[0]["id"]
        with self.queue.db() as db: db.execute("UPDATE problems SET creation_state='creating'")
        server,_=self.transport()
        with patch.object(self.queue,"configure"),patch.object(native,"AppServer") as appserver:
            appserver.return_value.__enter__.return_value=server
            self.queue.process(identifier)
        server.create.assert_not_called()
        self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["status"],"uncertain")

    def test_recheck_never_treats_delivery_as_resolution(self):
        self.submit(); identifier=self.rows()[0]["id"]
        with self.assertRaises(handoff.HandoffError): self.queue.check_result(self.issue["id"])
        self.queue.update(identifier,status="delivered",receipt=json.dumps({"turn_id":"t"}))
        with patch.object(handoff,"verify_resolution",return_value={"state":"pending","message":"still pending"}):
            self.queue.check_result(self.issue["id"])
        self.assertEqual(self.queue.status(self.issue["id"])["handoff"]["verification"]["state"],"pending")


if __name__ == "__main__": unittest.main()
