from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CORE_ROOT = Path(__file__).resolve().parents[1]
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from activity_mvp.app_identity import (
    deterministic_legacy_identity_collision,
    resolve_app_identity,
    same_app_identity,
)
from activity_mvp.browser_signals import _browser_matches
from activity_mvp.document_signals import _document_mtime_state
from activity_mvp.identity_collision import (
    IDENTITY_COLLISION_MATCH_STATUS,
    PRIVATE_MANIFEST_SETTING_PREFIX,
    apply_document_signal_identity_collisions,
    preview_document_signal_identity_collisions,
    reverse_document_signal_identity_collisions,
)
from activity_mvp.macos_probe import (
    _browser_signal_matches_event,
    _browser_url,
    _current_document_path,
    _is_browser_context,
)
from activity_mvp.screen_regions import REGION_BROWSER_TAB, classify_screen_region
from activity_mvp.storage import SCHEMA_VERSION, init_schema
from activity_mvp.timeline_export import (
    _is_external_document_context,
    _is_primary_action_app,
    _is_trusted_memory_app,
    _short_content_buckets,
)


HISTORY_CUTOFF = "2026-08-23T00:00:00+00:00"


class V051AppIdentityGuardTests(unittest.TestCase):
    def test_bundle_first_exact_alias_and_conflict_contract(self) -> None:
        obsidian = resolve_app_identity("Obsidian", "md.obsidian")
        self.assertTrue(obsidian.matched)
        self.assertEqual(obsidian.family, "obsidian")

        conflict = resolve_app_identity("Dia", "md.obsidian")
        self.assertFalse(conflict.matched)
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(conflict.reason, "bundle_name_conflict")

        unknown_bundle = resolve_app_identity("Google Chrome", "example.unknown.chrome")
        self.assertFalse(unknown_bundle.matched)
        self.assertEqual(unknown_bundle.reason, "unknown_bundle")

        name_only = resolve_app_identity("  Google   Chrome  ", "")
        self.assertTrue(name_only.matched)
        self.assertEqual(name_only.family, "chrome")

    def test_obsidian_never_matches_dia_browser_routes_or_signals(self) -> None:
        dia_signal = {"browser": "Dia", "bundle_id": "company.thebrowser.dia"}
        obsidian_event = {"app_name": "Obsidian", "bundle_id": "md.obsidian"}
        with patch("activity_mvp.macos_probe._osascript") as osascript:
            result = _browser_url("Obsidian", "md.obsidian")
        self.assertEqual(result["source"], "not_browser")
        osascript.assert_not_called()
        self.assertFalse(_is_browser_context("Obsidian", "md.obsidian"))
        self.assertFalse(_browser_signal_matches_event(dia_signal, obsidian_event))
        self.assertFalse(_browser_matches(dia_signal, "Obsidian", "md.obsidian"))
        self.assertFalse(
            same_app_identity(
                "Obsidian",
                "md.obsidian",
                "Dia",
                "company.thebrowser.dia",
            )
        )

    def test_knowledge_never_matches_edge_by_substring(self) -> None:
        edge_signal = {"browser": "Microsoft Edge", "bundle_id": "com.microsoft.edgemac"}
        knowledge_event = {"app_name": "Knowledge", "bundle_id": "example.knowledge"}
        self.assertFalse(_is_browser_context("Knowledge", "example.knowledge"))
        self.assertFalse(_browser_signal_matches_event(edge_signal, knowledge_event))
        self.assertFalse(_browser_matches(edge_signal, "Knowledge", "example.knowledge"))
        self.assertTrue(deterministic_legacy_identity_collision("Knowledge", "example.knowledge"))

    def test_1password_never_routes_to_word_or_document_work_priors(self) -> None:
        frontmost = {
            "app_name": "1Password",
            "bundle_id": "com.1password.1password",
        }
        with patch("activity_mvp.macos_probe._osascript") as osascript:
            self.assertEqual(_current_document_path(frontmost, {}), "")
        osascript.assert_not_called()

        stat_result = SimpleNamespace(st_mtime=1_781_430_400.0)
        state = _document_mtime_state(
            {
                **frontmost,
                "captured_at": "2026-06-15T00:00:00+00:00",
            },
            stat_result,
            "foreground_app_or_file_url",
        )
        self.assertEqual(state, "viewed_without_recent_modify")
        self.assertFalse(_is_trusted_memory_app("1Password"))
        self.assertFalse(_is_primary_action_app("1Password"))
        self.assertFalse(_is_external_document_context("1Password", ""))

    def test_screen_region_app_prior_uses_resolved_identity(self) -> None:
        obsidian_signal = classify_screen_region(
            {
                "app_name": "Obsidian",
                "bundle_id": "md.obsidian",
                "window_title": "new tab",
                "ocr_text": "tab tab tab",
            }
        )
        knowledge_signal = classify_screen_region(
            {
                "app_name": "Knowledge",
                "bundle_id": "example.knowledge",
                "window_title": "new tab",
                "ocr_text": "tab tab tab",
            }
        )
        self.assertNotEqual(obsidian_signal["region_kind"], REGION_BROWSER_TAB)
        self.assertNotEqual(knowledge_signal["region_kind"], REGION_BROWSER_TAB)

    def test_short_content_diagnostics_do_not_treat_obsidian_as_dia(self) -> None:
        buckets = _short_content_buckets(
            [
                {
                    "apps": ["Obsidian"],
                    "cleaned_text": "short",
                    "activity_summary": "short",
                    "privacy_level": "public",
                    "dedupe_status": "unique",
                }
            ]
        )
        self.assertEqual(buckets["browser_or_system_chrome_count"], 0)
        self.assertEqual(buckets["true_low_signal_count"], 1)


