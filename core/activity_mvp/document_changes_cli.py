from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .document_changes import (
    document_changes_status,
    list_document_changes,
    resolve_document_change_range,
)
from .storage import connect_readonly

# Limitations that mean "this listing was emptied on purpose", as opposed to
# "nothing changed".  The surface must be able to tell those apart: an empty
# list because the owner revoked a folder is not the same fact as a quiet day,
# and showing the second when the first is true is a lie by omission.
BLOCKING_LIMITATIONS = frozenset(
    {
        "authorization_changed_during_read",
        "authorization_unverifiable",
        "root_control_refs_incomplete",
    }
)


class DatabaseMissingError(RuntimeError):
    """The configured live database does not exist; never fabricate an empty list."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pam-document-changes",
        description="Read-only low-latency file-change endpoint for the local surface",
    )
    parser.add_argument("--db", default="data/activity.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("changes", "coverage"):
        command = commands.add_parser(name)
        command.add_argument(
            "--range",
            dest="range_name",
            choices=("today", "yesterday", "7d", "30d", "90d"),
            default="today",
        )
        command.add_argument("--start", default=None)
        command.add_argument("--end", default=None)
        if name == "changes":
            command.add_argument("--limit", type=int, default=200)
            command.add_argument(
                "--sort",
                dest="sort_order",
                choices=("relevance", "recent"),
                default="recent",
            )
    return parser


def main() -> None:
    args = _parser().parse_args()
    path = Path(args.db).expanduser()
    if not path.exists():
        raise DatabaseMissingError("configured database is missing")
    # The App kills this fast path at 8 seconds; a shorter SQLite busy budget
    # lets contention surface as database_busy instead of a silent SIGKILL.
    conn = connect_readonly(path, busy_timeout_seconds=2.5)
    try:
        if args.command == "coverage":
            payload = {
                "status": "ok",
                "interface": "document_changes_fast_path",
                "raw_access": False,
                "coverage": document_changes_status(conn)["coverage"],
            }
        else:
            start, end = resolve_document_change_range(
                args.range_name, start=args.start, end=args.end
            )
            # for_export=False is the whole reason this endpoint exists.  The
            # `document-changes list` CLI hardcodes for_export=True because it
            # is the bounded downstream collector interface; the owner's own
            # screen is not a downstream collector and must not be gated behind
            # an export switch that ships off by default.
            listing = list_document_changes(
                conn,
                start,
                end,
                limit=args.limit,
                for_export=False,
                sort_order=args.sort_order,
            )
            limitations = list(listing.get("limitations") or [])
            blocked = sorted(BLOCKING_LIMITATIONS.intersection(limitations))
            payload = dict(listing)
            payload["status"] = "ok"
            payload["interface"] = "document_changes_fast_path"
            payload["range"] = args.range_name
            payload["time_range"] = {"start": start, "end": end}
            payload["sort_order"] = args.sort_order
            # An emptied listing is reported as emptied, never as "no changes".
            payload["data_state"] = "blocked" if blocked else "ok"
            payload["blocking_limitations"] = blocked
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    finally:
        conn.close()


def safe_main() -> None:
    try:
        main()
    except Exception as exc:
        if isinstance(exc, DatabaseMissingError):
            error = "database_missing"
        elif isinstance(exc, sqlite3.OperationalError) and any(
            token in str(exc).casefold() for token in ("locked", "busy")
        ):
            error = "database_busy"
        else:
            error = "command_failed"
        print(
            json.dumps(
                {"status": "error", "error": error, "raw_access": False},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    safe_main()
