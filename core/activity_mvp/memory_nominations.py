"""AI nominations into the shared memory review backlog.

The daily governance audit only finds candidates by heuristics. When a trusted
AI holds direct evidence that one memory is outdated (for example, a newer
record that replaces it), it may nominate that memory for Local Owner's review.

A nomination decides nothing and changes no retrieval: it is a separate,
append-only record that the review entry shows next to the audit queue. Only
a confirmed ``memory-review apply`` writes a lifecycle decision. The nominator
identity comes from the calling process ancestry, never from arguments or
environment, and every nomination binds the target's current content hash.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from . import memory_review as review


DEFAULT_NOMINATIONS_PATH = Path(
    "~/.hermes/memory-review-center/state/nominations.sqlite3"
).expanduser()
NOMINATION_CATEGORY = "ai_nomination"
NOMINATION_REASON = "MEM-NOMINATION-001"
MAX_TEXT_CHARS = 500
MAX_EVIDENCE_ITEMS = 10
MAX_ANCESTRY_DEPTH = 64
SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY|(token|secret|password|api[_-]?key)\s*[:=]\s*\S+)"
)

_HOME = str(Path.home())
# Matched against the real executable path from the kernel (proc_pidpath), so
# an argv[0] rename cannot impersonate a host.
EXECUTABLE_RULES: tuple[tuple[str, str, Callable[[str], bool]], ...] = (
    ("claude", "claude-code",
     lambda p: "/claude-code/" in p or "/@anthropic-ai/claude-code/" in p),
    ("claude", "claude-desktop",
     lambda p: p == "/Applications/Claude.app/Contents/MacOS/Claude"),
    ("codex", "codex",
     lambda p: p.startswith("/Applications/ChatGPT.app/Contents/Resources/codex")
     or "/@openai/codex/" in p),
    ("workbuddy", "workbuddy", lambda p: p.startswith("/Applications/WorkBuddy.app/")),
    ("qwenwork", "qwenwork", lambda p: p.startswith("/Applications/QwenWorkCN.app/")),
    ("mimocode", "mimocode",
     lambda p: p == f"{_HOME}/.local/bin/mimo"
     or p.startswith("/Applications/Xiaomi MiMo.app/")),
    ("openclaw", "openclaw",
     lambda p: p.startswith(f"{_HOME}/.openclaw/")
     or p.startswith("/Applications/OpenClaw.app/")),
)
# Hermes gateways run an ordinary Python interpreter, so they are recognised
# by their registered launchd job instead. A double-forked orphan is not one.
LABEL_RULES: tuple[tuple[str, str], ...] = (
    ("ai.hermes.gateway-", "hermes"),
    ("ai.openclaw.gateway", "openclaw"),
)


def _executable_path(pid: int) -> str:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(4096)
    if libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer)) <= 0:
        return ""
    return buffer.value.decode("utf-8", "replace")


def _parent_pid(pid: int) -> int:
    output = subprocess.run(
        ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True, text=True, timeout=5, check=False,
    ).stdout.strip()
    return int(output) if output.isdigit() else 0


def _launchd_labels() -> dict[int, str]:
    output = subprocess.run(
        ["/bin/launchctl", "list"], capture_output=True, text=True, timeout=5, check=False,
    ).stdout
    labels: dict[int, str] = {}
    for line in output.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            labels[int(parts[0])] = parts[2]
    return labels


def _classify(pid: int, path: str, label: str) -> dict[str, Any] | None:
    for prefix, platform in LABEL_RULES:
        if label.startswith(prefix):
            agent = platform
            if prefix.endswith("-"):
                agent = f"{platform}:{label[len(prefix):]}"
            return {"pid": pid, "platform": platform, "agent": agent,
                    "evidence": "launchd_label", "detail": label}
    for platform, agent, matches in EXECUTABLE_RULES:
        if path and matches(path):
            return {"pid": pid, "platform": platform, "agent": agent,
                    "evidence": "executable_path", "detail": path}
    return None


def detect_nominator(
    *,
    start_pid: int | None = None,
    executable_path: Callable[[int], str] = _executable_path,
    parent_pid: Callable[[int], int] = _parent_pid,
    launchd_labels: Callable[[], dict[int, str]] = _launchd_labels,
) -> dict[str, Any]:
    """Name the AI host that launched this process, from OS facts only."""
    labels = launchd_labels()
    pid = os.getppid() if start_pid is None else start_pid
    matches: list[dict[str, Any]] = []
    for _ in range(MAX_ANCESTRY_DEPTH):
        if pid <= 1:
            break
        found = _classify(pid, executable_path(pid), labels.get(pid, ""))
        if found:
            matches.append(found)
        pid = parent_pid(pid)
    platforms = {match["platform"] for match in matches}
    if not matches:
        raise review.MemoryReviewError("nominator_unrecognized: no trusted AI host in process ancestry")
    if len(platforms) > 1:
        # One AI running inside another cannot pick whose name it nominates in.
        raise review.MemoryReviewError(
            "nominator_ambiguous: " + ",".join(sorted(platforms))
        )
    nearest = matches[0]
    return {
        "platform": nearest["platform"],
        "agent": nearest["agent"],
        "evidence": nearest["evidence"],
        "detail": nearest["detail"],
        "matched_hosts": matches,
        "method": "process_ancestry",
    }


def _clean_text(value: Any, *, label: str, required: bool) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise review.MemoryReviewError(f"{label} is required")
    if len(text) > MAX_TEXT_CHARS:
        raise review.MemoryReviewError(f"{label} is longer than {MAX_TEXT_CHARS} characters")
    if SECRET_RE.search(text):
        raise review.MemoryReviewError(f"{label} looks like it contains a credential")
    return text


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.execute("PRAGMA trusted_schema=OFF")
    actions = ",".join(f"'{value}'" for value in review.ACTIONS)
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS nominations (
            nomination_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            action TEXT NOT NULL CHECK(action IN ({actions})),
            note TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            related_memory_ids_json TEXT NOT NULL,
            candidate_fingerprint TEXT NOT NULL CHECK(length(candidate_fingerprint) = 64),
            policy_version TEXT NOT NULL,
            nominator_platform TEXT NOT NULL,
            nominator_agent TEXT NOT NULL,
            nominator_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(memory_id, content_sha256)
        );
        CREATE TRIGGER IF NOT EXISTS nominations_no_update
            BEFORE UPDATE ON nominations
            BEGIN SELECT RAISE(ABORT, 'nominations_are_append_only'); END;
        CREATE TRIGGER IF NOT EXISTS nominations_no_delete
            BEFORE DELETE ON nominations
            BEGIN SELECT RAISE(ABORT, 'nominations_are_append_only'); END;
        """
    )
    return connection


