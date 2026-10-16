"""Local AGI consumer. Owner writes are never exposed through its consumer API."""
from pathlib import Path
import stat
import os
import json
import math
import sqlite3
from datetime import datetime, timezone
from . import codex_bridge as shared
from .storage import connect


def owner_action(conn, action, payload):
    with shared.agi_scope():
        if action == "feedback":
            if (set(payload) - {"share_key", "useful", "resume_seconds", "confirm_owner"}
                    or payload.get("confirm_owner") is not True or type(payload.get("useful")) is not bool):
                raise ValueError("owner_feedback_required")
            seconds = payload.get("resume_seconds")
            if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 86400):
                raise ValueError("invalid_resume_seconds")
            # AGI feedback must not join or alter the existing Codex value experiment.
            with shared.context._context_authorization_lock(conn, exclusive=False):
                shared._find_share(conn, payload.get("share_key"))
                row = conn.execute("SELECT value FROM daemon_state WHERE key='agi_owner_feedback'").fetchone()
                entries = json.loads(row[0]) if row else {}
                entries[payload["share_key"]] = {"useful": payload["useful"], "resume_seconds": seconds}
                entries = dict(list(entries.items())[-500:])
                conn.execute("INSERT OR REPLACE INTO daemon_state(key,value,updated_at) VALUES('agi_owner_feedback',?,?)",
                             (json.dumps(entries), datetime.now(timezone.utc).isoformat()))
                conn.commit()
            return {"status": "feedback_recorded", "raw_access": False}
        result = shared.owner_action(conn, action, payload)
        if action == "snapshot":
            result["consumer"] = "agi"
            result["experiment"] = {"status": "not_configured"}
            row = conn.execute("SELECT value FROM daemon_state WHERE key='agi_owner_feedback'").fetchone()
            feedback = json.loads(row[0]) if row else {}
            for pack in result["shares"]:
                rating = feedback.get(pack["share_key"])
                pack["owner_helpfulness"] = ("helpful" if rating["useful"] else "not_helpful") if rating else "not_assessed"
            result["withdrawal_notice"] = "撤回阻止后续读取；AGI 只保留无正文的引用和审计记录。"
            from .presentation_scope import for_connection
            result = for_connection(conn).project(result)
            if isinstance(result.get('shares'), list):
                result['shares'] = [share for share in result['shares'] if share.get('items')]
            return result
        return result


def open_private(database, *, readonly=False):
    path = Path(database)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("private_context_store_required")
    # Existing database only. Never initialize, migrate, or read activity/OCR.
    conn = sqlite3.connect(path.absolute().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def authorized_references(conn):
    """Authorization/source-graph validation only; no provided/use receipt."""
    with shared.agi_scope():
        from .presentation_scope import for_connection
        scope = for_connection(conn)
        return {item["context_ref"]: {"expires_at": item["expires_at"],
                "share_key": shared._ref("shared", delivery["delivery_ref"])}
                for delivery, items in shared._live_shares(conn) for item in items
                if not scope.hidden(item) and scope.unchanged()}


def consume_candidates(conn, use):
    """Read an explicitly shared package and acknowledge exact successful use.

    `use` is trusted local controller code, not a command supplied by a source.
    Summary text exists only in this call and is never returned in its receipt.
    """
    with shared.agi_scope():
        from .presentation_scope import for_connection
        scope = for_connection(conn)
        consumer = shared.CodexConsumer()
        used, provided, seen = [], [], set()
        for project in consumer.projects(conn)["projects"]:
            response = consumer.search(conn, project_key=project["project_key"], limit=10)
            for package in response["results"]:
                package["items"] = [item for item in package["items"] if not scope.hidden(item) and scope.unchanged()]
                if not package["items"]:
                    continue
                issued = consumer.tickets[package["ticket"]]
                delivery = issued[1]
                share_key = shared._ref("shared", delivery["delivery_ref"])
                if share_key in seen:
                    continue
                seen.add(share_key)
                shared.record_provided(conn, [share_key])
                provided.append(share_key)
                cited = []
                for item in package["items"]:
                    # Only schema-bound owner-safe text is provided to use().
                    # Hold the established shared lease through this bounded use;
                    # withdrawal waits for the admitted use and blocks the next one.
                    with shared.context._context_authorization_lock(conn, exclusive=False):
                        live = shared.context.fetch_prepared_delivery(conn, delivery_ref=delivery["delivery_ref"])
                        if live.get("status") != "available":
                            raise ValueError("source_no_longer_authorized")
                        if scope.hidden(item) or not scope.unchanged():
                            continue
                        use(item, share_key)
                        cited.append(item["reference"])
                if cited:
                    if delivery["state"] != "consumed":
                        consumer.acknowledge(conn, ticket=package["ticket"], cited_references=cited)
                    used.extend(cited)
        return {"status": "analyzed" if used else "empty", "provided_packages": len(provided),
                "cited_references": used, "owner_helpfulness": "not_assessed",
                "raw_access": False, "model_calls": 0, "memory_write_actions": False}