class V051DocumentSignalCollisionGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn)
        self._insert_collision("doc_obsidian", "Obsidian", "md.obsidian", semantic_match=1, match_status="matched_text")
        self._insert_collision("doc_1password", "1Password", "com.1password.1password", semantic_match=0, match_status="metadata_only")
        self._insert_collision("doc_dia", "Dia", "company.thebrowser.dia", semantic_match=1, match_status="matched_text")
        self._insert_collision("doc_knowledge", "Knowledge", "com.microsoft.edgemac", semantic_match=1, match_status="matched_text")
        self.conn.execute(
            "INSERT INTO file_change_versions("
            "id,aggregate_event_id,file_id,root_id,change_kind,changed_at,actor_source,deletion_group_id,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "version_unattributed",
                "aggregate_1",
                "file_1",
                "root_1",
                "modified",
                "2026-08-22T10:00:00+00:00",
                "unattributed",
                "file_group_1",
                "2026-08-22T10:00:00+00:00",
                "2026-08-22T10:00:00+00:00",
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_collision(
        self,
        signal_id: str,
        app_name: str,
        bundle_id: str,
        *,
        semantic_match: int,
        match_status: str,
        captured_at: str = "2026-08-22T09:00:00+00:00",
    ) -> None:
        suffix = signal_id[4:] if signal_id.startswith("doc_") else signal_id
        activity_id = f"activity_{suffix}"
        frame_id = f"frame_{suffix}"
        group_id = f"group_{suffix}"
        self.conn.execute(
            "INSERT INTO extracted_activities("
            "id,session_id,started_at,activity_summary,apps,quarantine_status,deletion_group_id"
            ") VALUES(?,?,?,?,?,?,?)",
            (activity_id, "session_1", captured_at, "safe synthetic summary", json.dumps([app_name]), "clear", group_id),
        )
        self.conn.execute(
            "INSERT INTO frame_signals("
            "id,session_id,captured_at,app_name,bundle_id,deletion_group_id"
            ") VALUES(?,?,?,?,?,?)",
            (frame_id, "session_1", captured_at, app_name, bundle_id, group_id),
        )
        self.conn.execute(
            "INSERT INTO document_signals("
            "id,session_id,captured_at,app_name,semantic_match,match_status,source,deletion_group_id"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                signal_id,
                "session_1",
                captured_at,
                app_name,
                semantic_match,
                match_status,
                "foreground_app_or_file_url",
                group_id,
            ),
        )
        for source_type, source_id in (("document_signal", signal_id), ("frame_signal", frame_id)):
            self.conn.execute(
                "INSERT INTO activity_provenance("
                "id,derived_type,derived_id,source_type,source_id,deletion_group_id,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    f"prov_{suffix}_{source_type}",
                    "extracted_activity",
                    activity_id,
                    source_type,
                    source_id,
                    group_id,
                    captured_at,
                ),
            )

    def test_preview_apply_reverse_is_content_free_reversible_and_schema_stable(self) -> None:
        columns_before = [row["name"] for row in self.conn.execute("PRAGMA table_info(document_signals)")]
        total_before = self.conn.execute("SELECT COUNT(*) FROM document_signals").fetchone()[0]
        attribution_before = self.conn.execute(
            "SELECT actor_source FROM file_change_versions WHERE id='version_unattributed'"
        ).fetchone()[0]

        preview = preview_document_signal_identity_collisions(
            self.conn,
            historical_cutoff=HISTORY_CUTOFF,
        )
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["candidate_count"], 3)
        self.assertEqual(preview["updated_count"], 0)
        self.assertEqual(len(preview["manifest_hash"]), 64)
        encoded_preview = json.dumps(preview, ensure_ascii=False)
        for forbidden in ("doc_obsidian", "Obsidian", "1Password", "Knowledge", "/Users/", "safe synthetic summary"):
            self.assertNotIn(forbidden, encoded_preview)

        applied = apply_document_signal_identity_collisions(
            self.conn,
            confirm_hash=preview["manifest_hash"],
            historical_cutoff=preview["historical_cutoff"],
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["updated_count"], 3)
        invalidated = self.conn.execute(
            "SELECT id,semantic_match,match_status FROM document_signals "
            "WHERE match_status=? ORDER BY id",
            (IDENTITY_COLLISION_MATCH_STATUS,),
        ).fetchall()
        self.assertEqual(
            [row["id"] for row in invalidated],
            ["doc_1password", "doc_knowledge", "doc_obsidian"],
        )
        self.assertTrue(all(row["semantic_match"] == 0 for row in invalidated))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM document_signals").fetchone()[0], total_before)
        self.assertEqual(
            self.conn.execute("SELECT actor_source FROM file_change_versions WHERE id='version_unattributed'").fetchone()[0],
            attribution_before,
        )
        manifest_row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (f"{PRIVATE_MANIFEST_SETTING_PREFIX}{preview['manifest_hash']}",),
        ).fetchone()
        self.assertIsNotNone(manifest_row)
        manifest = json.loads(manifest_row["value"])
        self.assertEqual(manifest["payload"]["entries"][0]["id"], "doc_1password")
        self.assertEqual(manifest["payload"]["entries"][0]["match_status"], "metadata_only")

        reversed_result = reverse_document_signal_identity_collisions(
            self.conn,
            manifest_hash=preview["manifest_hash"],
        )
        self.assertEqual(reversed_result["status"], "reversed")
        self.assertEqual(reversed_result["restored_count"], 3)
        originals = {
            row["id"]: (row["semantic_match"], row["match_status"])
            for row in self.conn.execute(
                "SELECT id,semantic_match,match_status FROM document_signals "
                "WHERE id IN ('doc_obsidian','doc_1password','doc_knowledge')"
            )
        }
        self.assertEqual(originals["doc_obsidian"], (1, "matched_text"))
        self.assertEqual(originals["doc_1password"], (0, "metadata_only"))
        self.assertEqual(originals["doc_knowledge"], (1, "matched_text"))
        self.assertEqual([row["name"] for row in self.conn.execute("PRAGMA table_info(document_signals)")], columns_before)
        self.assertEqual(
            int(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]),
            SCHEMA_VERSION,
        )

    def test_apply_rechecks_preview_hash_and_rolls_back_on_drift(self) -> None:
        preview = preview_document_signal_identity_collisions(
            self.conn,
            historical_cutoff=HISTORY_CUTOFF,
        )
        self.conn.execute(
            "UPDATE document_signals SET match_status='reviewed_after_preview' WHERE id='doc_obsidian'"
        )
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "preview_hash_mismatch"):
            apply_document_signal_identity_collisions(
                self.conn,
                confirm_hash=preview["manifest_hash"],
                historical_cutoff=preview["historical_cutoff"],
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT match_status FROM document_signals WHERE id='doc_1password'"
            ).fetchone()[0],
            "metadata_only",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM settings WHERE key LIKE ?",
                (f"{PRIVATE_MANIFEST_SETTING_PREFIX}%",),
            ).fetchone()[0],
            0,
        )

    def test_preview_excludes_signals_after_the_release_history_cutoff(self) -> None:
        self._insert_collision(
            "doc_future_obsidian",
            "Obsidian",
            "md.obsidian",
            semantic_match=1,
            match_status="matched_text",
            captured_at="2026-08-24T09:00:00+00:00",
        )
        self.conn.commit()

        preview = preview_document_signal_identity_collisions(
            self.conn,
            historical_cutoff=HISTORY_CUTOFF,
        )

        self.assertEqual(preview["candidate_count"], 3)

    def test_reverse_fails_closed_if_an_invalidated_row_changed(self) -> None:
        preview = preview_document_signal_identity_collisions(
            self.conn,
            historical_cutoff=HISTORY_CUTOFF,
        )
        apply_document_signal_identity_collisions(
            self.conn,
            confirm_hash=preview["manifest_hash"],
            historical_cutoff=preview["historical_cutoff"],
        )
        self.conn.execute(
            "UPDATE document_signals SET match_status='manually_reviewed' WHERE id='doc_obsidian'"
        )
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, "reverse_conservation_failed"):
            reverse_document_signal_identity_collisions(
                self.conn,
                manifest_hash=preview["manifest_hash"],
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT match_status FROM document_signals WHERE id='doc_1password'"
            ).fetchone()[0],
            IDENTITY_COLLISION_MATCH_STATUS,
        )

    def test_cli_preview_apply_reverse_outputs_only_safe_counts_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "synthetic.sqlite"
            file_conn = sqlite3.connect(db_path)
            try:
                self.conn.backup(file_conn)
            finally:
                file_conn.close()
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

            preview = self._run_cli(
                db_path,
                "dry-run",
                "--historical-cutoff",
                HISTORY_CUTOFF,
                env=env,
            )
            applied = self._run_cli(
                db_path,
                "apply",
                "--confirm-hash",
                preview["manifest_hash"],
                "--historical-cutoff",
                preview["historical_cutoff"],
                env=env,
            )
            reversed_result = self._run_cli(
                db_path,
                "reverse",
                "--manifest-hash",
                preview["manifest_hash"],
                env=env,
            )

            self.assertEqual(preview["candidate_count"], 3)
            self.assertEqual(applied["updated_count"], 3)
            self.assertEqual(reversed_result["restored_count"], 3)
            safe_keys = {
                "interface",
                "status",
                "safe_status",
                "schema_version",
                "rule_version",
                "historical_cutoff",
                "candidate_count",
                "updated_count",
                "restored_count",
                "manifest_hash",
                "deleted_count",
                "attribution_changed_count",
                "raw_access",
                "destructive_actions",
            }
            for payload in (preview, applied, reversed_result):
                self.assertLessEqual(set(payload), safe_keys)
                encoded = json.dumps(payload, ensure_ascii=False)
                for forbidden in (
                    "doc_obsidian",
                    "Obsidian",
                    "1Password",
                    "Knowledge",
                    str(db_path),
                    "safe synthetic summary",
                ):
                    self.assertNotIn(forbidden, encoded)

    def _run_cli(self, db_path: Path, mode: str, *extra: str, env: dict[str, str]) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "activity_mvp",
                "document-signal-collisions",
                "--db",
                str(db_path),
                "--mode",
                mode,
                *extra,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