def _require_live_replacement(
    memory_id: str,
    *,
    queue: dict[str, Any],
    decisions: dict[tuple[str, str], dict[str, str]],
    chroma_path: Path | None,
) -> dict[str, str]:
    """A replacement must itself be retrievable, or the fact disappears with it."""
    current = review.read_memory_text(memory_id, chroma_path=chroma_path)
    if str(current["status"] or "active") != "active":
        raise review.MemoryReviewError(f"replacement memory is not active: {memory_id}")
    override = decisions.get((memory_id, current["content_sha256"]))
    if override and override["lifecycle_status"] != "active":
        raise review.MemoryReviewError(f"replacement memory is retired: {memory_id}")
    queued = {item["memory_id"]: item for item in queue["items"]}.get(memory_id)
    if queued and not (
        override
        and override["decision"] == "keep_active"
        and override.get("candidate_fingerprint") == queued["candidate_fingerprint"]
    ):
        raise review.MemoryReviewError(f"replacement memory is still pending review: {memory_id}")
    return {
        "type": "superseded_by",
        "memory_id": memory_id,
        "content_sha256": current["content_sha256"],
    }


def nominate(
    memory_id: str,
    *,
    action: str,
    note: str,
    superseded_by: list[str] | None = None,
    evidence_text: list[str] | None = None,
    nominator: dict[str, Any] | None = None,
    nominations_path: Path | None = None,
    queue_path: Path | None = None,
    ledger_path: Path | None = None,
    chroma_path: Path | None = None,
) -> dict[str, Any]:
    """Record one nomination; retrieval and the decision ledger stay untouched."""
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        raise review.MemoryReviewError("memory_id is required")
    if action not in review.ACTIONS:
        raise review.MemoryReviewError(f"unsupported action: {action or '(empty)'}")
    note = _clean_text(note, label="note", required=True)
    replacement_ids = list(dict.fromkeys(
        str(value).strip() for value in superseded_by or [] if str(value).strip()
    ))
    statements = [
        _clean_text(value, label="evidence", required=True) for value in evidence_text or []
    ]
    if not replacement_ids and not statements:
        raise review.MemoryReviewError("a nomination needs at least one piece of evidence")
    if len(replacement_ids) + len(statements) > MAX_EVIDENCE_ITEMS:
        raise review.MemoryReviewError(f"a nomination holds at most {MAX_EVIDENCE_ITEMS} evidence items")
    if memory_id in replacement_ids:
        raise review.MemoryReviewError("a memory cannot replace itself")

    identity = nominator or detect_nominator()
    target = review.read_memory_text(memory_id, chroma_path=chroma_path)
    queue = review.read_queue(queue_path)
    decisions = review.read_decisions(ledger_path)
    evidence = [
        _require_live_replacement(
            value, queue=queue, decisions=decisions, chroma_path=chroma_path
        )
        for value in replacement_ids
    ]
    evidence.extend({"type": "statement", "text": text} for text in statements)
    fingerprint = review._candidate_fingerprint(
        policy_version=review.REVIEW_POLICY_VERSION,
        memory_id=memory_id,
        content_sha256=target["content_sha256"],
        categories=[NOMINATION_CATEGORY],
        related_memory_ids=replacement_ids,
    )
    record = {
        "nomination_id": uuid.uuid4().hex,
        "memory_id": memory_id,
        "content_sha256": target["content_sha256"],
        "action": action,
        "note": note,
        "evidence": evidence,
        "related_memory_ids": sorted(replacement_ids),
        "candidate_fingerprint": fingerprint,
        "policy_version": review.REVIEW_POLICY_VERSION,
        "nominator": identity,
        "created_at": review._utc_now(),
    }
    path = Path(nominations_path) if nominations_path else DEFAULT_NOMINATIONS_PATH
    connection = _connect(path)
    try:
        connection.execute(
            """
            INSERT INTO nominations(
                nomination_id, memory_id, content_sha256, action, note, evidence_json,
                related_memory_ids_json, candidate_fingerprint, policy_version,
                nominator_platform, nominator_agent, nominator_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["nomination_id"], memory_id, record["content_sha256"], action, note,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                json.dumps(record["related_memory_ids"]),
                fingerprint, record["policy_version"],
                identity["platform"], identity["agent"],
                json.dumps(identity, ensure_ascii=False, sort_keys=True),
                record["created_at"],
            ),
        )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise review.MemoryReviewError(
                f"this version of the memory is already nominated: {memory_id}"
            ) from exc
        raise review.MemoryReviewError(f"nomination write failed: {exc}") from exc
    finally:
        connection.close()
    os.chmod(path, 0o600)
    return {
        "interface": "memory-review",
        "contract": 2,
        "nominated": True,
        "nomination_id": record["nomination_id"],
        "memory_id": memory_id,
        "fingerprint": record["content_sha256"][:12],
        "action": action,
        "nominator": {"platform": identity["platform"], "agent": identity["agent"]},
        "evidence": evidence,
        "retrieval_changed": False,
        "next": "Local Owner reviews with memory-review list / preview / apply",
        "raw_access": False,
    }


def nomination_items(nominations_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return the newest nomination per memory, shaped like queue items."""
    path = Path(nominations_path) if nominations_path else DEFAULT_NOMINATIONS_PATH
    if not path.exists():
        return {}
    connection = review._connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT * FROM nominations ORDER BY created_at, nomination_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise review.MemoryReviewError("nomination store read failed") from exc
    finally:
        connection.close()
    items: dict[str, dict[str, Any]] = {}
    for row in rows:
        related = json.loads(row["related_memory_ids_json"])
        expected = review._candidate_fingerprint(
            policy_version=row["policy_version"],
            memory_id=row["memory_id"],
            content_sha256=row["content_sha256"],
            categories=[NOMINATION_CATEGORY],
            related_memory_ids=related,
        )
        if (
            row["policy_version"] != review.REVIEW_POLICY_VERSION
            or row["candidate_fingerprint"] != expected
        ):
            # A tampered or old-policy nomination is not offered for review.
            continue
        evidence = json.loads(row["evidence_json"])
        items[row["memory_id"]] = {
            "memory_id": row["memory_id"],
            "content_sha256": row["content_sha256"],
            "candidate_fingerprint": row["candidate_fingerprint"],
            "policy_version": row["policy_version"],
            "categories": [NOMINATION_CATEGORY],
            "reason_codes": [NOMINATION_REASON],
            "related_memory_ids": related,
            "rule_ids": [NOMINATION_REASON],
            "evidence_types": [NOMINATION_CATEGORY],
            "last_seen_at": row["created_at"],
            "source": NOMINATION_CATEGORY,
            "nomination": {
                "nomination_id": row["nomination_id"],
                "suggested_action": row["action"],
                "note": row["note"],
                "evidence": evidence,
                "nominator_platform": row["nominator_platform"],
                "nominator_agent": row["nominator_agent"],
                "nominated_at": row["created_at"],
            },
        }
    return items
