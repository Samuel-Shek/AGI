"""Explicit source lookup and local investigation notes, never memory decisions.

Search reads filenames and mail index fields only. Recording requires a named
memory authorization and exact source hashes. Refresh reads only the resulting
note and file metadata; it never opens business or memory bodies. Notes are
operator findings, not an independent authority or a permission to share.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import memory_review as memory
from .user_decisions import safe_decision_text, unsafe_decision_text, contains_absolute_path

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_NOTE_AGE = timedelta(hours=72)


def root_for(database):
    return Path(database).resolve().parent / "attention-center"


def _private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("核查配置或记录的访问权限不符合本机要求。")
    value = json.loads(memory._bounded_regular_file(path, max_bytes=65536))
    if not isinstance(value, dict):
        raise ValueError("核查配置或记录必须是结构化对象。")
    return value


def source_config(database):
    value = _private_json(root_for(database) / "sources.json")
    if value.get("schema_version") != 1 or value.get("owner_id") != memory.OWNER_ID:
        raise ValueError("业务来源配置归属或版本不匹配。")
    result = {}
    for key in ("memory_root", "email_database", "email_archive_root"):
        path = Path(value[key])
        if not path.is_absolute() or path.is_symlink() or not path.exists():
            raise ValueError("已配置的业务来源暂不可用。")
        result[key] = path.resolve()
    return result


def search_sources(database, queries, limit=20):
    """On-demand bounded discovery; no body, attachment, or memory-store read."""
    if not isinstance(queries, list) or not 1 <= len(queries) <= 5 or any(
        not isinstance(q, str) or not 2 <= len(q.strip()) <= 60 for q in queries
    ):
        raise ValueError("请提供 1 至 5 个明确关键词，每个 2 至 60 字。")
    queries = [q.strip() for q in queries]
    config = source_config(database)
    results, visited, truncated = [], 0, False
    # Filename discovery deliberately excludes archival, template and backup trees.
    for directory, folders, files in os.walk(config["memory_root"], followlinks=False):
        folders[:] = sorted(f for f in folders if not f.startswith((".", "91_", "99_"))
                            and not (Path(directory) / f).is_symlink())
        visited += 1
        if visited > 2000:
            truncated = True
            break
        for name in sorted(files):
            if name.endswith(".md") and any(q.casefold() in name.casefold() for q in queries):
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                results.append({"kind": "business_index", "title": safe_decision_text(path.stem),
                                "local_path": str(path), "evidence": "filename_only"})
                if len(results) >= 100:
                    truncated = True
                    break
        if len(results) >= 100:
            break
    conn = sqlite3.connect(config["email_database"].as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    started = time.monotonic()
    conn.set_progress_handler(lambda: int(time.monotonic() - started > 2), 1000)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        where = " OR ".join("instr(lower(subject), lower(?)) > 0" for _ in queries)
        rows = conn.execute("SELECT subject,received_at,direction,archive_path FROM messages "
                            "WHERE archive_state='archived' AND (" + where + ") "
                            "ORDER BY received_at DESC LIMIT 101", queries).fetchall()
        truncated |= len(rows) > 100
        for row in rows[:100]:
            archive = Path(row["archive_path"] or "")
            if not archive.is_absolute() or not archive.resolve().is_relative_to(config["email_archive_root"]):
                continue
            results.append({"kind": "mail_index", "title": safe_decision_text(row["subject"]),
                            "observed_at": row["received_at"], "direction": row["direction"],
                            "local_path": str(archive), "evidence": "mail_index_only"})
    finally:
        conn.close()
    results.sort(key=lambda r: (r.get("observed_at", ""), r["title"]), reverse=True)
    # Reserve space for both layers: mail threads must not crowd out the vault.
    mails = [r for r in results if r["kind"] == "mail_index"]
    pages = [r for r in results if r["kind"] == "business_index"]
    selected = (pages[:limit // 2] + mails[:limit - min(len(pages), limit // 2)])[:limit]
    return {"status": "ok", "items": selected, "truncated": truncated or len(results) > len(selected),
            "raw_access": False, "fact_verified": False,
            "boundary": "仅定位本机索引。标题不证明批准、当前岗位或结果；正文读取沿用已有有效任务授权，遵守用户本次具体限制，不扩大范围。"}


def _signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise ValueError("来源不是有界普通文件。")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _store_signature():
    # Chroma may change before the daily audit catches up. Detect that without
    # reading any memory body on refresh, including changes still in its WAL.
    path = memory.DEFAULT_CHROMA_PATH
    result = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            result.append(None)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("记忆存储身份无法核对。")
        result.append([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
    if result[0] is None:
        raise ValueError("记忆存储暂不可用。")
    return result


def _source_path(value, config):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("来源必须是已配置目录内的普通文件。")
    resolved = path.resolve()
    if not any(resolved.is_relative_to(config[k]) for k in ("memory_root", "email_archive_root")):
        raise ValueError("来源不在已配置的授权业务目录内。")
    return resolved


def _note_path(database, issue_id):
    if not re.fullmatch(r"memory-[0-9a-f]{24}", issue_id):
        raise ValueError("核查事项标识无效。")
    return root_for(database) / "investigations" / (issue_id + ".json")


def record(database, issue, payload):
    """An explicit operator command. No review-table or Chroma writes."""
    members = issue.get("members", [])
    if issue.get("kind") != "memory" or len(members) != 1:
        raise ValueError("核查记录必须绑定一条明确的记忆。")
    member = members[0]
    if payload.get("authorized_memory_id") != member["id"] or payload.get("revision") != issue["revision"]:
        raise ValueError("缺少这条正文的明确授权，或事项版本已经变化。")
    summary = payload.get("finding", "")
    if not isinstance(summary, str) or not 1 <= len(summary) <= 700 or unsafe_decision_text(summary) or contains_absolute_path(summary):
        raise ValueError("核查结论应为不含私密原文的简短说明。")
    evidence = payload.get("sources")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise ValueError("核查结论需要 1 至 8 个精确来源及内容哈希。")
    config = source_config(database)
    store_before = _store_signature()
    actual = memory.read_memory_text(member["id"])
    if actual["content_sha256"] != member["content_sha256"]:
        raise ValueError("记忆正文已变化，必须重新核查。")
    sources = []
    for source in evidence:
        path = _source_path(source["path"], config)
        before = _signature(path)
        sha = hashlib.sha256(memory._bounded_regular_file(path, max_bytes=MAX_FILE_BYTES)).hexdigest()
        if sha != source.get("sha256") or before != _signature(path):
            raise ValueError("来源内容已变化，不能保存旧核查结论。")
        sources.append({"path": str(path), "sha256": sha, "signature": before})
    if store_before != _store_signature():
        raise ValueError("记忆存储在核查时变化，请重新核对。")
    now = datetime.now(timezone.utc)
    note = {"schema_version": 1, "owner_id": memory.OWNER_ID, "issue_id": issue["id"],
            "memory_id": member["id"], "content_sha256": member["content_sha256"],
            "candidate_fingerprint": member["candidate_fingerprint"],
            "finding": safe_decision_text(summary), "sources": sources, "store_signature": store_before,
            "recorded_at": now.isoformat(), "valid_until": (now + MAX_NOTE_AGE).isoformat(),
            "author": "local_operator", "lifecycle_changed": False, "share_authorized": False}
    path = _note_path(database, issue["id"])
    if path.parent.is_symlink():
        raise ValueError("核查目录不可为符号链接。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("核查目录权限不符合本机要求。")
    descriptor, temporary = tempfile.mkstemp(prefix=".note-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(note, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"status": "evidence_recorded", "lifecycle_changed": False,
            "message": "本机核查记录已保存；记忆审核仍待用户选择，未授权再次转交结论。"}


def project(database, issue):
    """Refresh checks metadata only. Notes never grant retrieval or resolution."""
    try:
        path = _note_path(database, issue["id"])
        if not path.exists():
            return issue
        note = _private_json(path)
        config = source_config(database)
        member = issue["members"][0]
        if (note.get("schema_version") != 1 or note.get("owner_id") != memory.OWNER_ID
            or len(issue["members"]) != 1 or note.get("issue_id") != issue["id"]
            or any(note.get(key) != member[key] for key in ("content_sha256", "candidate_fingerprint"))
            or note.get("memory_id") != member["id"]):
            raise ValueError("核查绑定已变化。")
        now = datetime.now(timezone.utc)
        recorded = datetime.fromisoformat(note["recorded_at"])
        expires = datetime.fromisoformat(note["valid_until"])
        if not recorded <= expires <= recorded + MAX_NOTE_AGE or recorded > now:
            raise ValueError("核查记录时间无效。")
        finding = note.get("finding")
        if not isinstance(finding, str) or not 1 <= len(finding) <= 700 or unsafe_decision_text(finding) or contains_absolute_path(finding):
            raise ValueError("核查记录内容无效。")
        recheck = []
        if now > expires:
            recheck.append("已到复查时间")
        if not 1 <= len(note["sources"]) <= 8:
            raise ValueError("缺少核查来源。")
        for source in note["sources"]:
            try:
                if _signature(_source_path(source["path"], config)) != source["signature"]:
                    recheck.append("来源版本已变化")
            except OSError:
                recheck.append("来源暂不可用")
        try:
            if note.get("store_signature") != _store_signature():
                recheck.append("记忆存储已变化")
        except (OSError, ValueError):
            recheck.append("记忆存储暂不可用")
        # Preserve generic context for subsequent handoffs: a local note is not
        # an authorization to forward its finding to any AI or another task.
        original = {k: issue[k] for k in ("title", "summary", "reason", "recommendation", "attempts")}
        if recheck:
            # Preserve dated work, not current authority. A refresh may neither
            # read private bodies nor turn an old finding into a review decision.
            reasons = "；".join(dict.fromkeys(recheck))
            return {**issue, "title": "已有核查记录 · 待更新核对",
                    "summary": "历史核查记录已保留，当前依据需要更新核对；审核状态未改变。",
                    "attempts": "历史核查（" + recorded.astimezone().strftime("%Y-%m-%d %H:%M") + "）：" + safe_decision_text(finding)
                        + "\n复查原因：" + reasons + "。以上是当时的核查结论，不代表当前已重新核验。",
                    "recommendation": "先复用历史核查线索，对照变化的依据继续处理；无需重复填写相同备注。使用方式仍须按已确认范围落实并核验。",
                    "investigation_state": "needs_recheck", "handoff_context": original,
                    "revision": hashlib.sha256((issue["revision"] + json.dumps(note, sort_keys=True) + reasons).encode()).hexdigest()}
        return {**issue, "title": "已核查依据 · 待选择使用方式", "summary": "已有本机核查记录，审核状态未改变。",
                "background": safe_decision_text(note["finding"]),
                "reason": "原标签来自自动审计。下方核查记录补充了业务依据；最终使用方式仍由你决定。",
                "attempts": "本机核查记录（" + recorded.astimezone().strftime("%m-%d %H:%M") + "）：" + safe_decision_text(note["finding"]),
                "recommendation": "结合已核查依据选择使用方式；包含历史与仍有效内容的记忆，应分别整理，避免整条误放行或误隔离。",
                "investigation_state": "recorded", "handoff_context": original,
                "revision": hashlib.sha256((issue["revision"] + json.dumps(note, sort_keys=True)).encode()).hexdigest()}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {**issue, "investigation_state": "needs_recheck",
                "attempts": "已有核查记录，但来源、事项版本或有效期需要重新核对；暂不展示旧结论。"}
