from __future__ import annotations

import fcntl
import hashlib
import hmac
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


HOOK_SOURCE = Path(__file__).resolve().parents[1] / "tools" / "pam_ai_file_hook.py"


def _load_hook(home: Path):
    module_name = f"pam_ai_file_hook_under_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, HOOK_SOURCE)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError("unable to load hook source")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"HOME": str(home)}):
        spec.loader.exec_module(module)
    return module


class PamAiFileHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.hook = _load_hook(self.home)
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.hmac_secret = bytes(range(32))
        self.database_writer: sqlite3.Connection | None = None
        self._write_authorization_state(["root_" + "0" * 32])

    def tearDown(self) -> None:
        if self.database_writer is not None:
            self.database_writer.close()
        self.temporary.cleanup()

    def _write_authorization_state(
        self,
        root_ids: list[str],
        *,
        revision: int = 1,
        tracking: str = "true",
        pause_mode: str | None = None,
    ) -> None:
        if self.database_writer is not None:
            self.database_writer.close()
            self.database_writer = None
        self.hook.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if self.hook.HMAC_SECRET_PATH.exists() or self.hook.HMAC_SECRET_PATH.is_symlink():
            self.hook.HMAC_SECRET_PATH.unlink()
        self.hook.HMAC_SECRET_PATH.write_bytes(self.hmac_secret)
        self.hook.HMAC_SECRET_PATH.chmod(0o600)
        for path in (
            self.hook.DATABASE_PATH,
            self.hook.DATABASE_WAL_PATH,
            self.hook.DATABASE_SHM_PATH,
        ):
            if path.exists() or path.is_symlink():
                path.unlink()
        connection = sqlite3.connect(self.hook.DATABASE_PATH)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE daemon_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE file_watch_roots(root_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO settings(key,value) VALUES('document_change_tracking_enabled',?)",
            (tracking,),
        )
        connection.execute(
            "INSERT INTO daemon_state(key,value) "
            "VALUES('document_change_root_authorization_revision',?)",
            (str(revision),),
        )
        connection.execute(
            "INSERT INTO daemon_state(key,value) VALUES(?,?)",
            (
                self.hook.HMAC_VERIFIER_STATE_KEY,
                "v1:"
                + hmac.new(
                    self.hmac_secret,
                    self.hook.HMAC_VERIFIER_DOMAIN.encode("ascii"),
                    hashlib.sha256,
                ).hexdigest(),
            ),
        )
        if pause_mode is not None:
            connection.execute(
                "INSERT INTO daemon_state(key,value) VALUES('pause_mode',?)",
                (pause_mode,),
            )
        connection.executemany(
            "INSERT INTO file_watch_roots(root_id,enabled) VALUES(?,1)",
            [(root_id,) for root_id in root_ids],
        )
        connection.commit()
        self.database_writer = connection
        for path in (
            self.hook.DATABASE_PATH,
            self.hook.DATABASE_WAL_PATH,
            self.hook.DATABASE_SHM_PATH,
        ):
            path.chmod(0o600)
        self.hook.AUTHORIZATION_LOCK_PATH.write_bytes(b"")
        self.hook.AUTHORIZATION_LOCK_PATH.chmod(0o600)

    def _root_id_for(self, root: Path) -> str:
        return "root_" + hmac.new(
            self.hmac_secret,
            str(root.resolve()).encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()[:32]

    def _file_id_for(self, root: Path, path: Path) -> str:
        root_id = self._root_id_for(root)
        relative = path.resolve(strict=False).relative_to(root.resolve()).as_posix()
        return "file_" + hmac.new(
            self.hmac_secret,
            f"{root_id}:{relative}".encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()[:40]

    def _write_roots(self, *roots: Path) -> None:
        self.hook.ROOTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": 2,
            "authorization_revision": 1,
            "roots": [
                {
                    "root_id": self._root_id_for(root),
                    "root_ref": f"rootref_{'R' * 23}{index % 10}",
                    "root_label": f"Root {index}",
                    "path": str(root),
                }
                for index, root in enumerate(roots)
            ],
        }
        self.hook.ROOTS_JSON.write_text(json.dumps(payload), encoding="utf-8")
        self.hook.ROOTS_JSON.chmod(0o600)
        self._write_authorization_state(
            [str(item["root_id"]) for item in payload["roots"]],
            revision=int(payload["authorization_revision"]),
        )

    def _write_root_payload(self, payload: object) -> None:
        self.hook.ROOTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        self.hook.ROOTS_JSON.write_text(json.dumps(payload), encoding="utf-8")
        self.hook.ROOTS_JSON.chmod(0o600)

    def _run(self, payload: object, *, source: str = "codex") -> None:
        self.hook.main(
            ["--source", source],
            io.StringIO(json.dumps(payload, ensure_ascii=False)),
        )

    def _run_raw(self, raw: str) -> None:
        self.hook.main(["--source", "codex"], io.StringIO(raw))

    def _records(self) -> list[dict]:
        if not self.hook.LOG_PATH.exists():
            return []
        return [
            json.loads(line)
            for line in self.hook.LOG_PATH.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_claude_file_path_and_notebook_path_remain_compatible(self) -> None:
        self._write_roots(self.workspace)
        file_path = self.workspace / "notes" / "answer.md"
        notebook_path = self.workspace / "analysis.ipynb"
        relative_path = self.workspace / "relative.md"

        self._run(
            {
                "tool_name": "Write",
                "session_id": "claude-write",
                "tool_input": {"file_path": str(file_path)},
            },
            source="claude",
        )
        self._run(
            {
                "tool_name": "NotebookEdit",
                "session_id": "claude-notebook",
                "tool_input": {"notebook_path": str(notebook_path)},
            },
            source="claude",
        )
        self._run(
            {
                "cwd": str(self.workspace),
                "tool_name": "Write",
                "session_id": "claude-relative",
                "tool_input": {"file_path": "relative.md"},
            },
            source="claude",
        )

        records = self._records()
        self.assertEqual(
            [record["file_id"] for record in records],
            [
                self._file_id_for(self.workspace, file_path),
                self._file_id_for(self.workspace, notebook_path),
                self._file_id_for(self.workspace, relative_path),
            ],
        )
        self.assertEqual(
            [record["tool"] for record in records],
            ["claude", "claude", "claude"],
        )
        self.assertEqual(
            [record["action"] for record in records],
            ["Write", "NotebookEdit", "Write"],
        )
        self.assertEqual(
            [record["operation"] for record in records],
            ["write", "write", "write"],
        )

    def test_apply_patch_parses_file_headers_resolves_cwd_and_deduplicates(self) -> None:
        self._write_roots(self.workspace)
        secret_body = "PATCH_BODY_MUST_NOT_BE_LOGGED"
        command = f"""*** Begin Patch
*** Add File: docs/added.md
+{secret_body}
*** Update File: ./docs/added.md
@@
-old
+new
*** Update File: {self.workspace}/docs/added.md
@@
-old
+new
*** Update File: src/original.py
*** Move to: src/moved.py
@@
-old
+new
*** Delete File: docs/deleted.md
*** Update File: docs/added.md
@@
-old
+new
*** End Patch"""

        self.assertFalse((self.workspace / "docs/added.md").exists())
        self.assertFalse((self.workspace / "docs/deleted.md").exists())

        self._run(
            {
                "cwd": str(self.workspace),
                "tool_name": "apply_patch",
                "session_id": "codex-session",
                "tool_input": {"command": command},
            }
        )

        records = self._records()
        self.assertEqual(
            [record["file_id"] for record in records],
            [
                self._file_id_for(self.workspace, self.workspace / "docs/added.md"),
                self._file_id_for(self.workspace, self.workspace / "src/original.py"),
                self._file_id_for(self.workspace, self.workspace / "src/moved.py"),
                self._file_id_for(self.workspace, self.workspace / "docs/deleted.md"),
            ],
        )
        self.assertEqual(
            [record["operation"] for record in records],
            ["created", "modified", "renamed", "deleted"],
        )
        self.assertTrue(all(record["action"] == "apply_patch" for record in records))
        self.assertTrue(all(record["tool"] == "codex" for record in records))
        raw_log = self.hook.LOG_PATH.read_text(encoding="utf-8")
        self.assertNotIn(secret_body, raw_log)
        self.assertNotIn("***", raw_log)
        self.assertFalse((self.workspace / "docs/added.md").exists())
        self.assertFalse((self.workspace / "docs/deleted.md").exists())

    def test_only_exact_apply_patch_file_headers_are_parsed(self) -> None:
        self._write_roots(self.workspace)
        command = """*** Begin Patch
*** Add File: accepted.md
+*** Add File: body-content.md
 *** Update File: context-content.md
*** Rename File: unsupported.md
not-a-header *** Delete File: embedded.md
*** End Patch"""
        self._run(
            {
                "cwd": str(self.workspace),
                "tool_name": "apply_patch",
                "tool_input": {"command": command},
            }
        )
        self._run(
            {
                "cwd": str(self.workspace),
                "tool_name": "shell_command",
                "tool_input": {"command": "*** Add File: ignored.md"},
            }
        )

        self.assertEqual(
            [record["file_id"] for record in self._records()],
            [self._file_id_for(self.workspace, self.workspace / "accepted.md")],
        )

    def test_apply_patch_requires_one_closed_envelope(self) -> None:
        self._write_roots(self.workspace)
        commands = {
            "header-before-envelope": """*** Add File: outside.md
*** Begin Patch
*** Add File: inside.md
*** End Patch""",
            "header-after-envelope": """*** Begin Patch
*** Add File: inside.md
*** End Patch
*** Delete File: outside.md""",
            "multiple-envelopes": """*** Begin Patch
*** Add File: first.md
*** End Patch
*** Begin Patch
*** Add File: second.md
*** End Patch""",
            "missing-begin": """*** Add File: missing-begin.md
*** End Patch""",
            "missing-end": """*** Begin Patch
*** Add File: missing-end.md""",
        }
        for name, command in commands.items():
            with self.subTest(name=name):
                self._run(
                    {
                        "cwd": str(self.workspace),
                        "tool_name": "apply_patch",
                        "tool_input": {"command": command},
                    }
                )
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_missing_malformed_empty_or_invalid_roots_fail_closed(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "private.md")},
        }
        cases: list[tuple[str, str | None]] = [
            ("missing", None),
            ("malformed", "{not-json"),
            ("empty", json.dumps({"roots": []})),
            ("invalid", json.dumps({"roots": [{"path": 7}]})),
            (
                "parent-reference",
                json.dumps({"roots": [{"path": f"{self.workspace}/nested/.."}]}),
            ),
        ]
        for name, root_payload in cases:
            with self.subTest(name=name):
                if self.hook.LOG_PATH.exists():
                    self.hook.LOG_PATH.unlink()
                if self.hook.ROOTS_JSON.exists():
                    self.hook.ROOTS_JSON.unlink()
                if root_payload is not None:
                    self.hook.ROOTS_JSON.parent.mkdir(parents=True, exist_ok=True)
                    self.hook.ROOTS_JSON.write_text(root_payload, encoding="utf-8")
                    self.hook.ROOTS_JSON.chmod(0o600)
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_unsafe_root_registry_files_fail_closed(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "private.md")},
        }
        self._write_roots(self.workspace)
        valid_payload = self.hook.ROOTS_JSON.read_bytes()

        self.hook.ROOTS_JSON.chmod(0o644)
        self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

        self.hook.ROOTS_JSON.unlink()
        target = self.home / "root-registry-target.json"
        target.write_bytes(valid_payload)
        target.chmod(0o600)
        self.hook.ROOTS_JSON.symlink_to(target)
        self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

        self.hook.ROOTS_JSON.unlink()
        self.hook.ROOTS_JSON.write_bytes(b" " * (self.hook.ROOTS_MAX_BYTES + 1))
        self.hook.ROOTS_JSON.chmod(0o600)
        self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_authorized_root_replaced_by_symlink_fails_closed(self) -> None:
        self._write_roots(self.workspace)
        original_root = self.home / "original-workspace"
        outside = self.home / "outside"
        outside.mkdir()
        self.workspace.rename(original_root)
        self.workspace.symlink_to(outside, target_is_directory=True)

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "escaped.md")},
            },
            source="claude",
        )

        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_valid_hmac_bound_root_records(self) -> None:
        self._write_roots(self.workspace)

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "bound.md")},
            },
            source="claude",
        )

        self.assertEqual(len(self._records()), 1)

    def test_sidecar_path_cannot_reuse_another_roots_hmac_identity(self) -> None:
        self._write_roots(self.workspace)
        substituted_root = self.home / "substituted-root"
        substituted_root.mkdir()
        payload = json.loads(self.hook.ROOTS_JSON.read_text(encoding="utf-8"))
        payload["roots"][0]["path"] = str(substituted_root)
        self._write_root_payload(payload)

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(substituted_root / "not-authorized.md")},
            },
            source="claude",
        )

        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_swapped_root_id_bindings_fail_even_when_id_set_matches(self) -> None:
        second_root = self.home / "second-workspace"
        second_root.mkdir()
        self._write_roots(self.workspace, second_root)
        payload = json.loads(self.hook.ROOTS_JSON.read_text(encoding="utf-8"))
        payload["roots"][0]["root_id"], payload["roots"][1]["root_id"] = (
            payload["roots"][1]["root_id"],
            payload["roots"][0]["root_id"],
        )
        self._write_root_payload(payload)

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "swapped.md")},
            },
            source="claude",
        )

        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_root_registry_must_match_the_core_v2_contract(self) -> None:
        self._write_roots(self.workspace)
        valid = json.loads(self.hook.ROOTS_JSON.read_text(encoding="utf-8"))
        valid_item = valid["roots"][0]
        duplicate = dict(valid_item)
        duplicate["path"] = str(self.home / "other-root")
        cases = {
            "entries-fallback": {"entries": valid["roots"]},
            "missing-key": {"format_version": 2, "roots": valid["roots"]},
            "extra-key": {**valid, "extra": True},
            "wrong-version": {**valid, "format_version": 1},
            "float-version": {**valid, "format_version": 2.0},
            "bool-revision": {**valid, "authorization_revision": True},
            "negative-revision": {**valid, "authorization_revision": -1},
            "bad-root-id": {
                **valid,
                "roots": [{**valid_item, "root_id": "root_invalid"}],
            },
            "bad-root-ref": {
                **valid,
                "roots": [{**valid_item, "root_ref": "ref_invalid"}],
            },
            "bad-root-label": {
                **valid,
                "roots": [{**valid_item, "root_label": 7}],
            },
            "unnormalized-path": {
                **valid,
                "roots": [{**valid_item, "path": f"{self.workspace}/nested/.."}],
            },
            "duplicate-root-id": {**valid, "roots": [valid_item, duplicate]},
        }
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "private.md")},
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                if self.hook.LOG_PATH.exists():
                    self.hook.LOG_PATH.unlink()
                self._write_root_payload(payload)
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

        optional_ref_item = {
            key: value for key, value in valid_item.items() if key != "root_ref"
        }
        self._write_root_payload({**valid, "roots": [optional_ref_item]})
        self._run(event, source="claude")
        self.assertEqual(len(self._records()), 1)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX mkfifo")
    def test_fifo_root_registry_fails_quickly(self) -> None:
        self.hook.ROOTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.hook.ROOTS_JSON, 0o600)
        started = time.monotonic()
        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "private.md")},
            },
            source="claude",
        )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_uncheckpointed_wal_tracking_disable_fails_closed(self) -> None:
        self._write_roots(self.workspace)
        assert self.database_writer is not None
        self.database_writer.execute(
            "UPDATE settings SET value='false' "
            "WHERE key='document_change_tracking_enabled'"
        )
        self.database_writer.commit()
        self.assertGreater(self.hook.DATABASE_WAL_PATH.stat().st_size, 0)

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "disabled.md")},
            },
            source="claude",
        )
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_manual_and_unknown_pause_modes_fail_closed(self) -> None:
        self._write_roots(self.workspace)
        assert self.database_writer is not None
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "paused.md")},
        }
        for pause_mode in ("manual", "until_lock", "today", "duration", "unexpected"):
            with self.subTest(pause_mode=pause_mode):
                self.database_writer.execute(
                    "INSERT INTO daemon_state(key,value) VALUES('pause_mode',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (pause_mode,),
                )
                self.database_writer.commit()
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_nonempty_pause_until_fails_closed_even_with_empty_mode(self) -> None:
        self._write_roots(self.workspace)
        assert self.database_writer is not None
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "pause-until.md")},
        }
        self.database_writer.execute(
            "INSERT INTO daemon_state(key,value) VALUES('pause_mode','')"
        )
        for pause_until in ("2099-01-01T00:00:00+00:00", "not-a-time"):
            with self.subTest(pause_until=pause_until):
                self.database_writer.execute(
                    "INSERT INTO daemon_state(key,value) VALUES('pause_until',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (pause_until,),
                )
                self.database_writer.commit()
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_database_revision_and_enabled_roots_must_match_sidecar(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "mismatch.md")},
        }
        self._write_roots(self.workspace)
        assert self.database_writer is not None
        self.database_writer.execute(
            "UPDATE daemon_state SET value='2' "
            "WHERE key='document_change_root_authorization_revision'"
        )
        self.database_writer.commit()
        self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

        self._write_roots(self.workspace)
        assert self.database_writer is not None
        self.database_writer.execute("UPDATE file_watch_roots SET enabled=0")
        self.database_writer.commit()
        self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_missing_unsafe_or_replaced_hmac_secret_fails_closed(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "secret-gate.md")},
        }
        cases = ("missing", "unsafe-mode", "short", "long", "replaced", "symlink")
        for case in cases:
            with self.subTest(case=case):
                if self.hook.LOG_PATH.exists():
                    self.hook.LOG_PATH.unlink()
                self._write_roots(self.workspace)
                secret_path = self.hook.HMAC_SECRET_PATH
                if case == "missing":
                    secret_path.unlink()
                elif case == "unsafe-mode":
                    secret_path.chmod(0o644)
                elif case == "short":
                    secret_path.write_bytes(self.hmac_secret[:-1])
                    secret_path.chmod(0o600)
                elif case == "long":
                    secret_path.write_bytes(self.hmac_secret + b"x")
                    secret_path.chmod(0o600)
                elif case == "replaced":
                    secret_path.write_bytes(bytes(reversed(self.hmac_secret)))
                    secret_path.chmod(0o600)
                else:
                    secret_path.unlink()
                    target = self.home / "secret-symlink-target"
                    target.write_bytes(self.hmac_secret)
                    target.chmod(0o600)
                    secret_path.symlink_to(target)
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX mkfifo")
    def test_fifo_hmac_secret_fails_quickly(self) -> None:
        self._write_roots(self.workspace)
        self.hook.HMAC_SECRET_PATH.unlink()
        os.mkfifo(self.hook.HMAC_SECRET_PATH, 0o600)

        started = time.monotonic()
        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "fifo-secret.md")},
            },
            source="claude",
        )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_missing_blank_wrong_or_wal_only_hmac_verifier_fails_closed(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "verifier-gate.md")},
        }
        for case in ("missing", "blank", "malformed", "wrong"):
            with self.subTest(case=case):
                if self.hook.LOG_PATH.exists():
                    self.hook.LOG_PATH.unlink()
                self._write_roots(self.workspace)
                assert self.database_writer is not None
                if case == "missing":
                    self.database_writer.execute(
                        "DELETE FROM daemon_state WHERE key=?",
                        (self.hook.HMAC_VERIFIER_STATE_KEY,),
                    )
                else:
                    values = {
                        "blank": "",
                        "malformed": "not-a-verifier",
                        "wrong": "v1:" + "0" * 64,
                    }
                    self.database_writer.execute(
                        "UPDATE daemon_state SET value=? WHERE key=?",
                        (values[case], self.hook.HMAC_VERIFIER_STATE_KEY),
                    )
                self.database_writer.commit()
                self.assertGreater(self.hook.DATABASE_WAL_PATH.stat().st_size, 0)
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_legacy_hmac_key_row_is_migration_pending_and_fails_closed(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "legacy-key.md")},
        }
        for legacy_value in (
            self.hmac_secret.hex(),
            bytes(reversed(self.hmac_secret)).hex(),
            "not-hex",
        ):
            with self.subTest(legacy_value=legacy_value[:8]):
                if self.hook.LOG_PATH.exists():
                    self.hook.LOG_PATH.unlink()
                self._write_roots(self.workspace)
                assert self.database_writer is not None
                self.database_writer.execute(
                    "INSERT INTO daemon_state(key,value) VALUES(?,?)",
                    (self.hook.LEGACY_HMAC_STATE_KEY, legacy_value),
                )
                self.database_writer.commit()
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_hmac_binding_contract_matches_core(self) -> None:
        core_root = str(HOOK_SOURCE.parents[1])
        with mock.patch.object(sys, "path", [core_root, *sys.path]):
            from activity_mvp import document_changes as core_document_changes

        resolved = self.workspace.resolve()
        self.assertEqual(
            self.hook.HMAC_VERIFIER_DOMAIN,
            core_document_changes.HMAC_VERIFIER_DOMAIN,
        )
        self.assertEqual(
            self.hook._hmac_verifier(self.hmac_secret),
            core_document_changes._hmac_verifier(self.hmac_secret),
        )
        self.assertEqual(
            "root_" + self.hook._hmac_value(self.hmac_secret, str(resolved))[:32],
            "root_"
            + core_document_changes._hmac(self.hmac_secret, str(resolved))[:32],
        )

    def test_busy_authorization_lock_fails_closed(self) -> None:
        self._write_roots(self.workspace)
        descriptor = os.open(self.hook.AUTHORIZATION_LOCK_PATH, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(self.workspace / "locked.md")},
                },
                source="claude",
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_authorization_lock_is_held_through_log_append(self) -> None:
        self._write_roots(self.workspace)
        original_append = self.hook._append_records
        observed_busy: list[bool] = []

        def append_while_probing(records):
            descriptor = os.open(self.hook.AUTHORIZATION_LOCK_PATH, os.O_RDONLY)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    observed_busy.append(True)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    observed_busy.append(False)
            finally:
                os.close(descriptor)
            return original_append(records)

        with mock.patch.object(self.hook, "_append_records", side_effect=append_while_probing):
            self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(self.workspace / "held.md")},
                },
                source="claude",
            )
        self.assertEqual(observed_busy, [True])
        self.assertEqual(len(self._records()), 1)

    def test_missing_or_unsafe_database_family_fails_closed_without_creation(self) -> None:
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "database-gate.md")},
        }
        self._write_roots(self.workspace)
        assert self.database_writer is not None
        self.database_writer.close()
        self.database_writer = None
        self.hook.DATABASE_PATH.unlink()
        self._run(event, source="claude")
        self.assertFalse(self.hook.DATABASE_PATH.exists())
        self.assertFalse(self.hook.LOG_PATH.exists())

        for path_name in ("DATABASE_WAL_PATH", "DATABASE_SHM_PATH"):
            with self.subTest(path=path_name):
                self._write_roots(self.workspace)
                path = getattr(self.hook, path_name)
                path.unlink()
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

        for path_name in (
            "DATABASE_PATH",
            "DATABASE_WAL_PATH",
            "DATABASE_SHM_PATH",
            "AUTHORIZATION_LOCK_PATH",
        ):
            with self.subTest(unsafe_mode=path_name):
                self._write_roots(self.workspace)
                path = getattr(self.hook, path_name)
                path.chmod(0o644)
                self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_database_busy_exception_and_missing_schema_fail_closed(self) -> None:
        self._write_roots(self.workspace)
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "db-error.md")},
        }
        for error in (
            sqlite3.OperationalError("database is locked"),
            RuntimeError("unexpected database failure"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.hook.sqlite3, "connect", side_effect=error):
                    self._run(event, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())

        assert self.database_writer is not None
        self.database_writer.execute("DROP TABLE settings")
        self.database_writer.commit()
        self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_outside_parent_traversal_and_symlink_escape_are_rejected(self) -> None:
        self._write_roots(self.workspace)
        outside = self.home / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        command = f"""*** Begin Patch
*** Add File: safe.md
*** Add File: ../outside/relative-traversal.md
*** Add File: {self.workspace}/nested/../absolute-traversal.md
*** Add File: escape/symlink-escape.md
*** Add File: {outside}/outside.md
*** End Patch"""

        self._run(
            {
                "cwd": str(self.workspace),
                "tool_name": "apply_patch",
                "tool_input": {"command": command},
            }
        )

        self.assertEqual(
            [record["file_id"] for record in self._records()],
            [self._file_id_for(self.workspace, self.workspace / "safe.md")],
        )

    def test_relative_path_requires_an_absolute_safe_cwd(self) -> None:
        self._write_roots(self.workspace)
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": """*** Begin Patch
*** Add File: relative.md
*** End Patch"""
            },
        }
        for cwd in (None, "relative/cwd", f"{self.workspace}/nested/.."):
            with self.subTest(cwd=cwd):
                if self.hook.LOG_PATH.exists():
                    self.hook.LOG_PATH.unlink()
                event = dict(payload)
                if cwd is not None:
                    event["cwd"] = cwd
                self._run(event)
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_bad_empty_and_non_object_inputs_do_not_raise_or_write(self) -> None:
        self._write_roots(self.workspace)
        for name, raw in (
            ("empty", ""),
            ("bad-json", "{not-json"),
            ("list", "[]"),
            ("string", '"payload"'),
            ("null", "null"),
            ("non-object-tool-input", '{"tool_name":"Write","tool_input":[1]}'),
        ):
            with self.subTest(name=name):
                self._run_raw(raw)
                self.assertFalse(self.hook.LOG_PATH.exists())

    def test_input_candidate_path_and_batch_limits_fail_closed(self) -> None:
        self._write_roots(self.workspace)
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "private.md")},
        }
        raw_event = json.dumps(event)
        with mock.patch.object(self.hook, "STDIN_MAX_BYTES", len(raw_event.encode("utf-8")) - 1):
            self._run_raw(raw_event)
        self.assertFalse(self.hook.LOG_PATH.exists())

        command = """*** Begin Patch
*** Add File: one.md
*** Add File: two.md
*** Add File: three.md
*** End Patch"""
        with mock.patch.object(self.hook, "MAX_CANDIDATE_PATHS", 2):
            self._run(
                {
                    "cwd": str(self.workspace),
                    "tool_name": "apply_patch",
                    "tool_input": {"command": command},
                }
            )
        self.assertFalse(self.hook.LOG_PATH.exists())

        path_limit = len(str(self.workspace).encode("utf-8")) + 8
        with mock.patch.object(self.hook, "MAX_PATH_BYTES", path_limit):
            self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {
                        "file_path": str(self.workspace / ("x" * (path_limit + 1)))
                    },
                },
                source="claude",
            )
        self.assertFalse(self.hook.LOG_PATH.exists())

        with mock.patch.object(self.hook, "MAX_BATCH_BYTES", 1):
            self._run(event, source="claude")
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_log_identity_fields_reject_non_strings_unsafe_chars_and_overflow(self) -> None:
        self._write_roots(self.workspace)
        target = str(self.workspace / "private.md")
        payloads = {
            "dict-tool": {"tool_name": {"name": "Write"}, "tool_input": {"file_path": target}},
            "dict-session": {
                "tool_name": "Write",
                "session_id": {"id": "session"},
                "tool_input": {"file_path": target},
            },
            "unsafe-tool": {"tool_name": "Write Tool", "tool_input": {"file_path": target}},
            "unsafe-session": {
                "tool_name": "Write",
                "session_id": "session\nleak",
                "tool_input": {"file_path": target},
            },
            "long-tool": {
                "tool_name": "W" * (self.hook.MAX_ACTION_BYTES + 1),
                "tool_input": {"file_path": target},
            },
            "long-session": {
                "tool_name": "Write",
                "session_id": "s" * (self.hook.MAX_SESSION_BYTES + 1),
                "tool_input": {"file_path": target},
            },
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                self._run(payload, source="claude")
                self.assertFalse(self.hook.LOG_PATH.exists())
        self.hook.main(
            ["--source", "bad source"],
            io.StringIO(
                json.dumps(
                    {"tool_name": "Write", "tool_input": {"file_path": target}}
                )
            ),
        )
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_log_has_only_safe_identity_fields_and_enforces_private_modes(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o777)
        self.hook.LOG_DIR.chmod(0o777)
        self.hook.LOG_PATH.write_text("", encoding="utf-8")
        self.hook.LOG_PATH.chmod(0o666)
        target = self.workspace / "private.md"

        self._run(
            {
                "tool_name": "Write",
                "session_id": "privacy-test",
                "tool_input": {"file_path": str(target), "content": "DO_NOT_LOG_CONTENT"},
                "prompt": "DO_NOT_LOG_PROMPT",
            },
            source="claude",
        )

        record = self._records()[0]
        self.assertEqual(
            set(record),
            {"at", "tool", "action", "operation", "root_id", "file_id"},
        )
        raw_log = self.hook.LOG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("DO_NOT_LOG_CONTENT", raw_log)
        self.assertNotIn("DO_NOT_LOG_PROMPT", raw_log)
        self.assertNotIn(str(target), raw_log)
        self.assertNotIn("privacy-test", raw_log)
        self.assertEqual(stat.S_IMODE(self.hook.LOG_DIR.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.hook.LOG_PATH.stat().st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX mkfifo")
    def test_fifo_log_target_fails_quickly_without_writing(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o700)
        os.mkfifo(self.hook.LOG_PATH, 0o600)
        started = time.monotonic()
        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "private.md")},
            },
            source="claude",
        )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(stat.S_ISFIFO(self.hook.LOG_PATH.lstat().st_mode))

    def test_symlinked_log_targets_fail_closed_without_touching_targets(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o700)
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.workspace / "private.md")},
        }
        for path in (self.hook.LOG_PATH, self.hook.ROTATED_PATH):
            with self.subTest(path=path.name):
                for candidate in (self.hook.LOG_PATH, self.hook.ROTATED_PATH):
                    if candidate.is_symlink() or candidate.exists():
                        candidate.unlink()
                target = self.home / f"{path.name}-target"
                target.write_text("outside-canary\n", encoding="utf-8")
                path.symlink_to(target)
                self._run(event, source="claude")
                self.assertEqual(target.read_text(encoding="utf-8"), "outside-canary\n")
                self.assertTrue(path.is_symlink())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX mkfifo")
    def test_fifo_rotated_target_fails_quickly_without_replacement(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o700)
        os.mkfifo(self.hook.ROTATED_PATH, 0o600)
        started = time.monotonic()
        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "private.md")},
            },
            source="claude",
        )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(stat.S_ISFIFO(self.hook.ROTATED_PATH.lstat().st_mode))
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_predicted_overflow_rotates_before_append(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o700)
        old_log = "x" * 1000
        self.hook.LOG_PATH.write_text(old_log, encoding="utf-8")
        self.hook.LOG_PATH.chmod(0o600)
        self.hook.MAX_BYTES = 1024

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "overflow.md")},
            },
            source="claude",
        )

        self.assertEqual(self.hook.ROTATED_PATH.read_text(encoding="utf-8"), old_log)
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertLessEqual(self.hook.LOG_PATH.stat().st_size, self.hook.MAX_BYTES)

    def test_rotation_failure_refuses_to_append(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o700)
        old_log = "x" * 1024
        self.hook.LOG_PATH.write_text(old_log, encoding="utf-8")
        self.hook.LOG_PATH.chmod(0o600)
        self.hook.MAX_BYTES = len(old_log)

        with mock.patch.object(self.hook.os, "replace", side_effect=OSError("blocked")):
            self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(self.workspace / "blocked.md")},
                },
                source="claude",
            )

        self.assertEqual(self.hook.LOG_PATH.read_text(encoding="utf-8"), old_log)
        self.assertFalse(self.hook.ROTATED_PATH.exists())

    def test_single_batch_larger_than_file_cap_writes_nothing(self) -> None:
        self._write_roots(self.workspace)
        self.hook.MAX_BYTES = 32
        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "too-large.md")},
            },
            source="claude",
        )
        self.assertFalse(self.hook.LOG_PATH.exists())

    def test_logging_uses_descriptor_chmod_not_path_chmod(self) -> None:
        self._write_roots(self.workspace)
        with mock.patch.object(
            self.hook.os,
            "chmod",
            side_effect=AssertionError("path chmod must not be used"),
        ):
            self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(self.workspace / "fd-only.md")},
                },
                source="claude",
            )
        self.assertEqual(len(self._records()), 1)

    def test_rotation_remains_single_generation_and_happens_before_append(self) -> None:
        self._write_roots(self.workspace)
        self.hook.LOG_DIR.mkdir(mode=0o700)
        old_log = "x" * 1024
        self.hook.LOG_PATH.write_text(old_log, encoding="utf-8")
        self.hook.LOG_PATH.chmod(0o666)
        self.hook.ROTATED_PATH.write_text("stale-generation\n", encoding="utf-8")
        self.hook.MAX_BYTES = len(old_log.encode("utf-8"))

        self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "after-rotation.md")},
            },
            source="claude",
        )

        self.assertEqual(self.hook.ROTATED_PATH.read_text(encoding="utf-8"), old_log)
        self.assertEqual(len(self._records()), 1)
        self.assertEqual(
            self._records()[0]["file_id"],
            self._file_id_for(self.workspace, self.workspace / "after-rotation.md"),
        )
        self.assertEqual(stat.S_IMODE(self.hook.ROTATED_PATH.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.hook.LOG_PATH.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
