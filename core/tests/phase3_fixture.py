from __future__ import annotations

import importlib.util
import hashlib
import inspect
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from activity_mvp.storage import SCHEMA_VERSION, ensure_ready
from activity_mvp.daemon import _runtime_core_code_hash


REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLOUT_PATH = REPO_ROOT / "tools" / "phase3_rollout.py"
SPEC = importlib.util.spec_from_file_location("pam_phase3_rollout", ROLLOUT_PATH)
assert SPEC is not None and SPEC.loader is not None
rollout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rollout
SPEC.loader.exec_module(rollout)
REPORT_SPEC = importlib.util.spec_from_file_location(
    "pam_verify_swift_test_report",
    REPO_ROOT / "tools" / "verify_swift_test_report.py",
)
assert REPORT_SPEC is not None and REPORT_SPEC.loader is not None
swift_report = importlib.util.module_from_spec(REPORT_SPEC)
sys.modules[REPORT_SPEC.name] = swift_report
REPORT_SPEC.loader.exec_module(swift_report)
PRIVACY_SPEC = importlib.util.spec_from_file_location(
    "pam_release_privacy_scan",
    REPO_ROOT / "tools" / "release_privacy_scan.py",
)
assert PRIVACY_SPEC is not None and PRIVACY_SPEC.loader is not None
privacy_scan = importlib.util.module_from_spec(PRIVACY_SPEC)
sys.modules[PRIVACY_SPEC.name] = privacy_scan
PRIVACY_SPEC.loader.exec_module(privacy_scan)


