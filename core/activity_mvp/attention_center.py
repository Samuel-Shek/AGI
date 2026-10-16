"""Read-only, content-minimising projections for AGI's decision workspace.

Evidence files, answerable questions and memory decisions have separate units.
This module never marks an unavailable source repaired or answers a question.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import memory_review as memory
from .activity_confirmations import (
    activity_confirmation_card_snapshot, resolve_confirmation_range,
    _group_confirmation_rows, _live_confirmation_rows,
    _visible_confirmation_card_groups, _confirmation_group_section,
)
from .daemon import get_quick_status

CONTRACT = 1
REASONS = {
    "MEM-AUDIT-TEMPORAL-001": ("时效待核对", "自动审计匹配到阶段性信息；尚未对照较新的业务记录或原始依据，不能据此判断内容已过期。"),
    "MEM-AUDIT-STALE-001": ("旧信息待复核", "审计发现旧信息线索；时间较早本身不能证明内容已经失效。"),
    "MEM-AUDIT-RAW-001": ("内容需整理", "这条记忆可能夹有原始材料，需要先确认哪些内容适合长期保留。"),
    "MEM-AUDIT-SUBJECT-001": ("归属字段待补核", "旧记忆缺少职业信息的归属或来源字段；字段缺失不等于内容不属于你，应先核查已授权正文和权威来源。"),
    "MEM-AUDIT-DUP-001": ("相似记忆", "审计发现相似记录，需要检查内容与适用范围；相似不代表可以删除。"),
}
ACTION_IMPACTS = {
    "keep_active": "允许 AI 再次使用当前版本；提交时会重新核对正文、归属与审核条件。",
    "mark_legacy": "保留为历史，AI 不再把它当作当前信息自动调用。",
    "mark_expired": "保留原记录并标为过期，AI 不再自动调用。",
    "review_later": "保持待复核，AI 暂不自动调用，之后仍可继续处理。",
    "quarantine_wrong_subject": "隔离主体不符的记忆，保留原记录并停止自动调用。",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reason_description(codes):
    return "；".join(dict.fromkeys(REASONS.get(c, ("需要复核", "审计发现需要进一步核对的内容。"))[1] for c in codes))


def memory_title(item):
    return " · ".join(dict.fromkeys(REASONS.get(c, ("需要复核", ""))[0] for c in item.get("reason_codes", []))) or "待复核的记忆"


def _issue(key, kind, title, summary, **fields):
    return {"id": key, "kind": kind, "title": title, "summary": summary,
            "requires_decision": False, "member_count": 0, "members": [],
            "background": summary, "reason": "", "impact": "", "inaction": "",
            "recommendation": "", "attempts": "尚未记录针对本事项的来源核查。",
            "completion_rule": "重新读取真实状态后核对结果。", **fields}


def _memory_issues(candidates):
    groups = defaultdict(list)
    for row in candidates["items"]:
        if row["review_state"] != "pending" and row.get("decision") != "review_later":
            continue
        # Group for reading only. Unknown scope NEVER grants batch permission.
        codes = sorted(row["reason_codes"])
        safety = any("SUBJECT" in c or "CONFLICT" in c for c in codes)
        key = digest([codes, row["memory_id"] if safety else "reading-group"])[:24]
        groups[key].append(row)
    issues = []
    for key, rows in groups.items():
        members = [{"id": r["memory_id"], "title": memory_title(r),
                    "reason": reason_description(r["reason_codes"]),
                    "content_sha256": r["content_sha256"],
                    "candidate_fingerprint": r["candidate_fingerprint"],
                    "current_decision": r.get("decision", ""), "decided_at": r.get("decided_at", ""),
                    "related_count": len(r.get("related_memory_ids", [])),
                    "last_seen_at": r.get("last_seen_at", "")} for r in rows]
        issues.append(_issue("memory-" + key, "memory", memory_title(rows[0]),
            f"{len(rows)} 条记忆待复核，当前保持原有调用状态。",
            requires_decision=True, member_count=len(rows), members=members,
            revision=digest([candidates["source_sha256"], members]),
            reason=reason_description(rows[0]["reason_codes"]),
            attempts="仅核对审核队列与审核台账。队列生成于 " + str(candidates["generated_at"]) + "；尚未核查记忆正文、业务记忆库及原始邮件。",
            impact="你的决定会改变这些记忆能否被 AI 调用；原始记忆保留。",
            inaction="继续保留在待复核列表，当前未放行的内容不会自动进入 AI 上下文。",
            recommendation="先由 Codex 核查已授权范围内的业务记忆与原始依据；已有明确答案时不重复提问。正文授权与审核选择分开处理。",
            completion_rule="明确选择的成员与影响通过预览核对，审核台账写入成功，并重新读取调用状态。",
            actions=[{"id": a, "title": memory.ACTION_TITLES[a], "impact": ACTION_IMPACTS[a]} for a in memory.ACTIONS]))
    return sorted(issues, key=lambda x: (x["title"], x["id"]))


def _cards(conn, *, historical=False, _evidence_gaps=None):
    start, end = resolve_confirmation_range("7d")
    if historical:
        end, start = start, "1970-01-01T00:00:00+00:00"
    def observe(keys):
        try:
            _evidence_gaps.append(_evidence_summary_for_keys(conn, keys))
        except (sqlite3.Error, KeyError, ValueError, RuntimeError):
            _evidence_gaps.append(None)
    result = activity_confirmation_card_snapshot(conn, range_name="7d", start=start, end=end,
        section="pending_confirmation",
        _eligible_group_observer=observe if _evidence_gaps is not None else None)
    return result, result["cards"]


def _activity_issue(card):
    return _issue("activity-" + card["group_key"], "activity", card["display_title"],
        card["display_question"], requires_decision=True, member_count=1,
        revision=card["member_revision"], group_key=card["group_key"], card=card,
        background=card["display_evidence"], reason="系统发现一个具体事项，但现有证据还不能独立确认结果。",
        impact="回答只修改这个事项的活动记录；不会自动发送消息或写入长期记忆。",
        inaction="保持待答复，不会因为时间过去而自动同意或否定。",
        recommendation="仅确认你确知的结果；记不清时可以暂缓，或交给 Codex 查证。",
        completion_rule="当前事项版本的回答成功写入，重新读取后状态与选择一致。")


def current_evidence_gaps(conn):
    """Join evidence to the SAME eligible semantic groups as the question UI."""
    return _evidence_gaps_for_keys(conn, _current_evidence_keys(conn))


def _current_evidence_keys(conn):
    start, end = resolve_confirmation_range("7d")
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM activity_confirmation_items WHERE state IN ('pending','deferred') "
        "AND answer IS NULL AND datetime(observed_start)<datetime(?) AND datetime(observed_end)>datetime(?)",
        (end, start))]
    grouped = _visible_confirmation_card_groups(_group_confirmation_rows(_live_confirmation_rows(conn, rows)))
    keys = {str(r["item_key"]):key for key,members in grouped.items()
            if _confirmation_group_section(members)=="pending_confirmation" for r in members}
    return keys


def _evidence_gaps_for_keys(conn, keys):
    """Count evidence using the exact eligible item-to-semantic-group mapping."""
    affected, evidence = set(), set()
    ordered = sorted(keys)
    for offset in range(0,len(ordered),400):
        chunk = ordered[offset:offset+400]
        for row in conn.execute(
            "SELECT r.item_key,r.evidence_id FROM activity_confirmation_evidence_refs r "
            "JOIN activity_confirmation_evidence_files f ON f.evidence_id=r.evidence_id "
            "WHERE f.state='unavailable' AND r.released_at IS NULL AND r.item_key IN (" + ",".join("?" for _ in chunk) + ")",chunk):
            affected.add(keys[row[0]]); evidence.add(row[1])
    return len(affected), len(evidence)


def _evidence_summary_for_keys(conn, keys):
    # Read all parts of this summary from one SQLite snapshot. This is nested
    # safely when the caller already owns a transaction and writes nothing.
    savepoint = f"attention_evidence_summary_{id(keys):x}"
    conn.execute("SAVEPOINT " + savepoint)
    try:
        reasons = Counter()
        for row in conn.execute("SELECT last_error,COUNT(*) AS n FROM activity_confirmation_evidence_files WHERE state='unavailable' GROUP BY last_error"):
            reasons[str(row[0])] += int(row[1])
        affected, missing = _evidence_gaps_for_keys(conn, keys)
        # Only the unavailable total is displayed here. Other evidence health
        # surfaces retain their full actionable/historical bucket contract.
        return sum(reasons.values()), reasons, affected, missing
    finally:
        conn.execute("RELEASE SAVEPOINT " + savepoint)


HERMES_COMPAT_STATES = {
    "needs_review": ("Hermes 兼容检查：需要你看一下", "有变化不在可自动认账的范围内，回执保持原样。"),
    "fix_failed": ("Hermes 兼容检查：自动修复失败", "重新验证后检查仍未通过，回执保持原样。"),
    "check_error": ("Hermes 兼容检查：检查本身出错", "巡检程序运行出错，当前兼容状态未知。"),
}
HERMES_COMPAT_STALE = timedelta(hours=3)
HERMES_COMPAT_RECENT = timedelta(hours=24)


def hermes_compat_dir():
    return Path.home()/".hermes"/"state"/"hermes-maintenance"/"compat-watch"


def _hermes_compat_issues(root=None, now=None):
    """Project the hourly Hermes receipt watcher (an external, read-only source).

    A missing status file means the watcher is not installed and adds nothing.
    """
    root = Path(root) if root is not None else hermes_compat_dir()
    now = now or datetime.now().astimezone()
    systems, resources = [], []
    path = root/"status.json"
    if not path.exists():
        return systems, resources
    impact = "检查不通过期间，Hermes 机器人照常运行，但无法走正规流程重启或上线新版本。"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        state, checked = status["state"], datetime.fromisoformat(status["checked_at"])
        if status.get("schema") != 1 or checked.tzinfo is None or state not in {"ok", "auto_fixed", *HERMES_COMPAT_STATES}:
            raise ValueError("unknown hermes compat status")
    except (OSError, ValueError, KeyError, TypeError):
        systems.append(_issue("hermes-compat-unreadable", "system", "Hermes 兼容检查状态读不出来",
            "巡检结果文件无法读取或格式不认识，当前兼容状态未知。",
            impact="无法据此判断 Hermes 能否正常重启或升级。",
            recommendation="让 AI 按「Hermes 兼容检查」排查巡检程序。",
            completion_rule="巡检结果文件能正常读取后，这一条自动消失。"))
        return systems, resources
    if now - checked > HERMES_COMPAT_STALE:
        systems.append(_issue("hermes-compat-stale", "system", "Hermes 兼容巡检好几个小时没更新",
            f"最后一次检查在 {checked:%m-%d %H:%M}，正常应每小时一次。",
            reason="巡检后台任务可能停了或一直出错。",
            impact="巡检停了，兼容回执再过期也不会有人提醒。",
            recommendation="让 AI 检查后台任务 ai.hermes.compat-watch 是否还在运行。",
            completion_rule="巡检恢复每小时更新后，这一条自动消失。"))
    findings = [f for f in status.get("findings") or [] if isinstance(f, dict)]
    review = [str(f.get("detail", "")) for f in findings if not f.get("auto")]
    note = str(status.get("note") or "")
    if state in HERMES_COMPAT_STATES:
        title, fallback = HERMES_COMPAT_STATES[state]
        since = status.get("problem_since") or status["checked_at"]
        systems.append(_issue("hermes-compat-"+state, "system", title, note or fallback,
            member_count=len(review), revision=str(since),
            reason=("没有自动认账的变化：" + "；".join(review[:3]) + (f" 等 {len(review)} 项" if len(review) > 3 else "")) if review else "",
            impact=impact,
            inaction="Hermes 照常运行；重启或升级会一直被拦下，巡检每天再提醒一次。",
            recommendation="让 AI 按「Hermes 兼容检查」排查，或告诉 AI 这些变化是你批准的。",
            attempts=note or "巡检程序已逐项对照，没有替你认账。",
            completion_rule="巡检下一次（每小时一次）显示正常后，这一条自动消失。"))
    fixes = []
    try:
        with (root/"history.jsonl").open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell()-65536))
            tail = log.read().decode("utf-8", "ignore").splitlines()[1:]
        for line in tail:
            try:
                entry = json.loads(line)
                at = datetime.fromisoformat(entry["checked_at"])
                recent = now - at <= HERMES_COMPAT_RECENT
            except (ValueError, KeyError, TypeError):
                continue
            if entry.get("state") == "auto_fixed" and recent:
                fixes.append((at, str(entry.get("note") or "")))
    except OSError:
        pass
    if state == "auto_fixed" and not any(at == checked for at, _ in fixes):
        fixes.append((checked, note))
    if fixes:
        at, latest = max(fixes)
        resources.append(_issue("hermes-compat-auto-fixed", "reference", "Hermes 兼容检查：最近自动修好过",
            f"24 小时内自动修好 {len(fixes)} 次，最近一次在 {at:%m-%d %H:%M}。",
            member_count=len(fixes), revision=at.isoformat(), reason=latest,
            impact="只认账了模型切换、代码上线、规则更新这三类正常变化，Hermes 已恢复可正常重启。",
            inaction="不用做任何事，24 小时后这一条自动收起。",
            recommendation="不用做任何事。", attempts=latest or "巡检程序自动重新验证并更新了回执。",
            completion_rule="24 小时内没有新的自动修复后自动收起。"))
    return systems, resources


def snapshot(conn, *, queue_path=None, ledger_path=None):
    from .presentation_scope import display_scope, read_view, ACTIVITY_TABLES
    from .retirement_snapshot import source_snapshot
    from .task_closure import personal_scope
    from .user_decisions import decision_text_read_cache
    database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    with decision_text_read_cache(), source_snapshot(personal_scope, bounded_evidence=True), display_scope(Path(database).parent) as scope:
        return scope.project(_snapshot(read_view(conn, scope, ACTIVITY_TABLES), queue_path=queue_path, ledger_path=ledger_path))


def _snapshot(conn, *, queue_path=None, ledger_path=None):
    decisions, resources, systems = [], [], []
    autonomy = {"enabled": False, "items": []}
    try:
        from .feedback.store import Store as FeedbackStore, default_root as feedback_root
        if (feedback_root()/"feedback.sqlite").exists():
            from .feedback.autonomy import snapshot as autonomous_snapshot
            with FeedbackStore(readonly=True) as feedback:
                autonomy = autonomous_snapshot(feedback)
    except (OSError, sqlite3.Error, ValueError):
        pass
    activity_ok = memory_ok = evidence_ok = runtime_ok = True
    memory_total = memory_pending = recent_count = insufficient = None
    current_gap_read = []
    try:
        result, cards = _cards(conn, _evidence_gaps=current_gap_read)
        recent_count = result["pending_question_count"]
        insufficient = result["information_insufficient_count"]
        decisions.extend(_activity_issue(c) for c in cards)
        old_result, old_cards = _cards(conn, historical=True)
        if old_cards:
            # Historical questions remain answerable and unresolved; their age is
            # not a fabricated answer and they are never silently deleted.
            resources.append(_issue("historical-questions", "historical_questions", "历史待答复事项",
                f"{len(old_cards)} 个历史问题仍保留待答复，可按需继续。",
                member_count=len(old_cards), children=[_activity_issue(c) for c in old_cards],
                impact="保留待答复状态；不要求为了清空列表逐条回忆。"))
        if insufficient:
            resources.append(_issue("insufficient-clues", "reference", "信息不足的线索",
                f"最近 7 天有 {insufficient} 组线索未形成可回答的问题。",
                member_count=insufficient, reason="没有足够的具体对象或独立结果证据。",
                impact="不会作为已确认事实使用，也不要求你猜测。",
                recommendation="有新证据时再核对；目前无需逐条处理。"))
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        activity_ok = False
        systems.append(_issue("activity-read-unavailable", "system", "活动事项暂不可读取",
            "当前无法核对活动问题数量，请重新读取；这里不会显示为零。",
            reason="数据库繁忙、来源关系变化或读取未完成。", impact="活动事项数量暂时未知。"))
    try:
        database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
        candidates = memory.memory_review_candidates(limit=None, queue_path=queue_path, ledger_path=ledger_path,
            display_root=Path(database).parent if database else None)
        memory_total = candidates["total"]
        memory_pending = sum(r["review_state"] == "pending" or r.get("decision") == "review_later" for r in candidates["items"])
        from .attention_investigation import project
        databases = conn.execute("PRAGMA database_list").fetchall()
        database = next((r[2] for r in databases if r[1] == "main" and r[2]), None)
        for issue in _memory_issues(candidates):
            projected = project(database, issue) if database else issue
            if autonomy.get("enabled"):
                work = [w for w in autonomy.get("items", []) if w["kind"] == "memory"
                        and w["reference"] in {m["id"] for m in issue["members"]}]
                if not any(w["state"] == "human_required" for w in work):
                    projected.update(requires_decision=False,
                        summary=f"{issue['member_count']} 条记忆由系统核查；缺少依据时保留未知。",
                        recommendation="在自主进化查看查证依据与实际处理结果。",
                        inaction="系统根据新证据继续核对，不要求你回忆历史，也不自动放行缺证记忆。")
                    resources.append(projected)
                    continue
            decisions.append(projected)
    except (memory.MemoryReviewError, sqlite3.Error, OSError, ValueError):
        memory_ok = False
        systems.append(_issue("memory-review-unavailable", "system", "记忆审核资料暂不可读取",
            "无法核对当前审核清单，数量暂时未知。",
            reason="候选清单缺失、超过有效期或读取失败。",
            impact="共享长期记忆按现有规则停止自动调用；需要恢复有效审核资料。",
            recommendation="先交给 Codex 检查审核来源与最新生成记录。"))
    try:
        if activity_ok and current_gap_read:
            if len(current_gap_read) != 1 or current_gap_read[0] is None:
                raise RuntimeError("current evidence mapping unavailable")
            count, reasons, affected, current_missing = current_gap_read[0]
        else:
            # Compatibility for callers that supply cards independently, and
            # the existing partial-activity fallback, retain full fresh reads.
            keys = _current_evidence_keys(conn) if activity_ok else {}
            count, reasons, affected, current_missing = _evidence_summary_for_keys(conn, keys)
        if count:
            text = "；".join(f"{ {'privacy_not_clear':'隐私检查未通过','source_unavailable':'来源不可用'}.get(k,'其他来源缺口')} {v} 条" for k,v in sorted(reasons.items()))
            resources.append(_issue("evidence-gaps", "reference", "历史证据资料" if not current_missing else "不可用的证据资料",
                f"{count} 条证据不可用；这表示资料缺口，不是待办数量。",
                member_count=count, reason=text,
                current_evidence_count=current_missing, historical_evidence_count=count-current_missing,
                impact="这些资料不能用于重读或证明历史结果。原记录和原因保留。",
                inaction="保持不可用，不影响已经独立核实的其他记录。",
                recommendation="不需要逐条确认；如某个当前事项受影响，进入那个事项核查。"))
        # Only evidence attached to current answerable questions is operational.
        if affected:
            systems.append(_issue("current-evidence-gaps", "system", "部分当前事项缺少可重读的证据",
                f"影响 {affected} 个当前事项，具体影响与选项见对应详情。",
                member_count=affected, impact="不能依据缺失资料补写结果；可核对其他独立来源。"))
    except (sqlite3.Error, KeyError, ValueError, RuntimeError):
        evidence_ok = False
        systems.append(_issue("evidence-read-unavailable", "system", "资料状态暂不可读取",
            "历史资料数量暂时未知，重新读取后再判断。"))
    try:
        runtime = get_quick_status(conn)
        runtime_ok = runtime.get("status") != "unavailable"
        codes = runtime.get("attention_codes", [])
        for code in dict.fromkeys(codes):
            title = {
                "service_stopped":"自动记录服务已停止",
                "service_status_unavailable":"记录服务状态暂不可读取",
                "session_state_unknown":"当前会话状态尚未确认",
                "private_purge_pending":"隐私清理尚未完成",
                "retention_cleanup_failed":"保留期限清理需要核查",
                "capture_stale":"记录进度需要核查",
                "last_error":"最近记录出现异常",
            }.get(code,"记录后台进度需要核查")
            systems.append(_issue("runtime-"+code,"system",title,
                runtime.get("status_explanation") or "最新运行状态显示这项问题仍需要核查。",
                source_code=code, reason="已读取后台运行记录与服务状态。",
                impact="可能影响持续记录或既定的隐私清理；缺少的内容不会被补写为已记录。",
                inaction="保持当前权限与暂停设置，保留问题供继续核查。",
                recommendation="先交给 Codex 检查原因；需要改变权限时再明确决定。",
                completion_rule="重新读取服务与最新进度，确认原问题消失，并核对实际记录或清理结果。"))
    except (sqlite3.Error, RuntimeError, OSError, ValueError):
        runtime_ok = False
        systems.append(_issue("runtime-read-unavailable","system","运行状态暂不可读取",
            "无法核对当前记录是否正常，需要重新读取。"))
    # Feedback has independent storage and permissions; this projection exposes
    # a count and navigation only, never quotes or private investigation text.
    try:
        from .feedback.store import Store as FeedbackStore,default_root as feedback_root
        if (feedback_root()/"feedback.sqlite").exists():
            with FeedbackStore(readonly=True) as feedback:
                count=feedback.conn.execute("SELECT count(*) FROM incidents WHERE state IN ('decision','human_required')" if autonomy.get("enabled") else "SELECT count(*) FROM incidents WHERE state IN ('decision','recurred')").fetchone()[0]
                if count:
                    decisions.append(_issue("feedback-decisions","feedback","批评与改进需要你决定",f"{count} 件事项等待你的决定。",
                        requires_decision=True,member_count=count,revision=str(count),
                        reason="已整理事实和影响，仍需你的具体选择。" if autonomy.get("enabled") else "自动处理遇到需核实的复发、范围边界或验证缺口。",
                        impact="打开自主进化查看具体方案和实际尝试。",inaction="保持待决策，不会自动代选。",
                        recommendation="在批评与改进中查看原话、证据与处理方案。"))
    except (OSError,sqlite3.Error,ValueError):
        systems.append(_issue("feedback-read-unavailable","system","批评与改进暂不可读取","该部分状态未知，不能据此判断没有待办。"))
    # Hermes is a separate system; its watcher state never changes AGI's own completeness.
    compat_systems, compat_resources = _hermes_compat_issues()
    systems.extend(compat_systems)
    resources.extend(compat_resources)
    # Task closure is a separate private sidecar, never a memory decision.
    closure_ok = True
    try:
        from .task_closure import projection
        database = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        closures = projection(database)
    except (OSError, sqlite3.Error, ValueError, StopIteration):
        closure_ok = False
        closures = [_issue("closure-unavailable", "system", "闭环事项暂不可读取", "状态未知，不能据此判断没有待办。")]
    notices = [item for item in closures if item.get("id") == "closure-evidence-read-unavailable" and item.get("kind") == "system"]
    systems.extend(notices)
    closures = [item for item in closures if item not in notices]
    decisions.extend(item for item in closures if item.get("requires_decision"))
    complete = activity_ok and memory_ok and evidence_ok and runtime_ok and closure_ok
    return {"interface":"attention-center", "contract":CONTRACT,
            "status":"ok" if complete else "partial", "raw_access":False,
            "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
            "revision":digest([decisions, resources, systems, closures]),
            "decision_count":len(decisions) if activity_ok and memory_ok and closure_ok else None,
            "system_count":len(systems), "resource_count":len(resources),
            "activity_question_count":recent_count, "information_insufficient_count":insufficient,
            "memory_total":memory_total, "memory_pending":memory_pending, "autonomy_enabled":autonomy.get("enabled",False),
            "decisions":decisions, "systems":systems, "resources":resources, "closures":closures}


def detail(conn, identifier, **paths):
    if identifier.startswith("closure:"):
        from .task_closure import projection
        database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), None)
        items = projection(database, identifier=identifier.removeprefix("closure:")) if database else []
        return {"interface":"attention-center", "contract":CONTRACT,
                "status":"ok" if items else "changed", **({"item":items[0]} if items else {})}
    state = snapshot(conn, **paths)
    for item in state["decisions"] + state["systems"] + state["resources"] + state["closures"]:
        if item["id"] == identifier:
            return {"interface":"attention-center", "contract":CONTRACT, "status":"ok", "item":item}
        for child in item.get("children", []):
            if child["id"] == identifier:
                return {"interface":"attention-center", "contract":CONTRACT, "status":"ok", "item":child}
    return {"interface":"attention-center", "contract":CONTRACT,
            "status":"changed" if state["status"] == "ok" else "unavailable",
            "message":"事项已变化，请重新读取；没有执行任何处理。"}


def open_closure_codex(conn, identifier, payload):
    """Recheck this item's identity, then request navigation without a turn."""
    from . import attention_native as native
    item = detail(conn, identifier).get("item")
    assistance = payload.get("assistance") is True
    field = "codex_assistance_thread_id" if assistance else "codex_thread_id"
    if (not item or item.get("kind") != "closure" or
            item["revision"] != payload.get("revision") or
            item.get(field) != payload.get("thread_id") or not item.get(field)):
        raise ValueError("事项或负责聊天已变化，请重新读取后再打开。")
    if item.get("codex_open_via_handoff") or assistance:
        from .attention_handoff import command
        database = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        try:
            return command(database, "handoff-open", identifier, payload)
        except (RuntimeError, OSError, KeyError) as exc:
            return {"status":"unavailable", "error":"负责聊天暂时无法打开。事项仍保留，请重新核对接手状态。",
                    "error_code":getattr(exc, "code", "codex_open_failed")}
    thread = native.thread_id(item["codex_thread_id"])
    metadata_unavailable = False
    try:
        # Do not resume, create or submit a turn if the original cannot be read.
        with native.AppServer(max_duration=10) as server:
            try:
                current = server.call("thread/read", {"threadId":thread, "includeTurns":False}, timeout=5)["thread"]
            except native.NativeError as error:
                metadata_unavailable = native.missing_thread(error, thread)
                raise
            if current.get("id") != thread:
                raise native.NativeError("codex_thread_mismatch")
        # Recheck after the read: an owner/revision change must invalidate click.
        latest = detail(conn, identifier).get("item")
        if not latest or latest["revision"] != item["revision"] or latest.get("codex_thread_id") != thread:
            raise ValueError("负责聊天已变化，请重新读取后再打开。")
        native.open_thread(thread, timeout=3)
    except (native.NativeError, OSError, KeyError) as exc:
        receipt = None
        if metadata_unavailable:
            from .attention_handoff import Queue
            database = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
            receipt = Queue(database).record_navigation_failure(identifier, item["revision"], thread)
        return {"status":"unavailable", "error":"原负责聊天暂时无法打开。事项仍保留。" +
                ("可以让 Codex 协助核对，原任务不会重新执行。" if receipt else "请重新核对原任务。"),
                "assistance_available":bool(receipt), "item":detail(conn,identifier).get("item") if receipt else None,
                "error_code":getattr(exc, "code", "codex_open_failed")}
    return {"status":"ok", "navigation_requested":True, "thread_id":thread}


