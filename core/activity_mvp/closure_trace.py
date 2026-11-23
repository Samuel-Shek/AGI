"""Read-only, metadata-only traces of existing closure issues; no new store."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

EVENT_LABELS = {
    "registered": "已登记", "duplicate_linked": "重复线索关联原事项",
    "state_changed": "登记状态变化", "explicit_choice": "已记录本人明确选择",
    "accepted": "已记录验收", "recurrence_version": "来源变化，重新待验收",
    "dispatch_started": "已开始原任务派发", "native_acknowledged": "已确认原任务接收",
    "receipt_checked": "已核对原回执", "dispatch_uncertain": "原送达结果待核对",
    "canceled_effects_checked": "已核对取消后的原动作",
    "recovery_registered": "已登记恢复检查", "recovery_checked": "已核对恢复条件",
}


def _timestamp(value):
    return value if type(value) in (float, int) and math.isfinite(value) and 0 < value <= time.time() else None


def detail(database, identifier):
    """Reuse the policy-filtered projection and reject a mixed-version read."""
    from . import task_closure as closure
    from .presentation_scope import display_scope
    from .retirement_snapshot import source_snapshot, EvidenceReadUnavailable

    identifier = identifier.removeprefix("closure:")
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("trace_issue_id_invalid")
    with source_snapshot(closure.personal_scope, bounded_evidence=True), display_scope(Path(database).resolve().parent) as scope:
        items = scope.project(closure._projection(database, identifier))
        if not items:
            raise ValueError("trace_issue_unavailable")
        item = items[0]
        with closure.Store(database, readonly=True) as store:
            # Sender batches can change without changing the issue revision.
            # All counters and rows must come from one SQLite read snapshot.
            store.conn.execute("BEGIN")
            observed_at = time.time()
            row = store.get(identifier)
            if str(row["revision"]) != item["revision"]:
                raise ValueError("trace_issue_changed")
            marker = (row["revision"], row["updated"], store.conn.execute(
                "SELECT max(seq) FROM closure_events WHERE issue_id=?", (identifier,)).fetchone()[0])
            retired = closure.owner_retired(row)
            events = [] if retired else store.conn.execute(
                """SELECT seq,kind,at, CASE WHEN kind='duplicate_linked' AND
                json_extract(CASE WHEN json_valid(data) THEN data ELSE '{}' END,'$.reason')=
                    'explicit_requirements_continuation' THEN 1 ELSE 0 END
                FROM closure_events WHERE issue_id=? ORDER BY seq DESC LIMIT 201""", (identifier,)).fetchall()
            counts = {} if retired else dict(store.conn.execute(
                "SELECT kind,count(*) FROM closure_events WHERE issue_id=? GROUP BY kind", (identifier,)))
            # A requirements continuation only attaches the original issue to
            # another goal; it is not an actual register/reuse attempt.
            duplicates = None if retired else store.conn.execute("""
                SELECT count(*) FROM closure_events WHERE issue_id=? AND kind='duplicate_linked'
                AND coalesce(json_extract(CASE WHEN json_valid(data) THEN data ELSE '{}' END,'$.reason'),'')
                    != 'explicit_requirements_continuation'
                """, (identifier,)).fetchone()[0]
            # One digest may contain many event records. Count successful request
            # batches, never equate per-event flags with visible notifications.
            batches = None if retired else store.conn.execute("""
                SELECT count(DISTINCT d.seq) FROM closure_events d,
                json_each(CASE WHEN json_valid(d.data) THEN d.data ELSE '{}' END,'$.event_seqs') j
                JOIN closure_events e ON e.seq=j.value
                WHERE d.kind='notification_digest_requested' AND e.issue_id=?
                AND json_extract(CASE WHEN json_valid(d.data) THEN d.data ELSE '{}' END,'$.requested')=1
                """, (identifier,)).fetchone()[0]
            notice_records = None if retired else store.conn.execute("""
                SELECT count(*) FROM closure_notifications n JOIN closure_events e ON e.seq=n.event_seq
                WHERE e.issue_id=? AND n.requested=1""", (identifier,)).fetchone()[0]
            uncertain = None if retired else store.conn.execute("""
                SELECT count(*) FROM closure_notifications n JOIN closure_events e ON e.seq=n.event_seq
                WHERE e.issue_id=? AND n.error='request_uncertain'""", (identifier,)).fetchone()[0]
            ungrouped = None if retired else store.conn.execute("""
                SELECT count(*) FROM closure_notifications n JOIN closure_events e ON e.seq=n.event_seq
                WHERE e.issue_id=? AND n.requested=1 AND NOT EXISTS (
                    SELECT 1 FROM closure_events d,
                    json_each(CASE WHEN json_valid(d.data) THEN d.data ELSE '{}' END,'$.event_seqs') j
                    WHERE d.kind='notification_digest_requested' AND j.value=n.event_seq
                    AND json_extract(CASE WHEN json_valid(d.data) THEN d.data ELSE '{}' END,'$.requested')=1)
                """, (identifier,)).fetchone()[0]
            store.conn.execute("ROLLBACK")
            latest = store.get(identifier)
            after = (latest["revision"], latest["updated"], store.conn.execute(
                "SELECT max(seq) FROM closure_events WHERE issue_id=?", (identifier,)).fetchone()[0])
            if marker != after:
                raise ValueError("trace_issue_changed")
        if not scope.unchanged():
            raise ValueError("display_scope_unavailable")

        acceptance = json.loads(row["acceptance"] or "{}")
        unknown = item.get("current_verification") == "unknown"
        if not retired and not unknown and item["state"] == "resolved" and acceptance.get("evidence"):
            try:
                if not closure.current(acceptance["evidence"]):
                    item.update(state="verification", reason="末次核对发现验收依据已变化，需要重新核验。",
                                summary="验收依据已变化，结果待核验 · 负责人："+row["owner"])
            except EvidenceReadUnavailable:
                unknown = True
                item.update(current_verification="unknown", codex_handoff_available=False, codex_reply_available=False,
                            summary="历史验收保留，当前依据未知 · 负责人："+row["owner"],
                            reason="末次核对暂不可读取验收依据，不能确认已解决。")
        if not scope.unchanged():
            raise ValueError("display_scope_unavailable")
        accepted = not retired and not unknown and item["state"] == "resolved" and bool(acceptance.get("evidence"))
        state = ("inactive" if retired else "unknown" if unknown else
                 "canceled" if item["state"] in {"canceled", "dismissed"} else
                 "waiting_decision" if item["state"] == "decision" else
                 "verified" if accepted else
                 "running" if item.get("runtime", {}).get("activity") == "active" else
                 "verification" if item["state"] in {"resolved", "verification"} else
                 "received" if row["dispatch_state"] in {"acknowledged", "received"} else
                 "unknown" if row["dispatch_state"] in closure.UNCERTAIN_DISPATCH_STATES else
                 "blocked" if item["state"] == "blocked" else "pending")
        labels = {"inactive":"本人已停用", "unknown":"当前结果未知", "canceled":"已取消或关闭",
                  "waiting_decision":"等待本人决定", "verified":"当前证据已验收", "running":"正在执行",
                  "verification":"结果待核验", "received":"已接收，结果待核验", "blocked":"受阻", "pending":"已登记，待处理"}
        created, accepted_at = _timestamp(row["created"]), _timestamp(acceptance.get("at")) if accepted else None
        duration = accepted_at - created if created and accepted_at and accepted_at >= created else None
        trace = {"interface":"task-closure-trace", "contract":1, "raw_access":False,
                 "id":item["id"], "revision":item["revision"], "observed_at":observed_at,
                 "state":state, "label":labels[state], "owner":row["owner"],
                 "codex_thread_id":item.get("codex_thread_id"), "runtime":item.get("runtime"),
                 "registered_at":created, "accepted_at":accepted_at,
                 "timeline":[{"sequence":e["seq"], "label":"已续接原事项" if e[3] else EVENT_LABELS[e["kind"]], "at":_timestamp(e["at"])}
                             for e in reversed(events[:200]) if e["kind"] in EVENT_LABELS],
                 "timeline_truncated":len(events)>200,
                 "metrics":{"explicit_decision_count":None if retired else counts.get("explicit_choice",0),
                            "duplicate_registration_count":duplicates,
                            "notification_request_count":None if retired or ungrouped or uncertain else batches,
                            "notification_request_records":notice_records,
                            "registration_to_acceptance_seconds":duration,
                            "user_reminder_count":None, "notification_seen_count":None, "cost":None},
                 "evidence":{"current_acceptance":accepted, "current_verification":"unknown" if unknown else "checked",
                             "acceptance_file_count":len(acceptance.get("evidence",[])) if not retired else None},
                 "boundary":"只读原事项元数据；已接收不代表完成，通知请求不代表本人已看到。"}
        item["closure_trace"] = trace
        return {"item":item, "trace":trace}


def trace(database, identifier):
    return detail(database, identifier)["trace"]