class Phase3Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._real_system_integrity_protection_identity = (
            rollout._system_integrity_protection_identity
        )
        self._sip_identity_patcher = patch.object(
            rollout,
            "_system_integrity_protection_identity",
            return_value={
                "status": "enabled",
                "executable": "/usr/bin/csrutil",
            },
        )
        self._sip_identity_patcher.start()


    def tearDown(self) -> None:
        self._sip_identity_patcher.stop()
        self.temp.cleanup()


    def _config(
        self,
        *,
        workspace: Path | None = None,
        database: Path | None = None,
        root: Path | None = None,
    ):
        base = root or self.root
        live_core = base / "live" / "core"
        live_core.mkdir(parents=True, exist_ok=True)
        live_database = database or (live_core / "data" / "activity.sqlite")
        return rollout.RolloutConfig(
            source_root=REPO_ROOT,
            candidate_app=base / "candidate" / "Personal AI Monitor.app",
            live_core=live_core,
            live_database=live_database,
            installed_app=base / "Applications" / "Personal AI Monitor.app",
            launch_agent_plist=base / "LaunchAgents" / "app.personalai.monitor.core.plist",
            workspace=workspace or (base / "rollout-private"),
            expected_revision="a" * 40,
            expected_source_tree_sha256="b" * 64,
            expected_core_code_sha256="c" * 64,
            expected_app_payload_sha256="d" * 64,
            expected_app_executable_sha256="e" * 64,
            expected_app_cdhash="f" * 40,
            expected_app_signature_kind="adhoc",
            expected_app_team_identifier="not set",
            gh_executable=base / "trusted" / "gh",
            expected_gh_sha256="1" * 64,
            shadow_launch_agent_label="app.personalai.monitor.codex-shadow",
            shadow_launch_agent_plist=base / "LaunchAgents" / "app.personalai.monitor.codex-shadow.plist",
            github_repository="owner/private-repo",
            github_actions_run_id=123456789,
            release_candidate_tag="v0.4.0-rc.1",
        )


    def _git(self, repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
                "LANG": "C",
                "LC_ALL": "C",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "PAM Test",
                "GIT_AUTHOR_EMAIL": "pam-test@example.invalid",
                "GIT_COMMITTER_NAME": "PAM Test",
                "GIT_COMMITTER_EMAIL": "pam-test@example.invalid",
            },
        )
        return completed.stdout.strip()


    def _core_git_fixture(self) -> tuple[Path, object]:
        repository = self.root / "source"
        repository.mkdir()
        self._git(repository, "init", "-q")
        (repository / ".gitignore").write_text(
            "core/ignored.tmp\n", encoding="utf-8"
        )
        notes = repository / "core" / "notes.txt"
        tool = repository / "core" / "bin" / "tool.sh"
        tool.parent.mkdir(parents=True)
        notes.write_text("committed bytes\n", encoding="utf-8")
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        notes.chmod(0o644)
        tool.chmod(0o755)
        self._git(repository, "add", ".gitignore", "core/notes.txt", "core/bin/tool.sh")
        self._git(repository, "commit", "-q", "-m", "fixture")
        revision = self._git(repository, "rev-parse", "HEAD")
        config = replace(
            self._config(root=self.root / "runtime"),
            source_root=repository,
            expected_revision=revision,
        )
        manifest = rollout._git_core_manifest(config)
        config = replace(
            config,
            expected_core_code_sha256=rollout._git_core_code_hash(config, manifest),
        )
        return repository, config


    def _launchctl_output(
        self,
        identity: dict[str, object],
        *,
        pid: int | None,
        working_directory: str | None = None,
    ) -> str:
        arguments = "\n".join(str(value) for value in identity["arguments"])
        pid_line = f"pid = {pid}\n" if pid is not None else "state = starting\n"
        return (
            f"program = {identity['program']}\n"
            "arguments = {\n"
            f"{arguments}\n"
            "}\n"
            f"working directory = {working_directory or identity['working_directory']}\n"
            f"{pid_line}"
        )


    def _prepare_schema28(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = ensure_ready(path)
        try:
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE IF EXISTS context_search_index")
            tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'context_%' ORDER BY name DESC"
                ).fetchall()
            ]
            for table in tables:
                conn.execute(f'DROP TABLE "{table}"')
            for trigger in (
                "pam_schema32_guard_insert",
                "pam_schema32_guard_update",
                "pam_schema31_guard_insert",
                "pam_schema31_guard_update",
                "pam_schema30_guard_insert",
                "pam_schema30_guard_update",
            ):
                conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            conn.execute("UPDATE meta SET value='28' WHERE key='schema_version'")
            for key in rollout.REQUIRED_GATES:
                conn.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?, 'true', datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=datetime('now')",
                    (key,),
                )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        path.chmod(0o600)


    def _prepare_transaction_fixture(self, root: Path):
        config = self._config(root=root)
        config.live_database.parent.mkdir(parents=True, mode=0o700)
        conn = sqlite3.connect(config.live_database)
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES('schema_version','28')")
            conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
            conn.execute("INSERT INTO settings VALUES('legacy_setting','preserved','now')")
            conn.commit()
        finally:
            conn.close()
        config.live_database.chmod(0o600)
        (config.live_core / "marker.txt").write_text("old-core", encoding="utf-8")
        agents = config.live_core / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        config.installed_app.mkdir(parents=True)
        (config.installed_app / "marker.txt").write_text("old-app", encoding="utf-8")
        config.candidate_app.mkdir(parents=True)
        config.launch_agent_plist.parent.mkdir(parents=True)
        for path, label in (
            (config.launch_agent_plist, config.service_label),
            (config.shadow_launch_agent_plist, config.shadow_launch_agent_label),
        ):
            path.write_text(label, encoding="utf-8")
            path.chmod(0o600)
        config.workspace.mkdir(mode=0o700)
        state = {
            "phase": "shadow_verified",
            "run_id": "1" * 32,
            "shadow_database": str(config.workspace / "shadow" / "activity.sqlite"),
            "shadow_verified": {"sha256": "2" * 64},
        }
        rollout._write_state(config, state)
        services = {
            config.service_label: True,
            config.shadow_launch_agent_label: True,
            "app": True,
        }
        ui = {"exists": True, "value": True}
        return config, services, ui


    def _enter_transaction_mocks(
        self,
        stack: ExitStack,
        config,
        services: dict[str, bool],
        ui: dict[str, object],
    ) -> None:
        interpreter = {
            "program": "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            "resolved_path": "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
            "sha256": "8" * 64,
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
        }
        identity = {
            "installed_app_content_mode_sha256": "3" * 64,
            "live_core_code_sha256": "4" * 64,
            "live_agents_sha256": rollout._sha256_file(config.live_core / "AGENTS.md"),
            "launch_agent_plist_sha256": rollout._sha256_file(config.launch_agent_plist),
            "shadow_launch_agent_plist_sha256": rollout._sha256_file(
                config.shadow_launch_agent_plist
            ),
            "python_interpreter": interpreter,
            "shadow_core_database_pair": {
                "core_version": "0.4.0",
                "core_code_sha256": "9" * 64,
                "database_schema": 29,
                "pair_sha256": "a" * 64,
            },
        }

        def copy_app(_source: Path, destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "marker.txt").write_text("new-app", encoding="utf-8")

        def copy_core(_config, destination: Path):
            destination.mkdir(parents=True)
            (destination / "marker.txt").write_text("new-core", encoding="utf-8")
            return {
                "git": {"manifest_sha256": "b" * 64},
                "materialized": {"content_mode_manifest_sha256": "c" * 64},
            }

        def seal_launch_agents(_config, _identity, artifacts, **_kwargs) -> None:
            for source, destination in (
                (config.launch_agent_plist, artifacts.old_plist),
                (config.shadow_launch_agent_plist, artifacts.old_shadow_plist),
            ):
                if not destination.exists():
                    rollout._copy_private_file(source, destination)

        def stage_launch_agents(
            _config,
            artifacts,
            *,
            expected_core_identity,
            expected_git_core_identity,
            expected_agents_sha256,
            source_identity,
        ):
            self.assertIsInstance(expected_core_identity, dict)
            self.assertIsInstance(expected_git_core_identity, dict)
            self.assertIsInstance(expected_agents_sha256, str)
            self.assertIsInstance(source_identity, dict)
            for source, destination in (
                (config.launch_agent_plist, artifacts.incoming_plist),
                (config.shadow_launch_agent_plist, artifacts.incoming_shadow_plist),
            ):
                rollout._copy_private_file(source, destination)
            return {
                "core_plist_sha256": rollout._sha256_file(artifacts.incoming_plist),
                "shadow_plist_sha256": rollout._sha256_file(
                    artifacts.incoming_shadow_plist
                ),
                "python_interpreter": interpreter,
                "shadow_pair": identity["shadow_core_database_pair"],
            }

        def copy_agents(_source: Path, destination: Path, *, expected_sha256: str):
            target = destination / "AGENTS.md"
            target.write_text("owner rules\n", encoding="utf-8")
            target.chmod(0o644)
            return expected_sha256

        def copy_runtime(_source: Path, destination: Path, _database_name: str):
            destination.mkdir(parents=True)
            destination.chmod(0o700)
            return {"entry_count": 1, "content_mode_manifest_sha256": "5" * 64}

        def sqlite_backup(_source: Path, destination: Path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"backup")
            destination.chmod(0o600)
            return {
                "sha256": "6" * 64,
                "aggregate_counts": {},
                "explicit_confirmation_sha256": None,
            }

        def snapshot_ui(path: Path):
            previous = dict(ui)
            rollout._write_private_json(path, previous)
            ui.update({"exists": True, "value": False})
            return previous

        def restore_ui(path: Path):
            ui.clear()
            ui.update(json.loads(path.read_text(encoding="utf-8")))

        def restore_ui_value(value):
            ui.clear()
            ui.update(value)

        @contextmanager
        def live_database_barrier(_path: Path):
            yield SimpleNamespace(
                identity="sha256:" + "7" * 64,
                connection=object(),
                verify=lambda: None,
            )

        stack.enter_context(patch.object(rollout, "_verify_shadow", return_value={"verified": True}))
        stack.enter_context(patch.object(rollout, "_assert_release_identity", return_value=identity))
        stack.enter_context(patch.object(rollout, "_copy_candidate_app", side_effect=copy_app))
        stack.enter_context(patch.object(rollout, "_copy_candidate_core", side_effect=copy_core))
        stack.enter_context(
            patch.object(
                rollout,
                "_ensure_legacy_launch_agent_seals",
                side_effect=seal_launch_agents,
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_stage_isolated_launch_agents",
                side_effect=stage_launch_agents,
            )
        )
        stack.enter_context(
            patch.object(rollout, "_copy_live_runtime_agents", side_effect=copy_agents)
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_materialized_core_identity",
                return_value={"content_mode_manifest_sha256": "d" * 64},
            )
        )
        stack.enter_context(patch.object(rollout, "_assert_materialized_core_identity"))
        stack.enter_context(
            patch.object(
                rollout,
                "_storage_headroom",
                return_value={"status": "ready", "volumes": []},
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_database_snapshot_storage_bytes",
                return_value=1024 * 1024,
            )
        )
        stack.enter_context(patch.object(rollout, "_verify_candidate_code", return_value={"ok": True}))
        stack.enter_context(patch.object(rollout, "_copy_runtime_data", side_effect=copy_runtime))
        stack.enter_context(patch.object(rollout, "_sqlite_backup", side_effect=sqlite_backup))
        stack.enter_context(patch.object(rollout, "_migrate_with_candidate_core"))
        stack.enter_context(patch.object(rollout, "_set_initial_rollout_gates_off"))
        stack.enter_context(patch.object(rollout, "_sqlite_probe", return_value={"sha256": "6" * 64}))
        stack.enter_context(
            patch.object(
                rollout,
                "_verify_migration_backup",
                return_value={"sha256": "6" * 64, "identity_verified": True},
            )
        )
        stack.enter_context(patch.object(rollout, "_assert_legacy_data_preserved"))
        stack.enter_context(patch.object(rollout, "_verify_candidate_runtime", return_value={"database": {}}))
        stack.enter_context(patch.object(rollout, "_verify_old_release_artifacts", return_value={"ok": True}))
        stack.enter_context(patch.object(rollout, "_verify_restored_rollback_runtime"))
        stack.enter_context(
            patch.object(
                rollout,
                "_verify_release_before_rollback",
                side_effect=lambda _config, _state, _artifacts: {
                    "current_quarantine_paths": {
                        "core_plist_present": True,
                        "shadow_plist_present": True,
                    },
                    "release_gate_status": {"context_core_gate": "false"},
                    "release_core_identity_status": "valid",
                    "release_ui_preference": dict(ui),
                    "release_app_was_running": services["app"],
                    "release_core_was_loaded": services[config.service_label],
                    "release_shadow_was_loaded": services[
                        config.shadow_launch_agent_label
                    ],
                    "rollback_database_recovery_required": False,
                },
            )
        )
        agent_identity = {
            "interpreter": interpreter,
            "shadow_pair": identity["shadow_core_database_pair"],
        }
        stack.enter_context(
            patch.object(
                rollout,
                "_verify_core_launch_agent",
                return_value=agent_identity,
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_verify_shadow_launch_agent",
                return_value=agent_identity,
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_verify_shadow_runtime",
                return_value={"running": True, "pid": 456, "canonical_core": True},
            )
        )
        stack.enter_context(patch.object(rollout, "_verify_runtime_agents", return_value=identity["live_agents_sha256"]))
        stack.enter_context(
            patch.object(
                rollout,
                "_quit_app",
                side_effect=lambda: services.__setitem__("app", False),
            )
        )
        stack.enter_context(patch.object(rollout, "_assert_app_stopped"))
        stack.enter_context(
            patch.object(
                rollout,
                "_stop_app",
                side_effect=lambda _config, force, **_kwargs: services.__setitem__("app", False),
            )
        )
        stack.enter_context(
            patch.object(rollout, "_app_running", side_effect=lambda _config: services["app"])
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_restore_app_state",
                side_effect=lambda _config, running, **_kwargs: services.__setitem__("app", running),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_verified_app_signal_snapshots",
                return_value=(("/fixture/app", 1, 2, 3, 4, "f" * 64),),
            )
        )
        stack.enter_context(patch.object(rollout, "_assert_database_not_open"))
        stack.enter_context(
            patch.object(
                rollout,
                "_live_database_writer_barrier",
                side_effect=live_database_barrier,
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_restore_schema28_database_from_backup",
                return_value={"status": "original_or_recovered", "database": {}},
            )
        )
        stack.enter_context(
            patch.object(rollout, "_launch_agent_loaded", side_effect=lambda label: services[label])
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_bootout",
                side_effect=lambda _config: services.__setitem__(config.service_label, False),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_bootout_label",
                side_effect=lambda label: services.__setitem__(label, False),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_bootout_shadow",
                side_effect=lambda _config: services.__setitem__(
                    config.shadow_launch_agent_label, False
                ),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_write_context_core_gate_for_rollback",
                return_value="applied",
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_assert_launch_agent_stopped",
                side_effect=lambda label: (
                    None
                    if not services[label]
                    else (_ for _ in ()).throw(rollout.RolloutError("service_running"))
                ),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_bootstrap",
                side_effect=lambda _config: services.__setitem__(config.service_label, True),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_restore_agent_state",
                side_effect=lambda label, _plist, loaded: services.__setitem__(label, loaded),
            )
        )
        stack.enter_context(
            patch.object(
                rollout,
                "_verify_launch_agent",
                side_effect=lambda _config, **_kwargs: (
                    {"running": True, "pid": 123, "canonical_core": True}
                    if services[config.service_label]
                    else (_ for _ in ()).throw(rollout.RolloutError("service_stopped"))
                ),
            )
        )
        stack.enter_context(patch.object(rollout, "_read_ui_flag", side_effect=lambda: dict(ui)))
        stack.enter_context(patch.object(rollout, "_snapshot_and_disable_ui_flag", side_effect=snapshot_ui))
        stack.enter_context(patch.object(rollout, "_restore_ui_flag", side_effect=restore_ui))
        stack.enter_context(patch.object(rollout, "_restore_ui_value", side_effect=restore_ui_value))
        stack.enter_context(
            patch.object(
                rollout,
                "_set_ui_disabled",
                side_effect=lambda: ui.update({"exists": True, "value": False}),
            )
        )


    def _prepare_database_recovery_case(self, name: str):
        case_root = self.root / name
        case_root.mkdir()
        config = self._config(root=case_root)
        self._prepare_schema28(config.live_database)
        config.live_database.parent.chmod(0o700)
        config.live_database.chmod(0o600)
        run_id = rollout.hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        artifacts = rollout._rollout_artifacts(config, run_id)
        source_probe = rollout._sqlite_probe(
            config.live_database,
            expected_schema=rollout.SOURCE_SCHEMA,
        )
        backup_probe = rollout._sqlite_backup(
            config.live_database,
            artifacts.rollback_database,
        )
        state = {
            "run_id": run_id,
            "final_schema28": backup_probe,
            "rollback_database_sha256": backup_probe["sha256"],
            "rollback_identity": {"database": source_probe},
        }
        return config, state, artifacts, source_probe, backup_probe