def preview_memory_group(conn, payload, **paths):
    issue = detail(conn, payload["issue_id"], **paths).get("item")
    if not issue or issue["kind"] != "memory" or issue["revision"] != payload.get("revision"):
        raise memory.MemoryReviewError("事项已变化，请重新查看成员与影响。")
    choices = payload.get("decisions", [])
    allowed = {m["id"] for m in issue["members"]}
    if not choices or any(c.get("memory_id") not in allowed for c in choices):
        raise memory.MemoryReviewError("所选成员不属于当前事项。")
    # Actual selected records must agree on verified scope before one action can
    # be bulk-applied. Individual explicit choices remain supported.
    records = [memory.read_memory_text(c["memory_id"]) for c in choices]
    if len(choices) > 1:
        scopes = {str(r.get("applies_to") or "") for r in records}
        if len(scopes) != 1 or "" in scopes:
            raise memory.MemoryReviewError("成员的适用范围不一致或尚未明确，请分别选择处理方式。")
        if len({c["action"] for c in choices}) != 1:
            raise memory.MemoryReviewError("批量操作需要相同处理方式。")
    preview = memory.preview_review_batch(choices, **paths)
    preview["attention_revision"] = issue["revision"]
    preview["memory_preview_digest"] = preview["preview_digest"]
    preview["preview_digest"] = digest([preview["memory_preview_digest"],issue["revision"],
        [{k:r.get(k) for k in ("memory_id","content_sha256","applies_to","schema_version","status","review_status")} for r in records]])
    return preview


