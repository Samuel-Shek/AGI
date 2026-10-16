from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from activity_mvp.daemon import _safe_cleanup_retention_for_daemon
from activity_mvp.daemon import cleanup_raw_vault
from activity_mvp.document_changes import document_authorization_lock
from activity_mvp.ha_source import (
    expire_packages,
    generate_package,
    get_package,
    purge_package_cache_refs,
    retry_pending_package_cache_purges,
    revoke_package,
)
from activity_mvp.lifecycle import delete_range
from activity_mvp.pipeline import ingest_events
from activity_mvp.private_purge import (
    private_purge_status,
    schedule_private_purge,
)
from activity_mvp.reclassify import reclassify_existing
from activity_mvp.storage import SCHEMA_VERSION, connect_readonly, ensure_ready


def private_vlm_test_image(root: Path, name: str) -> Path:
    vault = root / "raw_vault"
    vault.mkdir(mode=0o700, exist_ok=True)
    vault.chmod(0o700)
    path = vault / name
    path.touch(mode=0o600, exist_ok=False)
    path.chmod(0o600)
    return path


class PrivatePurgeRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "activity.sqlite"
        self.conn = ensure_ready(self.db_path)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def _event(self, suffix: str, *, at: datetime | None = None) -> dict:
        observed = at or self.now
        return {
            "captured_at": observed.isoformat(timespec="seconds"),
            "display_id": "main",
            "frame_hash": f"frame-{suffix}",
            "app_name": "Obsidian",
            "window_title": f"Private purge {suffix}",
            "ocr_text": f"Private purge regression {suffix}",
            "context_confidence": 0.9,
        }

    def _package(self, suffix: str) -> dict:
        observed = self.now - timedelta(minutes=2)
        ingest_events(self.conn, [self._event(suffix, at=observed)])
        return generate_package(
            self.conn,
            (observed - timedelta(minutes=1)).isoformat(timespec="seconds"),
            (observed + timedelta(minutes=1)).isoformat(timespec="seconds"),
            self.root / f"packages-{suffix}",
        )

    def _retry_rows(self, kind: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT key,value FROM daemon_state "
            "WHERE key LIKE ? ORDER BY key",
            (f"private_purge_retry:{kind}:%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def _assert_content_free_retry_rows(self, kind: str) -> None:
        rows = self._retry_rows(kind)
        self.assertTrue(rows)
        encoded = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn(".png", encoded)
        self.assertNotIn(".json", encoded)



    def test_legacy_explicit_cache_refs_delete_without_undefined_identity(self) -> None:
        package_id = "ha_pkg_0123456789abcdef0123456789abcdef"
        json_path = self.root / f"{package_id}.json"
        markdown_path = self.root / f"{package_id}.md"
        json_path.write_text("{}", encoding="utf-8")
        markdown_path.write_text("# safe", encoding="utf-8")

        purged = purge_package_cache_refs([str(json_path)])

        self.assertEqual(purged, {"files_deleted": 2, "failed_refs": 0})
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())











    def test_repeated_revoke_retries_cache_and_never_leaves_writer_transaction(self) -> None:
        generated = self._package("repeat-revoke")
        package_id = generated["package"]["package_id"]
        json_path = Path(generated["json_path"])
        markdown_path = Path(generated["markdown_path"])

        with patch(
            "activity_mvp.ha_source._delete_package_cache",
            side_effect=PermissionError("denied"),
        ):
            first = revoke_package(self.conn, package_id, "permission_regression")

        self.assertTrue(first["revoked"])
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["cache_purge_failed"], 1)
        self.assertFalse(self.conn.in_transaction)
        self.assertTrue(json_path.exists())
        self._assert_content_free_retry_rows("ha")
        self.conn.close()
        self.conn = ensure_ready(self.db_path)

        second = revoke_package(self.conn, package_id, "permission_regression_retry")

        self.assertFalse(second["revoked"])
        self.assertEqual(second["status"], "completed")
        self.assertFalse(self.conn.in_transaction)
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())
        self.assertEqual(private_purge_status(self.conn)["pending_count"], 0)

    def test_partial_ha_cache_delete_counts_only_files_actually_removed(self) -> None:
        import activity_mvp.ha_source as ha_source_module

        generated = self._package("partial-count")
        package_id = generated["package"]["package_id"]
        json_path = Path(generated["json_path"])
        markdown_path = Path(generated["markdown_path"])
        original_unlink = ha_source_module._unlink_private_cache_file

        def fail_markdown(path: Path) -> int:
            if path.suffix == ".md":
                raise PermissionError("denied")
            return original_unlink(path)

        with patch(
            "activity_mvp.ha_source._unlink_private_cache_file",
            side_effect=fail_markdown,
        ):
            revoked = revoke_package(self.conn, package_id, "partial_count")

        self.assertEqual(revoked["cache_files_deleted"], 1)
        self.assertEqual(revoked["cache_purge_failed"], 1)
        self.assertFalse(json_path.exists())
        self.assertTrue(markdown_path.exists())
        self.assertEqual(retry_pending_package_cache_purges(self.conn)["pending_refs"], 0)
        self.assertFalse(markdown_path.exists())

    def test_external_custom_output_remains_purgeable_when_parent_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as external_temp:
            observed = self.now - timedelta(minutes=2)
            ingest_events(self.conn, [self._event("custom-out", at=observed)])
            generated = generate_package(
                self.conn,
                (observed - timedelta(minutes=1)).isoformat(timespec="seconds"),
                (observed + timedelta(minutes=1)).isoformat(timespec="seconds"),
                Path(external_temp),
            )
            package_id = generated["package"]["package_id"]

            revoked = revoke_package(self.conn, package_id, "custom_out_regression")

            self.assertEqual(revoked["status"], "completed")
            self.assertEqual(revoked["cache_files_deleted"], 2)
            self.assertFalse(Path(generated["json_path"]).exists())
            self.assertFalse(Path(generated["markdown_path"]).exists())

    def test_ha_retry_rejects_unprivate_cache_parent(self) -> None:
        generated = self._package("unsafe-parent")
        package_id = generated["package"]["package_id"]
        with tempfile.TemporaryDirectory() as external_temp:
            external_parent = Path(external_temp)
            external_parent.chmod(0o755)
            external = external_parent / f"{package_id}.json"
            external.write_text("outside", encoding="utf-8")
            self.conn.execute(
                "UPDATE distribution_packages SET cache_ref=? WHERE id=?",
                (str(external), package_id),
            )
            self.conn.commit()

            revoked = revoke_package(self.conn, package_id, "unsafe_parent_regression")

            self.assertEqual(revoked["status"], "partial")
            self.assertEqual(revoked["cache_files_deleted"], 0)
            self.assertTrue(external.exists())
            self._assert_content_free_retry_rows("ha")
            external_parent.chmod(0o700)
            self.conn.execute(
                "UPDATE distribution_packages SET cache_ref=? WHERE id=?",
                (generated["json_path"], package_id),
            )
            self.conn.commit()
            self.assertEqual(
                retry_pending_package_cache_purges(self.conn)["pending_refs"],
                0,
            )

    def test_ha_retry_rejects_cache_basename_not_matching_package_id(self) -> None:
        generated = self._package("wrong-basename")
        package_id = generated["package"]["package_id"]
        wrong_path = Path(generated["json_path"]).parent / "ha_pkg_ffffffffffffffffffffffffffffffff.json"
        wrong_path.write_text("do not delete", encoding="utf-8")
        self.conn.execute(
            "UPDATE distribution_packages SET cache_ref=? WHERE id=?",
            (str(wrong_path), package_id),
        )
        self.conn.commit()

        revoked = revoke_package(self.conn, package_id, "wrong_basename_regression")

        self.assertEqual(revoked["status"], "partial")
        self.assertTrue(wrong_path.exists())
        self.conn.execute(
            "UPDATE distribution_packages SET cache_ref=? WHERE id=?",
            (generated["json_path"], package_id),
        )
        self.conn.commit()
        self.assertEqual(retry_pending_package_cache_purges(self.conn)["pending_refs"], 0)

    def test_expiry_and_lifecycle_failures_remain_retryable(self) -> None:
        expiring = self._package("expiry")
        expiring_id = expiring["package"]["package_id"]
        self.conn.execute(
            "UPDATE ha_source_records SET retention_until=? WHERE package_id=?",
            (
                (self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                expiring_id,
            ),
        )
        self.conn.commit()
        with patch(
            "activity_mvp.ha_source._delete_package_cache",
            side_effect=PermissionError("denied"),
        ):
            expiry = expire_packages(self.conn, return_details=True)
        self.assertEqual(expiry["status"], "partial")
        self.assertIn(expiring_id, expiry["package_ids"])
        self.assertTrue(Path(expiring["json_path"]).exists())
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(retry_pending_package_cache_purges(self.conn)["pending_refs"], 0)
        self.assertFalse(Path(expiring["json_path"]).exists())

        lifecycle = self._package("lifecycle")
        lifecycle_id = lifecycle["package"]["package_id"]
        time_range = lifecycle["package"]["time_range"]
        with patch(
            "activity_mvp.ha_source._delete_package_cache",
            side_effect=PermissionError("denied"),
        ):
            deleted = delete_range(
                self.conn,
                time_range["from"],
                time_range["to"],
            )
        self.assertEqual(deleted["status"], "partial")
        self.assertEqual(deleted["cascade"]["ha_package_cache_purge_failed"], 1)
        self.assertEqual(get_package(self.conn, lifecycle_id)["source_status"], "user_deleted")
        self.assertTrue(Path(lifecycle["json_path"]).exists())
        self.assertEqual(retry_pending_package_cache_purges(self.conn)["pending_refs"], 0)
        self.assertFalse(Path(lifecycle["json_path"]).exists())

    def test_reclassify_rejects_dirty_connection_before_any_revocation(self) -> None:
        generated = self._package("dirty-reclassify")
        package_id = generated["package"]["package_id"]
        activity = self.conn.execute(
            "SELECT id,deletion_group_id FROM extracted_activities ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        sensitive = "Chrome / pornhub.com：PRIVATE_RECLASSIFY_TEXT"
        self.conn.execute(
            "UPDATE extracted_activities SET activity_summary=?,apps=?,domains=? WHERE id=?",
            (sensitive, '["Chrome"]', '["pornhub.com"]', activity["id"]),
        )
        self.conn.execute(
            "UPDATE frame_signals SET app_name='Chrome',domain='pornhub.com',"
            "ocr_redacted_summary=? WHERE deletion_group_id=?",
            (sensitive, activity["deletion_group_id"]),
        )
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
            ("dirty_reclassify_probe", "pending", self.now.isoformat()),
        )
        self.assertTrue(self.conn.in_transaction)

        with self.assertRaisesRegex(RuntimeError, "clean SQLite connection"):
            reclassify_existing(self.conn)

        self.assertTrue(self.conn.in_transaction)
        observer = connect_readonly(self.db_path)
        try:
            status = observer.execute(
                "SELECT source_status FROM ha_source_records WHERE package_id=?",
                (package_id,),
            ).fetchone()["source_status"]
            self.assertEqual(status, "active")
            self.assertEqual(private_purge_status(observer)["pending_count"], 0)
        finally:
            observer.close()
        self.conn.rollback()

    def test_reclassify_permission_error_returns_partial_and_retries(self) -> None:
        generated = self._package("reclassify-retry")
        package_id = generated["package"]["package_id"]
        activity = self.conn.execute(
            "SELECT id,deletion_group_id FROM extracted_activities ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        sensitive = "Chrome / pornhub.com：PRIVATE_RECLASSIFY_RETRY"
        self.conn.execute(
            "UPDATE extracted_activities SET activity_summary=?,apps=?,domains=? WHERE id=?",
            (sensitive, '["Chrome"]', '["pornhub.com"]', activity["id"]),
        )
        self.conn.execute(
            "UPDATE frame_signals SET app_name='Chrome',domain='pornhub.com',"
            "ocr_redacted_summary=? WHERE deletion_group_id=?",
            (sensitive, activity["deletion_group_id"]),
        )
        self.conn.commit()
        with patch(
            "activity_mvp.ha_source._delete_package_cache",
            side_effect=PermissionError("denied"),
        ):
            result = reclassify_existing(self.conn)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["ha_package_cache_purge_failed"], 1)
        self.assertEqual(get_package(self.conn, package_id)["source_status"], "revoked")
        self.assertTrue(Path(generated["json_path"]).exists())
        self.assertEqual(retry_pending_package_cache_purges(self.conn)["pending_refs"], 0)
        self.assertFalse(Path(generated["json_path"]).exists())

    def test_missing_or_invalid_cache_revocation_waits_for_exclusive_lease(self) -> None:
        for failure_kind in ("missing", "invalid"):
            with self.subTest(failure_kind=failure_kind):
                generated = self._package(f"lease-{failure_kind}")
                package_id = generated["package"]["package_id"]
                json_path = Path(generated["json_path"])
                if failure_kind == "missing":
                    json_path.unlink()
                    expected_reason = "cache_missing"
                else:
                    json_path.write_text("{}", encoding="utf-8")
                    expected_reason = "package_validation_failed"

                held = threading.Event()
                release = threading.Event()
                detected = threading.Event()
                done = threading.Event()
                errors: list[BaseException] = []
                response: dict = {}

                def hold_shared() -> None:
                    conn = ensure_ready(self.db_path)
                    try:
                        with document_authorization_lock(conn, exclusive=False):
                            held.set()
                            if not release.wait(5):
                                raise AssertionError("shared lease was not released")
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        conn.close()

                import activity_mvp.ha_source as ha_source_module

                original_get = ha_source_module._get_package_locked

                def observed_get(*args, **kwargs):
                    value = original_get(*args, **kwargs)
                    if value.get("reason") == expected_reason:
                        detected.set()
                    return value

                def read_package() -> None:
                    conn = ensure_ready(self.db_path)
                    try:
                        response.update(get_package(conn, package_id))
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        done.set()
                        conn.close()

                holder = threading.Thread(target=hold_shared, daemon=True)
                holder.start()
                self.assertTrue(held.wait(5))
                with patch(
                    "activity_mvp.ha_source._get_package_locked",
                    side_effect=observed_get,
                ):
                    reader = threading.Thread(target=read_package, daemon=True)
                    reader.start()
                    self.assertTrue(detected.wait(5))
                    time.sleep(0.1)
                    self.assertFalse(done.is_set())
                    observer = ensure_ready(self.db_path)
                    try:
                        status = observer.execute(
                            "SELECT source_status FROM ha_source_records WHERE package_id=?",
                            (package_id,),
                        ).fetchone()["source_status"]
                        self.assertEqual(status, "active")
                    finally:
                        observer.close()
                    release.set()
                    reader.join(5)
                holder.join(5)
                self.assertFalse(reader.is_alive())
                self.assertFalse(holder.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(response["source_status"], "revoked")
                self.assertEqual(response["reason"], expected_reason)
                self.assertFalse(response["raw_access"])
                self.assertFalse(self.conn.in_transaction)

    def test_daemon_retention_never_marks_pending_purge_ok(self) -> None:
        generated = self._package("daemon")
        package_id = generated["package"]["package_id"]
        with patch(
            "activity_mvp.ha_source._delete_package_cache",
            side_effect=PermissionError("denied"),
        ):
            revoked = revoke_package(self.conn, package_id, "daemon_retry_seed")
            self.assertEqual(revoked["status"], "partial")
            retention = _safe_cleanup_retention_for_daemon(self.conn, self.root)

        self.assertEqual(retention["status"], "partial")
        self.assertEqual(retention["error"], "private_purge_pending")
        state = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key IN "
                "('private_purge_status','private_purge_pending_count')"
            ).fetchall()
        }
        self.assertEqual(state["private_purge_status"], "pending")
        self.assertGreater(int(state["private_purge_pending_count"]), 0)
        self.assertTrue(Path(generated["json_path"]).exists())
        self.assertEqual(retry_pending_package_cache_purges(self.conn)["pending_refs"], 0)
        self.assertFalse(Path(generated["json_path"]).exists())


if __name__ == "__main__":
    unittest.main()
