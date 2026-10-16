from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .activity_confirmations import (
    DEFAULT_CONFIRMATION_CARD_LIMIT,
    activity_confirmation_revision,
    list_activity_confirmation_cards,
)
from .storage import connect_readonly


class DatabaseMissingError(RuntimeError):
    """The configured live database does not exist; never fabricate an empty queue."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pam-confirmation-cards",
        description="Read-only low-latency ConfirmationCardV5 endpoint",
    )
    parser.add_argument("--db", default="data/activity.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("cards", "revision"):
        command = commands.add_parser(name)
        command.add_argument(
            "--range",
            dest="range_name",
            choices=("today", "yesterday", "7d"),
            default="today",
        )
        command.add_argument("--start", default=None)
        command.add_argument("--end", default=None)
        if name == "cards":
            command.add_argument("--limit", type=int, default=DEFAULT_CONFIRMATION_CARD_LIMIT)
            command.add_argument("--cursor", default=None)
            command.add_argument(
                "--section",
                choices=("pending_confirmation", "information_insufficient"),
                default=None,
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
        if args.command == "cards":
            payload = list_activity_confirmation_cards(
                conn,
                range_name=args.range_name,
                start=args.start,
                end=args.end,
                limit=args.limit,
                cursor=args.cursor,
                section=args.section,
            )
        else:
            payload = activity_confirmation_revision(
                conn,
                range_name=args.range_name,
                start=args.start,
                end=args.end,
            )
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