def verify_resolution(conn, source, **paths):
    """An explicit recheck uses authoritative state, never a Codex claim."""
    kind = source.get("kind")
    if kind == "closure":
        from .task_closure import Store, current, effects_pending
        database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), None)
        if not database:
            return {"state":"unavailable", "message":"原闭环事项暂不可读取，不能据此关闭。"}
        with Store(database, readonly=True) as store:
            latest = store.get(str(source.get("id", "")).removeprefix("closure:"))
        acceptance = json.loads(latest.get("acceptance") or "{}")
        if latest["state"] == "resolved" and acceptance.get("evidence") and current(acceptance["evidence"]):
            return {"state":"resolved", "message":"已核对原闭环事项的当前版本独立验收记录。"}
        if latest["state"] in {"canceled", "dismissed"} and not effects_pending(latest):
            return {"state":"closed", "message":"原事项已取消或关闭；不恢复工作，也不认定问题已修复。"}
        return {"state":"pending", "message":"原闭环事项仍在跟进或等待决定；聊天接收或结束不算解决。"}
    if kind == "memory":
        latest = memory.memory_review_candidates(limit=None, **paths)
        if any(issue["id"] == source.get("id") for issue in _memory_issues(latest)):
            return {"state":"pending", "message":"该组仍有待复核成员，问题尚未解决。"}
        by_id = {m["memory_id"]:m for m in latest["items"]}
        members = source.get("members", [])
        if not members:
            return {"state":"unavailable", "message":"缺少原始成员信息，无法核验。"}
        for member in members:
            current = by_id.get(member["id"])
            if not current or current["content_sha256"] != member["content_sha256"]:
                return {"state":"changed", "message":"原成员或正文版本已变化，需要重新核对。"}
            if current["review_state"] != "decided" or current["decision"] == "review_later":
                return {"state":"pending", "message":"审核台账仍有待复核成员，问题尚未解决。"}
            actual = memory.read_memory_text(member["id"])
            if actual["content_sha256"] != member["content_sha256"]:
                return {"state":"changed", "message":"正文在审核后发生变化，原结果需要重新核对。"}
        return {"state":"resolved", "message":"已核对审核台账：原成员均有当前版本的明确处理结果。"}
    if kind == "activity":
        rows = conn.execute("SELECT state,answer FROM activity_confirmation_items WHERE group_key=?", (source.get("group_key"),)).fetchall()
        if rows and all(r[0] not in {"pending","deferred"} and r[1] in {"occurred","not_occurred","ignored","misrecognized"} for r in rows):
            return {"state":"resolved", "message":"已核对原事项的明确回答与最新记录。"}
        return {"state":"pending", "message":"原事项仍未取得可核对的处理结果。"}
    state = snapshot(conn, **paths)
    if state["status"] != "ok":
        return {"state":"unavailable", "message":"当前资料不完整，无法证明问题已解决。"}
    if any(i["id"] == source.get("id") for i in state["systems"]+state["resources"]):
        return {"state":"pending", "message":"最新状态仍显示这个问题或资料缺口。"}
    if source.get("id") in {"activity-read-unavailable","memory-review-unavailable","evidence-read-unavailable","current-evidence-gaps","evidence-gaps"} or str(source.get("id", "")).startswith("runtime-"):
        return {"state":"resolved", "message":"重新读取全部状态成功，原问题已不再存在。"}
    return {"state":"unavailable", "message":"尚缺少与原完成条件对应的结果证据，请在 Codex 继续核查。"}


