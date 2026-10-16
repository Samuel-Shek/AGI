from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


CONTEXT_ONLY = "context-only"
NEVER_WRITE = "never-write"
CORRECT_SIGNAL = "correct-signal"
APPROVED_WRITE = "approved-write"
REWRITTEN_WRITE = "rewritten-write"
RECORD_ACTIONS = {CONTEXT_ONLY, NEVER_WRITE}
FEEDBACK_ACTIONS = {CORRECT_SIGNAL}
WRITE_ACTIONS = {APPROVED_WRITE, REWRITTEN_WRITE}
ALL_ACTIONS = RECORD_ACTIONS | FEEDBACK_ACTIONS | WRITE_ACTIONS
GENERALIZE_SCOPES = {"exact", "similar"}
PROVENANCE_COMPLETE = "complete"
PROVENANCE_TRACEABLE = "traceable"
PROVENANCE_LEGACY_UNKNOWN = "legacy_unknown"
PROVENANCE_STATUSES = {PROVENANCE_COMPLETE, PROVENANCE_TRACEABLE, PROVENANCE_LEGACY_UNKNOWN}

PATH_REDACTION_MARKER = "[path_redacted]"
_PATH_TERMINATORS = r"""\s"'`<>{}\[\]()（）,，。；;！？!?"""
_CJK_CHAR = r"\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF"
_KNOWN_POSIX_ROOT = (
    r"Users|home|root|Applications|Library|System|Volumes|private|var|tmp|"
    r"opt|usr|etc|dev|bin|sbin|nix|run|proc|sys|mnt|media|srv"
)
ABSOLUTE_PATH_RES = (
    # A file URI is path-bearing only when the scheme is followed by at least
    # one slash.  This deliberately leaves relative values such as
    # ``file:report`` alone while covering file:/tmp, file:///tmp and
    # file://server/share.
    re.compile(
        rf"""(?<![A-Za-z0-9_])file:/+[^{_PATH_TERMINATORS}/][^{_PATH_TERMINATORS}]*""",
        re.I,
    ),
    # Windows drive paths and real UNC shares.  Forward-slash ``//host/path``
    # values are intentionally not treated as UNC: they are commonly
    # protocol-relative intranet or web links.  Explicit file URIs above still
    # cover forward-slash network filesystem references.
    re.compile(
        rf"""(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^{_PATH_TERMINATORS}]*""",
        re.I,
    ),
    re.compile(
        rf"""(?<![A-Za-z0-9_\\])\\\\[^\\/{_PATH_TERMINATORS}]+[\\/][^{_PATH_TERMINATORS}]+""",
        re.I,
    ),
    re.compile(
        rf"""(?<![A-Za-z0-9_.~-])~[/\\][^{_PATH_TERMINATORS}]*""",
        re.I,
    ),
    # OCR and natural-language summaries can glue a POSIX path directly to a
    # CJK character (for example ``文件在/Users/example/result.txt``).  The
    # Unicode-aware boundary in the general POSIX rule below intentionally
    # rejects that shape so ordinary slash-separated Chinese prose remains
    # visible.  Recover only high-confidence glued paths here:
    #
    # * an exact, well-known local filesystem root; or
    # * an arbitrary root followed by a directory and a filename extension.
    #
    # Requiring either signal avoids classifying labels such as
    # ``海外渠道/代理商招募`` as paths.  URLs and protocol-relative intranet
    # references are still excluded because the slash must be immediately
    # preceded by a CJK character and cannot be paired.
    re.compile(
        rf"""(?<=[{_CJK_CHAR}])/(?!/)(?:"""
        rf"""(?:{_KNOWN_POSIX_ROOT})(?=/|$|[{_PATH_TERMINATORS}])"""
        rf"""|[^{_PATH_TERMINATORS}/]+(?:/[^{_PATH_TERMINATORS}/]+){{2,}}"""
        rf"""\.[A-Za-z0-9]{{1,16}}(?=$|[{_PATH_TERMINATORS}])"""
        rf""")[^{_PATH_TERMINATORS}]*""",
        re.I,
    ),
    # Any POSIX absolute path is sensitive, regardless of its first component.
    # Unicode-aware ``\w`` prevents slash-separated Chinese prose from being
    # mistaken for a path.  A colon is an allowed boundary so labelled values
    # such as ``path:/custom/private`` fail closed.  The paired-slash guards
    # keep URLs and protocol-relative ``//intranet/docs`` values out.
    re.compile(
        rf"""(?<![\w./-])/(?!/)(?=[^{_PATH_TERMINATORS}/])"""
        rf"""[^{_PATH_TERMINATORS}]*""",
        re.I,
    ),
)
SECRET_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d[\d\s().-]{7,}\d|1[3-9]\d{9}|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?!\w)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contains_absolute_path(value: Any) -> bool:
    """Return whether any recursive value contains a local absolute path."""
    if isinstance(value, dict):
        return any(
            contains_absolute_path(key) or contains_absolute_path(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_absolute_path(item) for item in value)
    text = str(value or "")
    if PATH_REDACTION_MARKER in text:
        return True
    # Every supported local path shape contains a slash or backslash. Most
    # card fields are opaque IDs or short labels, so avoid six regex searches
    # for the overwhelmingly common non-path case.
    if "/" not in text and "\\" not in text:
        return False
    if text.strip() == "/":
        return True
    cache = _decision_text_read_cache.get()
    key = (text, "absolute_path")
    if cache is not None and key in cache:
        cache.move_to_end(key)
        return cache[key]
    result = any(pattern.search(text) for pattern in ABSOLUTE_PATH_RES)
    if cache is not None:
        cache.remember(key, result)
    return result


def redact_absolute_paths(value: Any) -> str:
    """Replace platform-local absolute paths in a scalar with one marker."""
    text = str(value or "")
    if "/" not in text and "\\" not in text:
        return text
    if text.strip() == "/":
        return PATH_REDACTION_MARKER
    for pattern in ABSOLUTE_PATH_RES:
        text = pattern.sub(PATH_REDACTION_MARKER, text)
    return text


def redact_absolute_paths_in_value(value: Any) -> Any:
    """Recursively redact absolute paths while preserving container shapes."""
    if isinstance(value, dict):
        return {
            redact_absolute_paths_in_value(key): redact_absolute_paths_in_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_absolute_paths_in_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_absolute_paths_in_value(item) for item in value)
    if isinstance(value, set):
        return {redact_absolute_paths_in_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact_absolute_paths_in_value(item) for item in value)
    if isinstance(value, str):
        # Cards must never expose the remainder of a path containing spaces
        # after its absolute prefix was replaced.
        return PATH_REDACTION_MARKER if contains_absolute_path(value) else value
    return value


_decision_text_read_cache = ContextVar("decision_text_read_cache", default=None)


class _DecisionTextReadCache(OrderedDict):
    """Bound both entry count and retained source characters, including keys."""
    def __init__(self):
        super().__init__()
        self.characters = 0

    def remember(self, key, value):
        cost = len(key[0]) + (len(value) if isinstance(value, str) else 0)
        if cost > 4 * 1024 * 1024:
            return
        while self and (len(self) >= 4096 or self.characters + cost > 4 * 1024 * 1024):
            old_key, old_value = self.popitem(last=False)
            self.characters -= len(old_key[0]) + (len(old_value) if isinstance(old_value, str) else 0)
        self[key] = value
        self.characters += cost

    def clear(self):
        super().clear()
        self.characters = 0


@contextmanager
def decision_text_read_cache(*, reuse_existing=False):
    """Bounded memoization of pure redaction for one synchronous card read.

    Nothing persists across reads; reset and discard all text on every exit.
    Context isolation avoids changing another thread's redaction behaviour.
    """
    if reuse_existing and _decision_text_read_cache.get() is not None:
        yield
        return
    cache = _DecisionTextReadCache()
    token = _decision_text_read_cache.set(cache)
    try:
        yield
    finally:
        _decision_text_read_cache.reset(token)
        cache.clear()


def safe_decision_text(value: Any, *, max_len: int = 700) -> str:
    text = " ".join(str(value or "").split())
    cache = _decision_text_read_cache.get()
    key = (text, max_len)
    if cache is not None and key in cache:
        cache.move_to_end(key)
        return cache[key]
    result = _redact_decision_text(text, max_len)
    if cache is not None:
        cache.remember(key, result)
    return result


def _redact_decision_text(text: str, max_len: int) -> str:
    # A path may contain unquoted spaces.  Replacing only its first token would
    # leave the remaining directory or filename visible, so fail closed for
    # the complete scalar whenever any absolute-path prefix is present.
    text = PATH_REDACTION_MARKER if contains_absolute_path(text) else text
    text = SECRET_RE.sub("[secret_redacted]", text)
    text = EMAIL_RE.sub("[email_redacted]", text)
    text = PHONE_RE.sub("[phone_redacted]", text)
    return text[:max_len].rstrip()


def unsafe_decision_text(value: Any) -> bool:
    text = str(value or "")
    lowered = text.lower()
    if any(fragment in lowered for fragment in ["raw ocr", "raw screenshot", "oauth token", "api token", "private key"]):
        return True
    sanitized = safe_decision_text(text, max_len=max(700, len(text)))
    return any(marker in sanitized for marker in ["[path_redacted]", "[secret_redacted]", "[email_redacted]", "[phone_redacted]"])


def item_key_for_review_signal(item: dict[str, Any], source_kind: str) -> str:
    blob = json.dumps(
        {
            "source_kind": source_kind,
            "time_label": item.get("time_label") or "",
            "topic": item.get("topic") or "",
            "app": item.get("app") or "",
            "safe_summary": item.get("safe_summary") or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "review_" + _hash(blob)[:24]


def item_key_for_memory_candidate(package_id: str, item: dict[str, Any]) -> str:
    blob = json.dumps(
        {
            "package_id": package_id,
            "safe_summary": item.get("safe_summary") or "",
            "candidate_type": item.get("candidate_type") or "",
            "time_label": item.get("time_label") or "",
            "subject_scope": item.get("subject_scope") or "",
            "source_category": item.get("source_category") or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "cand_" + _hash(blob)[:24]


def decision_signature_for_item(item: dict[str, Any], *, source_kind: str, item_type: str) -> str:
    blob = json.dumps(
        {
            "source_kind": source_kind,
            "item_type": item_type,
            "signal_type": item.get("type") or item.get("signal_type") or "",
            "candidate_type": item.get("candidate_type") or "",
            "workflow": item.get("workflow") or "",
            "topic": item.get("topic") or "",
            "app": item.get("app") or "",
            "source_category": item.get("source_category") or "",
            "subject_scope": item.get("subject_scope") or "",
            "anchor_type": item.get("anchor_type") or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sig_" + _hash(blob)[:32]


def find_user_decision(conn: sqlite3.Connection, item: dict[str, Any], *, source_kind: str, item_type: str) -> dict[str, Any] | None:
    item_key = str(item.get("item_key") or item.get("candidate_key") or "")
    signature = str(item.get("decision_signature") or decision_signature_for_item(item, source_kind=source_kind, item_type=item_type))
    exact = None
    if item_key:
        exact = conn.execute(
            """
            SELECT *
            FROM user_decisions
            WHERE item_key = ?
            LIMIT 1
            """,
            (item_key,),
        ).fetchone()
    if exact:
        decision = _row_to_decision(exact, match_type="exact")
        # Historical write decisions without reversible source provenance must
        # never suppress a new candidate or act as evidence of a durable write.
        if decision.get("action") not in WRITE_ACTIONS or _decision_has_complete_provenance(decision):
            return decision
    similar = conn.execute(
        """
        SELECT *
        FROM user_decisions
        WHERE decision_signature = ?
          AND generalize_scope = 'similar'
          AND action IN ('context-only', 'never-write')
        ORDER BY datetime(updated_at) DESC
        LIMIT 1
        """,
        (signature,),
    ).fetchone()
    if similar:
        return _row_to_decision(similar, match_type="similar")
    return None


def upsert_user_decision(
    conn: sqlite3.Connection,
    *,
    item: dict[str, Any],
    source_kind: str,
    item_type: str,
    action: str,
    note: str = "",
    generalize_scope: str = "similar",
    applied_plan_id: str = "",
) -> dict[str, Any]:
    if action not in ALL_ACTIONS:
        raise ValueError(f"unsupported decision action: {action}")
    if generalize_scope not in GENERALIZE_SCOPES:
        raise ValueError(f"unsupported decision generalize scope: {generalize_scope}")
    item_key = str(item.get("item_key") or item.get("candidate_key") or "")
    if not item_key:
        raise ValueError("decision item_key is required")
    safe_summary = safe_decision_text(item.get("safe_summary") or item.get("summary") or "")
    if not safe_summary:
        raise ValueError("decision safe_summary is required")
    signature = str(item.get("decision_signature") or decision_signature_for_item(item, source_kind=source_kind, item_type=item_type))
    provenance = _decision_provenance(item)
    if action in WRITE_ACTIONS:
        if str(item.get("subject_scope") or "") != "self_or_user_owned":
            raise ValueError("write decision requires explicit self ownership")
        if provenance["provenance_status"] != PROVENANCE_COMPLETE:
            raise ValueError("write decision requires complete reversible source provenance")
    now = utc_now()
    existing = conn.execute("SELECT * FROM user_decisions WHERE item_key = ?", (item_key,)).fetchone()
    created_at = existing["created_at"] if existing else now
    if existing:
        existing_provenance = _row_provenance(existing)
        if _provenance_rank(existing_provenance["provenance_status"]) > _provenance_rank(provenance["provenance_status"]):
            provenance = existing_provenance
    conn.execute(
        """
        INSERT INTO user_decisions(
          item_key, item_type, source_kind, action, safe_summary, user_note,
          decision_signature, generalize_scope, applied_plan_id,
          source_package_id, source_activity_ids, source_start, source_end,
          deletion_group_ids, provenance_status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
          item_type = excluded.item_type,
          source_kind = excluded.source_kind,
          action = excluded.action,
          safe_summary = excluded.safe_summary,
          user_note = excluded.user_note,
          decision_signature = excluded.decision_signature,
          generalize_scope = excluded.generalize_scope,
          applied_plan_id = excluded.applied_plan_id,
          source_package_id = excluded.source_package_id,
          source_activity_ids = excluded.source_activity_ids,
          source_start = excluded.source_start,
          source_end = excluded.source_end,
          deletion_group_ids = excluded.deletion_group_ids,
          provenance_status = excluded.provenance_status,
          updated_at = excluded.updated_at
        """,
        (
            item_key,
            item_type,
            source_kind,
            action,
            safe_summary,
            safe_decision_text(note, max_len=360),
            signature,
            generalize_scope,
            applied_plan_id,
            provenance["source_package_id"],
            json.dumps(provenance["source_activity_ids"], ensure_ascii=False, sort_keys=True),
            provenance["source_start"],
            provenance["source_end"],
            json.dumps(provenance["deletion_group_ids"], ensure_ascii=False, sort_keys=True),
            provenance["provenance_status"],
            created_at,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{_hash(now + item_key)[:24]}",
            now,
            "user_decision_record",
            "decision_queue_item",
            item_key,
            json.dumps(
                {
                    "source_kind": source_kind,
                    "item_type": item_type,
                    "decision_action": action,
                    "generalize_scope": generalize_scope,
                    "applied_plan_id": applied_plan_id,
                    "provenance_status": provenance["provenance_status"],
                    "raw_access": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    conn.commit()
    stored = conn.execute("SELECT * FROM user_decisions WHERE item_key = ?", (item_key,)).fetchone()
    return _row_to_decision(stored, match_type="exact") if stored else {}


def list_user_decisions(conn: sqlite3.Connection, *, limit: int = 40) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM user_decisions
        ORDER BY datetime(updated_at) DESC
        LIMIT ?
        """,
        (max(1, int(limit or 40)),),
    ).fetchall()
    return [_row_to_decision(row, match_type="exact") for row in rows]


def public_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    if not decision:
        return {}
    return {
        "item_key": decision.get("item_key") or "",
        "action": decision.get("action") or "",
        "match_type": decision.get("match_type") or "",
        "generalize_scope": decision.get("generalize_scope") or "",
        "provenance_status": decision.get("provenance_status") or PROVENANCE_LEGACY_UNKNOWN,
        "write_basis_valid": bool(decision.get("write_basis_valid")),
        "updated_at": decision.get("updated_at") or "",
    }


def _row_to_decision(row: sqlite3.Row | None, *, match_type: str) -> dict[str, Any]:
    if row is None:
        return {}
    decision = {
        "item_key": row["item_key"],
        "item_type": row["item_type"],
        "source_kind": row["source_kind"],
        "action": row["action"],
        "safe_summary": row["safe_summary"],
        "user_note": row["user_note"] or "",
        "decision_signature": row["decision_signature"],
        "generalize_scope": row["generalize_scope"],
        "applied_plan_id": row["applied_plan_id"] or "",
        **_row_provenance(row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "match_type": match_type,
    }
    decision["write_basis_valid"] = bool(
        decision["action"] not in WRITE_ACTIONS or _decision_has_complete_provenance(decision)
    )
    return decision


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _decision_provenance(item: dict[str, Any]) -> dict[str, Any]:
    package_id = safe_decision_text(item.get("source_package_id") or item.get("package_id") or "", max_len=120)
    if package_id == "latest":
        package_id = ""
    activity_ids = _safe_id_list(item.get("activity_ids") or item.get("source_activity_ids"), limit=24)
    time_range = item.get("time_range") if isinstance(item.get("time_range"), dict) else {}
    source_start = safe_decision_text(item.get("source_start") or time_range.get("from") or "", max_len=80)
    source_end = safe_decision_text(item.get("source_end") or time_range.get("to") or "", max_len=80)
    if not _valid_source_time_range(source_start, source_end):
        source_start = ""
        source_end = ""
    deletion_group_ids = _safe_id_list(item.get("deletion_group_ids"), limit=32)
    complete = bool(package_id and activity_ids and source_start and source_end and deletion_group_ids)
    traceable = bool((source_start and source_end) or package_id or activity_ids or deletion_group_ids)
    status = PROVENANCE_COMPLETE if complete else PROVENANCE_TRACEABLE if traceable else PROVENANCE_LEGACY_UNKNOWN
    return {
        "source_package_id": package_id,
        "source_activity_ids": activity_ids,
        "source_start": source_start,
        "source_end": source_end,
        "deletion_group_ids": deletion_group_ids,
        "provenance_status": status,
    }


def _row_provenance(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    status = str(row["provenance_status"] or PROVENANCE_LEGACY_UNKNOWN) if "provenance_status" in keys else PROVENANCE_LEGACY_UNKNOWN
    if status not in PROVENANCE_STATUSES:
        status = PROVENANCE_LEGACY_UNKNOWN
    return {
        "source_package_id": str(row["source_package_id"] or "") if "source_package_id" in keys else "",
        "source_activity_ids": _json_id_list(row["source_activity_ids"] if "source_activity_ids" in keys else "[]"),
        "source_start": str(row["source_start"] or "") if "source_start" in keys else "",
        "source_end": str(row["source_end"] or "") if "source_end" in keys else "",
        "deletion_group_ids": _json_id_list(row["deletion_group_ids"] if "deletion_group_ids" in keys else "[]"),
        "provenance_status": status,
    }


def _decision_has_complete_provenance(decision: dict[str, Any]) -> bool:
    return bool(
        decision.get("provenance_status") == PROVENANCE_COMPLETE
        and decision.get("source_package_id")
        and decision.get("source_activity_ids")
        and decision.get("source_start")
        and decision.get("source_end")
        and decision.get("deletion_group_ids")
    )


def _safe_id_list(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    output: list[str] = []
    for entry in parsed:
        safe = safe_decision_text(entry, max_len=120)
        if safe and safe not in output:
            output.append(safe)
        if len(output) >= limit:
            break
    return output


def _json_id_list(value: Any) -> list[str]:
    return _safe_id_list(value, limit=32)


def _provenance_rank(status: str) -> int:
    return {
        PROVENANCE_LEGACY_UNKNOWN: 0,
        PROVENANCE_TRACEABLE: 1,
        PROVENANCE_COMPLETE: 2,
    }.get(status, 0)


def _valid_source_time_range(source_start: str, source_end: str) -> bool:
    if not source_start or not source_end:
        return False
    try:
        return datetime.fromisoformat(source_start) <= datetime.fromisoformat(source_end)
    except (TypeError, ValueError):
        return False
