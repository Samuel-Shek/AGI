"""Read-only ancestry checks for a cached analysis, without changing its record."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from ..feedback.store import digest
from ..presentation_scope import for_runtime, read_view


def pipeline_status(store, active, last, pending):
    """A lack of admitted input is distinct from a completed negative finding."""
    count = store.conn.execute('SELECT count(*) FROM contexts').fetchone()[0]
    if not store.enabled():
        state, label, explanation = 'disabled', '已关闭', '停止新的读取与分析，历史结果保留。'
    elif not active:
        state, label, explanation = 'paused', '已暂停', '暂停期间不读取新对话或提交分析。'
    elif store.get('last_error', ''):
        state, label, explanation = 'analysis_failed', '最近分析未完成', '原进度与调用记录保留，系统继续核对失败原因。'
    elif store.conn.execute("SELECT 1 FROM calls WHERE state IN ('unknown','validation_failed') LIMIT 1").fetchone():
        state, label, explanation = 'verification_pending', '部分结果待核对', '结果不明的旧请求不会重复提交；新的有效对话仍按原进度处理。'
    elif any(row['status']=='unavailable' for row in store.rows('SELECT status FROM sources')):
        state, label, explanation = 'source_unavailable', '部分来源读取失败', '来源暂不可用，现有材料不能证明近期对话已读完整。'
    elif any(row['status'] in {'backfill', 'pending'} for row in store.rows('SELECT status FROM sources')):
        state, label, explanation = 'reading', '对话仍在读取', '资料尚未读完整，分批处理结果不代表完整覆盖。'
    elif not count:
        state, label, explanation = 'no_material', '尚无可分析材料', '目前没有获准且可核对的近期个人对话；不能据此判断没有思路停滞。'
    elif pending:
        state, label, explanation = 'waiting_analysis', '已有材料，等待分析', '现有后台按原调度和用量限制处理，不新增并发调用。'
    elif last.get('status') != 'unavailable' and last.get('judgment') == 'not_found':
        state, label, explanation = 'not_found', '本次分析尚未发现', '已处理的材料中暂未发现足够依据，不代表所有近期讨论都已分析。'
    elif store.conn.execute("SELECT 1 FROM insights WHERE state='active' LIMIT 1").fetchone():
        state, label, explanation = 'insight_available', '有待检验的提醒', '查看对话依据、其他解释和反证条件后再判断。'
    else:
        state, label, explanation = 'waiting_analysis', '已有材料，结果待核对', '材料已纳入，尚无可核对的完整分析结果。'
    return {'state': state, 'label': label, 'explanation': explanation, 'admitted_contexts': count, 'pending_contexts': pending}


def last_analysis(store, value):
    scope = getattr(store.conn, "scope", None) or for_runtime(store.root.parent)
    from ..conversation_scope import personal_scope
    if not scope.enabled and not scope.invalid and not personal_scope.company_stopped():
        return value
    if not value:
        return {}

    def unavailable(reason="analysis_source_unverifiable"):
        result = {"status": "unavailable", "reason": reason,
                  "explanation": "上次分析的来源已退出或暂不可核对，摘要暂不展示。"}
        if isinstance(value, dict) and type(value.get("at")) in (int, float):
            result["at"] = value["at"]
        return result

    if scope.invalid or not scope.unchanged():
        return unavailable()
    if scope.hidden(value):
        return unavailable("source_not_in_display_scope")
    try:
        request = value.get("request")
        if not isinstance(request, str) or not re.fullmatch("[0-9a-f]{24}", request):
            raise ValueError("analysis_request_unverifiable")
        policy = scope.policy_view()
        feedback_root = Path(store.get("feedback_root", store.root.parent / "feedback"))
        # A run's location is not itself proof of personal provenance. Admission
        # precedes opening the exact private saved input; no transcript is read.
        from ..conversation_scope import allowed, authorization, binding, POLICY
        grant_binding=binding()
        try:
            conversation_grant = authorization()['enabled']
        except (OSError, ValueError, TypeError, KeyError):
            conversation_grant = False
        if not conversation_grant and policy.classify({"path": str(feedback_root / "runs" / request)}) != "personal":
            raise ValueError("analysis_run_source_unverifiable")
        conn = getattr(store.conn, "raw", store.conn)
        database_version = conn.execute("PRAGMA data_version").fetchone()[0]
        changes = conn.total_changes
        view = read_view(conn, scope, ("calls", "contexts", "retired_contexts", "turns",
                         "retired_turns", "insights"), personal_kind="reflection")
        call = view.execute("SELECT state,detail FROM calls WHERE id=?", (request,)).fetchone()
        if call is None or call[0] != "settled":
            raise ValueError("analysis_call_unverifiable")
        detail = json.loads(call[1])
        from .analysis import saved_input,turn_range_fingerprint
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def run_admitted():
            if (binding()!=grant_binding or not scope.unchanged() or not policy.unchanged()
                    or tuple(view.execute('SELECT state,detail FROM calls WHERE id=?',(request,)).fetchone() or ())!=tuple(call)):
                return False
            ranges=detail.get('input_ranges');manifest=detail.get('input_sources',{})
            if (manifest.get('version')!=1 or manifest.get('policy')!=POLICY
                    or not isinstance(manifest.get('authorization_binding'),list)
                    or not all(isinstance(manifest.get(k),list) for k in ('contexts','turns','insights'))
                    or not manifest['contexts'] or not manifest['turns']):return False
            contexts=set(manifest['contexts'])
            if any(not isinstance(ident,str) or not any(view.execute('SELECT 1 FROM '+table+' WHERE id=?',(ident,)).fetchone()
                    for table in ('contexts','retired_contexts') if table in tables) for ident in contexts):return False
            for item in manifest['turns']:
                matches=[dict(row) for table in ('turns','retired_turns') if table in tables
                    for row in view.execute('SELECT * FROM '+table+' WHERE id=?',(item['id'],))]
                if (len(matches)!=1 or matches[0]['context_id'] not in contexts or item.get('context_id')!=matches[0]['context_id']
                        or turn_range_fingerprint(matches[0])!=item.get('fingerprint')):return False
            for item in manifest['insights']:
                row=view.execute('SELECT * FROM insights WHERE id=?',(item['id'],)).fetchone()
                if row is None or digest(dict(row))!=item.get('fingerprint'):return False
            return (isinstance(ranges,dict) and bool(ranges) and set(ranges)<=contexts and all(
                isinstance(ident,str) and isinstance(values,list) and bool(values)
                and all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) for v in values)
                and view.execute('SELECT 1 FROM contexts WHERE id=?',(ident,)).fetchone() is not None
                for ident,values in ranges.items()))
        read_options={'read_check':run_admitted} if conversation_grant else {}
        if conversation_grant and not run_admitted():raise ValueError('analysis_call_range_unverifiable')
        raw, schema = saved_input(feedback_root, request,**read_options)
        if digest([raw, schema]) != detail.get("input_fingerprint"):
            raise ValueError("analysis_input_changed")
        data = json.loads(raw[raw.index("\n{") + 1:])
        if scope.hidden(data):
            return unavailable("source_not_in_display_scope")
        if not isinstance(data, dict) or not data.get("current_contexts") or not all(isinstance(data.get(k), list) for k in
                ("current_contexts", "historical_contexts", "previous_insights")):
            raise ValueError("analysis_input_unverifiable")

        def context_visible(ident):
            if not isinstance(ident, str) or not ident or not any(
                    True
                    for table in ("contexts", "retired_contexts") if table in tables
                    for row in view.execute("SELECT session FROM " + table + " WHERE id=?", (ident,))):
                raise ValueError("analysis_context_unverifiable")

        def turn_visible(item, context=None, *, full=False):
            ident = item.get("turn_id" if full or "turn_id" in item else "id")
            if not isinstance(ident, str) or not ident:
                raise ValueError("analysis_turn_unverifiable")
            matches = [dict(row) for table in ("turns", "retired_turns") if table in tables
                       for row in view.execute("SELECT * FROM " + table + " WHERE id=?", (ident,))]
            if len(matches) != 1:
                raise ValueError("analysis_turn_unverifiable")
            row = matches[0]
            context_visible(row["context_id"])
            reference = json.loads(row["reference"])
            path = reference.get("path")
            if (not isinstance(path, str) or not Path(path).is_absolute() or "\x00" in path
                    or not allowed(row["session"], path)):
                raise ValueError("analysis_turn_source_unverifiable")
            text = item.get("text" if full or "text" in item else "quote")
            if (context is not None and row["context_id"] != context
                    or not isinstance(item.get("role"), str) or item["role"] != row["role"]
                    or not isinstance(text, str) or not text
                    or (text != row["text"] if full else text not in row["text"])):
                raise ValueError("analysis_turn_changed")

        for context in data["current_contexts"]:
            ident = context["context_id"]
            context_visible(ident)
            if not context["turns"] and not context.get("previous_summary", {}).get("evidence"):
                raise ValueError("analysis_current_context_unverifiable")
            for turn in context["turns"]:
                turn_visible(turn, ident, full=True)
            previous = context.get("previous_summary", {})
            if previous:
                if not previous.get("evidence"):
                    raise ValueError("analysis_previous_summary_unverifiable")
                for evidence in previous["evidence"]:
                    turn_visible(evidence, ident)
        for context in data["historical_contexts"]:
            ident = context["context_id"]
            context_visible(ident)
            if not context.get("evidence"):
                raise ValueError("analysis_history_unverifiable")
            for evidence in context["evidence"]:
                turn_visible(evidence, ident)
        for insight in data["previous_insights"]:
            row = view.execute("SELECT * FROM insights WHERE id=?", (insight["id"],)).fetchone()
            if row is None or any(insight[k] != row[k] for k in ("question", "pattern", "refutation") if k in insight):
                raise ValueError("analysis_previous_insight_unverifiable")
            original_evidence = json.loads(row["evidence"])
            if not original_evidence:
                raise ValueError("analysis_previous_insight_source_unverifiable")
            for evidence in original_evidence:
                turn_visible(evidence)
            for evidence in insight.get("evidence", []):
                turn_visible(evidence)
        latest_raw, latest_schema = saved_input(feedback_root, request,**read_options)
        latest_call = conn.execute("SELECT state,detail FROM calls WHERE id=?", (request,)).fetchone()
        if (digest([latest_raw, latest_schema]) != detail.get("input_fingerprint")
                or latest_call is None or tuple(latest_call) != tuple(call)
                or database_version != conn.execute("PRAGMA data_version").fetchone()[0]
                or changes != conn.total_changes
                or not scope.unchanged() or not policy.unchanged()):
            raise ValueError("analysis_source_changed")
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
        return unavailable()