def read_payload():
    raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        raise ValueError("输入内容过长。")
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("输入格式无效。")
    return value


def main(argv=None, *, database=None):
    parser = argparse.ArgumentParser(description="AGI attention workspace")
    parser.add_argument("--db", default=database or "data/activity.sqlite")
    parser.add_argument("action", choices=("snapshot", "detail", "closure-open", "preview", "apply", "source-search", "investigation-record", "handoff-status", "handoff-submit", "handoff-retry", "handoff-open", "handoff-check", "draft"))
    parser.add_argument("--id", default="")
    args = parser.parse_args(argv)
    conn = None
    try:
        db_path = Path(args.db).expanduser().resolve()
        if args.action == "source-search":
            from .attention_investigation import search_sources
            result = search_sources(db_path, read_payload().get("queries"))
        elif args.action.startswith("handoff-") or args.action == "draft":
            from .attention_handoff import command
            result = command(db_path, args.action, args.id, read_payload() if args.action in {"handoff-submit", "handoff-retry", "draft"} else {})
        else:
            conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            if args.action == "snapshot":
                result = snapshot(conn)
            elif args.action == "detail":
                result = detail(conn, args.id)
            elif args.action == "closure-open":
                result = open_closure_codex(conn, args.id, read_payload())
            elif args.action == "investigation-record":
                from .attention_investigation import record
                issue = detail(conn, args.id).get("item")
                if not issue:
                    raise ValueError("事项已变化，请重新核查。")
                result = record(db_path, issue, read_payload())
            else:
                payload = read_payload()
                preview = preview_memory_group(conn, payload)
                if args.action == "preview":
                    result = preview
                else:
                    if preview["preview_digest"] != payload.get("confirm_digest"):
                        raise memory.MemoryReviewError("内容或成员已变化，请重新预览后确认。")
                    result = memory.apply_review_batch(payload["decisions"], confirm_digest=preview["memory_preview_digest"])
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError) as exc:
        if getattr(exc, 'code', None) in {'source_not_in_display_scope', 'display_scope_unavailable'}:
            from .attention_handoff import Queue
            result = Queue.withdrawn_status(exc.code)
        else:
            result = {"interface":"attention-center", "contract":CONTRACT, "status":"unavailable", "error":str(exc), "raw_access":False}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
