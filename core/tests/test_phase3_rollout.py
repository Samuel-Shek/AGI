from phase3_fixture import *
from phase3_fixture import _runtime_core_code_hash


class Phase3RolloutTests(Phase3Fixture):
    def test_minimal_environment_pins_system_toolchain_without_inherited_settings(self):
        with patch.dict(os.environ, {'DEVELOPER_DIR':'/tmp/untrusted-developer','PYTHONPATH':'/tmp/untrusted-python'}):
            env=rollout._minimal_command_environment()
        self.assertEqual(env['DEVELOPER_DIR'],'/Library/Developer/CommandLineTools')
        self.assertNotIn('PYTHONPATH',env)
        self.assertNotIn('HOME',env)

    def test_system_integrity_protection_is_an_install_precondition(self) -> None:
        enabled = subprocess.CompletedProcess(
            ["/usr/bin/csrutil", "status"],
            0,
            "System Integrity Protection status: enabled.\n",
            "",
        )
        with patch.object(rollout, "_run", return_value=enabled):
            self.assertEqual(
                self._real_system_integrity_protection_identity(),
                {"status": "enabled", "executable": "/usr/bin/csrutil"},
            )

        for result in (
            subprocess.CompletedProcess(
                ["/usr/bin/csrutil", "status"],
                0,
                "System Integrity Protection status: disabled.\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["/usr/bin/csrutil", "status"],
                1,
                "",
                "unavailable\n",
            ),
        ):
            with self.subTest(result=result):
                with patch.object(rollout, "_run", return_value=result):
                    with self.assertRaisesRegex(
                        rollout.RolloutError,
                        "system_integrity_protection_required",
                    ):
                        self._real_system_integrity_protection_identity()


    def test_release_tool_stays_anchored_to_040_while_tree_is_current(self) -> None:
        # The rollout tool is frozen on its historical mission (install 0.4.0
        # by migrating schema 28 to 31); the source tree has since moved to
        # the 0.4.1 hotfix line, so tool anchors and tree surfaces diverge on
        # purpose here.
        self.assertEqual(rollout.RELEASE_VERSION, "0.4.0")
        self.assertEqual(
            ROLLOUT_PATH.read_text(encoding="utf-8").splitlines()[0],
            "#!/usr/bin/env -S -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=C LC_ALL=C /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -I -S",
        )
        self.assertEqual(rollout.stat.S_IMODE(ROLLOUT_PATH.stat().st_mode), 0o755)
        self.assertEqual(rollout.SOURCE_SCHEMA, 28)
        self.assertEqual(rollout.TARGET_SCHEMA, 31)
        self.assertEqual(rollout._core_version(REPO_ROOT / "core"), "0.9.1")
        self.assertEqual(
            json.loads((REPO_ROOT / "core/browser_extension/manifest.json").read_text())["version"],
            "0.9.1",
        )
        self.assertIn(
            'MARKETING_VERSION="0.9.1"',
            (REPO_ROOT / "scripts/build-macos-app-impl.sh").read_text(),
        )
        self.assertEqual(
            rollout.PHASE01_ARCHIVE_SHA256,
            "8f115cc1b1263d97caeb1e4e2b1a51248e4d45ce07c4a30199c46b9a64775c38",
        )
        self.assertEqual(
            rollout.PHASE01_PAYLOAD_TREE_SHA256,
            "04dff9cadb515dd118767b669028b21b9c931793f26d4c579beb5aec3342fbdd",
        )
        self.assertEqual(
            rollout.PHASE2_ARCHIVE_SHA256,
            "abff7c7a496e1d47604e941c304488183c4903202d1d60f09dd6feb253fbf11d",
        )
        self.assertEqual(
            rollout.PHASE2_PAYLOAD_TREE_SHA256,
            "7c57c139d9d2189aedd8bade16e5ab838a46eff0237b042dbfe7613b7151c607",
        )


    def test_release_chain_binds_archive_commits_and_rejects_merge_parents(self) -> None:
        config = self._config()
        third = "3" * 40
        revision = "4" * 40
        chain = [rollout.PHASE01_REVISION, rollout.PHASE2_REVISION, third, revision]
        parents = {
            rollout.PHASE01_REVISION: rollout.BASELINE_REVISION,
            rollout.PHASE2_REVISION: rollout.PHASE01_REVISION,
            third: rollout.PHASE2_REVISION,
            revision: third,
        }

        def git_output(_config, *args):
            if args == ("merge-base", rollout.BASELINE_REVISION, revision):
                return rollout.BASELINE_REVISION
            if args[:2] == ("rev-list", "--reverse"):
                return "\n".join(chain)
            if args[:3] == ("rev-list", "--first-parent", "--reverse"):
                return "\n".join(chain)
            if args[:4] == ("rev-list", "--parents", "-n", "1"):
                commit = args[4]
                return f"{commit} {parents[commit]}"
            raise AssertionError(args)

        with patch.object(rollout, "_git_output", side_effect=git_output):
            evidence = rollout._verify_release_commit_chain(config, revision)
        self.assertEqual(evidence["commit_chain"], chain)
        self.assertEqual(
            evidence["phase2_archive_evidence"]["zip_sha256"],
            rollout.PHASE2_ARCHIVE_SHA256,
        )

        parents[third] = f"{rollout.PHASE2_REVISION} {'9' * 40}"
        with patch.object(rollout, "_git_output", side_effect=git_output):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "release_non_linear_or_merge_commit",
            ):
                rollout._verify_release_commit_chain(config, revision)


    def test_config_is_closed_absolute_private_and_non_overlapping(self) -> None:
        config_path = self.root / "config.json"
        payload = {
            "source_root": str(REPO_ROOT),
            "candidate_app": str(self.root / "candidate.app"),
            "live_core": str(self.root / "live/core"),
            "live_database": str(self.root / "live/core/data/activity.sqlite"),
            "installed_app": str(self.root / "Applications/PAM.app"),
            "launch_agent_plist": str(self.root / "LaunchAgents/core.plist"),
            "workspace": str(self.root / "private-rollout"),
            "expected_revision": "a" * 40,
            "expected_source_tree_sha256": "b" * 64,
            "expected_core_code_sha256": "c" * 64,
            "expected_app_payload_sha256": "d" * 64,
            "expected_app_executable_sha256": "e" * 64,
            "expected_app_cdhash": "f" * 40,
            "expected_app_signature_kind": "adhoc",
            "expected_app_team_identifier": "not set",
            "gh_executable": str(self.root / "trusted/gh"),
            "expected_gh_sha256": "1" * 64,
            "shadow_launch_agent_label": "app.personalai.monitor.codex-shadow",
            "shadow_launch_agent_plist": str(self.root / "LaunchAgents/codex-shadow.plist"),
            "expected_privacy_profile": "personal_high_precision",
            "github_repository": "owner/private-repo",
            "github_actions_run_id": 123456789,
            "release_branch": "release/v0.4.0-context-console",
            "release_candidate_tag": "v0.4.0-rc.1",
        }
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        config_path.chmod(0o644)
        with self.assertRaisesRegex(rollout.RolloutError, "config_permissions_invalid"):
            rollout.load_config(config_path)
        config_path.chmod(0o600)
        self.assertEqual(rollout.load_config(config_path).workspace, self.root / "private-rollout")

        relative_config = Path(os.path.relpath(config_path, start=Path.cwd()))
        self.assertFalse(relative_config.is_absolute())
        self.assertTrue(relative_config.is_file())
        with self.assertRaisesRegex(rollout.RolloutError, "relative_config_forbidden"):
            rollout.load_config(relative_config)

        symlink_config = self.root / "config-link.json"
        symlink_config.symlink_to(config_path)
        with self.assertRaisesRegex(rollout.RolloutError, "config_not_regular_file"):
            rollout.load_config(symlink_config)

        hardlink_config = self.root / "config-hardlink.json"
        os.link(config_path, hardlink_config)
        with self.assertRaisesRegex(rollout.RolloutError, "config_permissions_invalid"):
            rollout.load_config(config_path)
        hardlink_config.unlink()
        self.assertEqual(rollout.load_config(config_path).workspace, self.root / "private-rollout")

        payload["workspace"] = str(self.root / "live/core/rollout")
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        config_path.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutError, "workspace_overlaps_protected_path"):
            rollout.load_config(config_path)


    def test_ui_preference_only_treats_exact_missing_pair_as_absent(self) -> None:
        missing = subprocess.CompletedProcess(
            ["defaults"],
            1,
            stdout="",
            stderr=(
                "The domain/default pair of "
                f"({rollout.APP_DEFAULTS_DOMAIN}, {rollout.PHASE2_UI_FLAG}) does not exist\n"
            ),
        )
        with patch.object(rollout, "_run", return_value=missing):
            self.assertEqual(rollout._read_ui_flag(), {"exists": False, "value": None})

        unexpected = subprocess.CompletedProcess(
            ["defaults"],
            1,
            stdout="",
            stderr="cfprefsd refused access to the preference domain\n",
        )
        with patch.object(rollout, "_run", return_value=unexpected):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "phase2_ui_preference_unreadable",
            ):
                rollout._read_ui_flag()


    def test_state_and_rename_directory_entries_are_fsynced_before_success(self) -> None:
        config = self._config()
        config.workspace.mkdir(mode=0o700)
        with patch.object(rollout, "_fsync_directory") as directory_fsync:
            rollout._write_state(config, {"phase": "fixture"})
        directory_fsync.assert_called_once_with(config.workspace)
        self.assertEqual(rollout._load_state(config)["phase"], "fixture")

        source = self.root / "source-parent" / "source"
        destination = self.root / "destination-parent" / "destination"
        source.parent.mkdir()
        destination.parent.mkdir()
        source.write_text("sealed", encoding="utf-8")
        with patch.object(rollout.os, "fsync", wraps=os.fsync) as fsync:
            rollout._durable_rename(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "sealed")
        self.assertGreaterEqual(fsync.call_count, 2)
        replacement = source.parent / "replacement"
        replacement.write_text("new", encoding="utf-8")
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "durable_rename_destination_exists",
        ):
            rollout._durable_rename(replacement, destination)


    def test_sqlite_backup_is_wal_consistent_private_and_no_clobber(self) -> None:
        source = self.root / "source.sqlite"
        conn = sqlite3.connect(source)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version','28')")
        conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.execute("INSERT INTO evidence VALUES('committed-before-backup')")
        conn.commit()
        source.chmod(0o600)
        destination = self.root / "private" / "backup.sqlite"
        result = rollout._sqlite_backup(source, destination)
        self.assertEqual(result["schema"], 28)
        self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o600")
        copied = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        try:
            self.assertEqual(copied.execute("SELECT value FROM evidence").fetchone()[0], "committed-before-backup")
        finally:
            copied.close()
            conn.close()
        with self.assertRaisesRegex(rollout.RolloutError, "backup_destination_exists"):
            rollout._sqlite_backup(source, destination)


    def test_rollback_gate_helper_never_writes_schema28(self) -> None:
        database = self.root / "schema28-gate.sqlite"
        conn = sqlite3.connect(database)
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES('schema_version','28')")
            conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
            conn.execute(
                "INSERT INTO settings VALUES(?, 'true', 'unchanged')",
                (rollout.CONTEXT_CORE_GATE,),
            )
            conn.commit()
        finally:
            conn.close()
        database.chmod(0o600)
        before = rollout._sha256_file(database)

        self.assertEqual(
            rollout._write_context_core_gate_for_rollback(
                database,
                "false",
                required=False,
            ),
            "unavailable",
        )
        self.assertEqual(rollout._sha256_file(database), before)
        read_only = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            self.assertEqual(
                read_only.execute(
                    "SELECT value,updated_at FROM settings WHERE key=?",
                    (rollout.CONTEXT_CORE_GATE,),
                ).fetchone(),
                ("true", "unchanged"),
            )
        finally:
            read_only.close()
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "context_core_gate_restore_failed",
        ):
            rollout._write_context_core_gate_for_rollback(
                database,
                "false",
                required=True,
            )


    def test_schema28_shadow_reaches_target_or_rejects_newer_core(self) -> None:
        database = self.root / "live" / "core" / "data" / "activity.sqlite"
        self._prepare_schema28(database)
        config = self._config(database=database)
        revision = self._git(REPO_ROOT, "rev-parse", "HEAD")
        config = replace(config, expected_revision=revision)
        manifest = rollout._git_core_manifest(config)
        config = replace(
            config,
            expected_core_code_sha256=rollout._git_core_code_hash(config, manifest),
        )
        agents = config.live_core / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        interpreter = {
            "program": sys.executable,
            "resolved_path": str(Path(sys.executable).resolve()),
            "sha256": "8" * 64,
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
        }
        identity = {
            "python_interpreter": interpreter,
            "live_agents_sha256": rollout._sha256_file(agents),
            "git_core_identity": rollout._git_core_manifest_identity(config, manifest),
        }
        with patch.object(
            rollout,
            "_assert_release_identity",
            return_value=identity,
        ), patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(
            rollout,
            "_storage_headroom",
            return_value={"status": "ready", "phase": "shadow", "volumes": []},
        ):
            if SCHEMA_VERSION > rollout.TARGET_SCHEMA:
                # This installer is intentionally frozen on the historical
                # schema-28 -> schema-31 mission.  Once the current tree moves
                # beyond schema 31, rehearsing it must fail closed instead of
                # accepting a newer database as the old release target.
                with self.assertRaisesRegex(
                    rollout.RolloutError,
                    "database_integrity_or_schema_mismatch",
                ):
                    rollout.shadow(
                        config,
                        apply=True,
                        confirmation=rollout.SHADOW_CONFIRMATION,
                    )
                self.assertEqual(
                    rollout._sqlite_probe(database, expected_schema=rollout.SOURCE_SCHEMA)[
                        "schema"
                    ],
                    rollout.SOURCE_SCHEMA,
                )
                shadow_db = config.workspace / "shadow" / "activity.sqlite"
                self.assertEqual(
                    rollout._sqlite_probe(shadow_db, expected_schema=SCHEMA_VERSION)["schema"],
                    SCHEMA_VERSION,
                )
                return
            result = rollout.shadow(
                config,
                apply=True,
                confirmation=rollout.SHADOW_CONFIRMATION,
            )
        self.assertEqual(result["status"], "verified")
        state = rollout._load_state(config)
        shadow_db = Path(state["shadow_database"])
        self.assertEqual(rollout._sqlite_probe(shadow_db, expected_schema=31)["schema"], 31)
        self.assertTrue(state["migration_backup_verified"]["identity_verified"])
        self.assertEqual(rollout._verify_shadow(config)["status"], "verified")

        redirected = dict(state)
        redirected["shadow_database"] = str(self.root / "redirected.sqlite")
        rollout._write_state(config, redirected)
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "shadow_database_path_invalid",
        ):
            rollout._verify_shadow(config)
        rollout._write_state(config, state)

        identity_path = shadow_db.with_name("activity.sqlite.schema28-context-backup.identity.json")
        original_identity = identity_path.read_bytes()
        identity = json.loads(identity_path.read_text())
        identity["database_identity"] = "sha256:" + "0" * 64
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        identity_path.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutError, "migration_backup_identity_mismatch"):
            rollout._verify_migration_backup(shadow_db)
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "migration_backup_identity_mismatch",
        ):
            rollout._verify_shadow(config)

        identity_path.write_bytes(original_identity)
        identity_path.chmod(0o600)
        backup = shadow_db.with_name("activity.sqlite.schema28-context-backup")
        original_backup = backup.read_bytes()
        backup.write_bytes(original_backup + b"tamper")
        backup.chmod(0o600)
        with self.assertRaises(rollout.RolloutError):
            rollout._verify_shadow(config)
        backup.write_bytes(original_backup)
        backup.chmod(0o600)
        identity_path.unlink()
        with self.assertRaises(rollout.RolloutError):
            rollout._verify_shadow(config)


    def test_shadow_executes_only_git_materialized_core_despite_info_exclude_shadow(self) -> None:
        repository = self.root / "release-source"
        repository.mkdir()
        self._git(repository, "init", "-q")
        package = repository / "core" / "activity_mvp"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "storage.py").write_text(
            "import sqlite3\n"
            "def ensure_ready(path):\n"
            "    conn = sqlite3.connect(path)\n"
            "    conn.execute(\"UPDATE meta SET value='31' WHERE key='schema_version'\")\n"
            "    conn.execute(\"CREATE TABLE materialized_proof(value TEXT NOT NULL)\")\n"
            "    conn.execute(\"INSERT INTO materialized_proof VALUES('git-blob')\")\n"
            "    conn.commit()\n"
            "    return conn\n",
            encoding="utf-8",
        )
        (repository / "core" / "pyproject.toml").write_text(
            '[project]\nname="pam-shadow-fixture"\nversion="0.4.0"\n',
            encoding="utf-8",
        )
        self._git(repository, "add", "core")
        self._git(repository, "commit", "-q", "-m", "materialized shadow fixture")
        revision = self._git(repository, "rev-parse", "HEAD")

        marker = self.root / "worktree-shadow-executed"
        ignored_shadow = repository / "core" / "sqlite3.py"
        ignored_shadow.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
            "raise RuntimeError('worktree import shadow executed')\n",
            encoding="utf-8",
        )
        exclude = repository / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\ncore/sqlite3.py\n")
        self.assertEqual(self._git(repository, "status", "--porcelain"), "")

        runtime_root = self.root / "shadow-runtime"
        config = replace(
            self._config(root=runtime_root),
            source_root=repository,
            expected_revision=revision,
        )
        config.live_database.parent.mkdir(parents=True, mode=0o700)
        config.live_database.parent.chmod(0o700)
        connection = sqlite3.connect(config.live_database)
        try:
            connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            connection.execute("INSERT INTO meta VALUES('schema_version','28')")
            connection.commit()
        finally:
            connection.close()
        config.live_database.chmod(0o600)
        agents = config.live_core / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        manifest = rollout._git_core_manifest(config)
        config = replace(
            config,
            expected_core_code_sha256=rollout._git_core_code_hash(config, manifest),
        )
        interpreter = {
            "program": sys.executable,
            "resolved_path": str(Path(sys.executable).resolve()),
            "sha256": "8" * 64,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "identifier": "python3",
            "team_identifier": "test",
        }
        identity = {
            "python_interpreter": interpreter,
            "live_agents_sha256": rollout._sha256_file(agents),
            "git_core_identity": rollout._git_core_manifest_identity(config, manifest),
        }
        source_backup: dict[str, object] = {}

        def sqlite_backup(source: Path, destination: Path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_connection = sqlite3.connect(source)
            destination_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()
            destination.chmod(0o600)
            probe = {
                "sha256": rollout._sha256_file(destination),
                "aggregate_counts": {},
                "explicit_confirmation_sha256": None,
            }
            source_backup.clear()
            source_backup.update(probe)
            return probe

        def sqlite_probe(path: Path, *, expected_schema: int, **_kwargs):
            conn = sqlite3.connect(path)
            try:
                schema = int(
                    conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
                )
                if schema != expected_schema:
                    raise rollout.RolloutError("fixture_schema_mismatch")
                if expected_schema == 31:
                    self.assertEqual(
                        conn.execute("SELECT value FROM materialized_proof").fetchone()[0],
                        "git-blob",
                    )
            finally:
                conn.close()
            return {
                "schema": schema,
                "sha256": rollout._sha256_file(path),
                "aggregate_counts": {},
                "explicit_confirmation_sha256": None,
            }

        with patch.object(
            rollout, "_assert_release_identity", return_value=identity
        ), patch.object(
            rollout, "_trusted_python_interpreter", return_value=interpreter
        ), patch.object(
            rollout,
            "_storage_headroom",
            return_value={"status": "ready", "phase": "shadow", "volumes": []},
        ), patch.object(
            rollout, "_sqlite_backup", side_effect=sqlite_backup
        ), patch.object(
            rollout, "_sqlite_probe", side_effect=sqlite_probe
        ), patch.object(
            rollout, "_set_initial_rollout_gates_off"
        ), patch.object(
            rollout,
            "_verify_migration_backup",
            side_effect=lambda _path: {**source_backup, "identity_verified": True},
        ), patch.object(
            rollout, "_assert_legacy_data_preserved"
        ):
            result = rollout.shadow(
                config,
                apply=True,
                confirmation=rollout.SHADOW_CONFIRMATION,
            )
            verified = rollout._verify_shadow(config)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(verified["status"], "verified")
        self.assertFalse(marker.exists())
        state = rollout._load_state(config)
        materialized_core = Path(state["shadow_migration_core"])
        self.assertEqual(materialized_core, config.workspace / "shadow" / "core")
        self.assertFalse((materialized_core / "sqlite3.py").exists())
        self.assertEqual(
            state["shadow_migration_core_git_identity"]["git"],
            identity["git_core_identity"],
        )


    def test_dry_run_never_creates_workspace_and_requires_explicit_apply_token(self) -> None:
        database = self.root / "live" / "core" / "data" / "activity.sqlite"
        self._prepare_schema28(database)
        config = self._config(database=database)
        with patch.object(
            rollout, "_assert_release_identity", return_value={"verified": True}
        ), patch.object(
            rollout,
            "_storage_headroom",
            return_value={"status": "ready", "phase": "shadow", "volumes": []},
        ):
            preview = rollout.shadow(config, apply=False, confirmation="")
            self.assertEqual(preview["mode"], "dry_run")
            self.assertFalse(config.workspace.exists())
            with self.assertRaisesRegex(rollout.RolloutError, "shadow_confirmation_required"):
                rollout.shadow(config, apply=True, confirmation="WRONG")
        self.assertFalse(config.workspace.exists())


    def test_active_handle_and_cross_volume_checks_fail_closed(self) -> None:
        database = self.root / "activity.sqlite"
        database.touch()
        active = subprocess.CompletedProcess(["lsof"], 0, stdout="123\n", stderr="")
        with patch.object(rollout, "_run", return_value=active):
            with self.assertRaisesRegex(rollout.RolloutError, "database_has_active_handles"):
                rollout._assert_database_not_open(database)
        with patch.object(
            rollout,
            "_nearest_existing_parent",
            side_effect=[Path("/left"), Path("/right")],
        ), patch.object(
            rollout.os,
            "stat",
            side_effect=[SimpleNamespace(st_dev=1), SimpleNamespace(st_dev=2)],
        ):
            self.assertFalse(rollout._same_volume(Path("/left/new"), Path("/right/new")))


    def test_install_and_rollback_order_bootout_before_database_or_new_core(self) -> None:
        install_source = inspect.getsource(rollout.install)
        rollback_source = inspect.getsource(rollout.rollback)
        self.assertLess(
            install_source.index("_bootout_label(config.service_label)"),
            install_source.index("_assert_database_not_open"),
        )
        self.assertLess(
            rollback_source.index("_bootout_label(config.service_label)"),
            rollback_source.index("_assert_database_not_open"),
        )
        self.assertNotIn("install_launch_agent", install_source)
        self.assertNotIn("install-launch-agent", install_source)
        entire_tool = ROLLOUT_PATH.read_text(encoding="utf-8")
        for forbidden_delete in (".unlink(", "shutil.rmtree(", "os.remove("):
            self.assertNotIn(forbidden_delete, entire_tool)


    def test_install_and_rollback_are_dry_run_by_default_in_cli(self) -> None:
        parser = rollout._parser()
        for command in ("shadow", "install", "rollback"):
            args = parser.parse_args(["--config", str(self.root / "config.json"), command])
            self.assertFalse(args.apply)
            self.assertEqual(args.confirm, "")


    def test_core_hash_matches_runtime_projector_and_detects_json_drift(self) -> None:
        self.assertEqual(
            rollout._core_code_hash(REPO_ROOT / "core"),
            _runtime_core_code_hash(REPO_ROOT / "core"),
        )
        fixture = self.root / "hash-core"
        (fixture / "activity_mvp").mkdir(parents=True)
        (fixture / "tools").mkdir()
        (fixture / "browser_extension").mkdir()
        (fixture / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        resource = fixture / "activity_mvp" / "contract.json"
        resource.write_text('{"version":1}\n', encoding="utf-8")
        first = rollout._core_code_hash(fixture)
        self.assertEqual(first, _runtime_core_code_hash(fixture))
        differently_named = self.root / ".core.v0.4.0.incoming.fixture"
        shutil.copytree(fixture, differently_named)
        self.assertEqual(first, rollout._core_code_hash(differently_named))
        self.assertEqual(first, _runtime_core_code_hash(differently_named))
        resource.write_text('{"version":2}\n', encoding="utf-8")
        self.assertNotEqual(first, rollout._core_code_hash(fixture))


    def test_git_materialized_core_hash_is_location_independent_across_all_projectors(self) -> None:
        revision = self._git(REPO_ROOT, "rev-parse", "HEAD")
        config = replace(
            self._config(root=self.root / "runtime-hash"),
            source_root=REPO_ROOT,
            expected_revision=revision,
        )
        manifest = rollout._git_core_manifest(config)
        expected = rollout._git_core_code_hash(config, manifest)
        config = replace(config, expected_core_code_sha256=expected)
        incoming = self.root / ".core.v0.4.0.incoming.fixture"
        # Avoid relying on a canonical basename: production uses a unique
        # hidden incoming directory before the durable cutover rename.
        rollout._copy_candidate_core(config, incoming)
        self.assertNotEqual(incoming.name, "core")
        self.assertEqual(rollout._core_code_hash(incoming), expected)
        self.assertEqual(_runtime_core_code_hash(incoming), expected)


    def test_remote_ci_requires_matching_private_repository(self) -> None:
        config = self._config()

        def git_output(_config, *args):
            if args == ("remote", "get-url", "origin"):
                return "git@github.com:owner/private-repo.git"
            if args[:2] == ("rev-parse", f"{config.release_candidate_tag}^{{commit}}"):
                return config.expected_revision
            raise AssertionError(args)

        successful_run = {
            "head_sha": config.expected_revision,
            "head_branch": config.release_branch,
            "path": ".github/workflows/ci.yml",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
        }
        successful_jobs = {
            "jobs": [
                {"name": name, "status": "completed", "conclusion": "success"}
                for name in (
                    "core",
                    "Swift tests (Xcode 15.4)",
                    "Swift tests (Xcode 16.4)",
                    "app-build",
                )
            ]
        }

        def command_for(private: bool, *, workflow_path: str = ".github/workflows/ci.yml"):
            run_payload = {**successful_run, "path": workflow_path}
            payloads = iter(
                (
                    {"private": private, "full_name": "owner/private-repo"},
                    {"object": {"type": "commit", "sha": config.expected_revision}},
                    {"object": {"type": "commit", "sha": config.expected_revision}},
                    run_payload,
                    successful_jobs,
                )
            )

            def run(_config, _command, **_kwargs):
                return subprocess.CompletedProcess(
                    _command,
                    0,
                    stdout=json.dumps(next(payloads)),
                    stderr="",
                )

            return run

        with patch.object(rollout, "_git_output", side_effect=git_output), patch.object(
            rollout, "_gh_run", side_effect=command_for(False)
        ):
            with self.assertRaisesRegex(rollout.RolloutError, "github_repository_not_private"):
                rollout._verify_remote_ci(config)
        with patch.object(rollout, "_git_output", side_effect=git_output), patch.object(
            rollout, "_gh_run", side_effect=command_for(True)
        ):
            self.assertEqual(rollout._verify_remote_ci(config)["conclusion"], "success")
        with patch.object(rollout, "_git_output", side_effect=git_output), patch.object(
            rollout,
            "_gh_run",
            side_effect=command_for(
                True,
                workflow_path=".github/workflows/not-the-release-gate.yml",
            ),
        ):
            with self.assertRaisesRegex(rollout.RolloutError, "github_ci_run_mismatch"):
                rollout._verify_remote_ci(config)


    def test_install_fault_injection_restores_old_runtime_at_every_boundary(self) -> None:
        boundaries = (
            "static_candidate_and_plists_preserved",
            "app_stopped",
            "core_stopped_directly",
            "shadow_stopped",
            "database_family_has_no_handles",
            "database_writer_barrier_acquired",
            "phase2_ui_disabled",
            "staging_schema31_verified",
            "old_core_preserved",
            "new_core_activated",
            "old_app_preserved",
            "new_app_activated",
            "canonical_candidate_verified_before_bootstrap",
        )
        original_record = rollout._record_transaction_step
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                case_root = self.root / f"install-fault-{index}"
                case_root.mkdir()
                config, services, ui = self._prepare_transaction_fixture(case_root)
                with ExitStack() as stack:
                    self._enter_transaction_mocks(stack, config, services, ui)

                    def inject(_config, journal, step, *, target=boundary):
                        original_record(_config, journal, step)
                        if step == target:
                            raise rollout.RolloutError("injected_install_fault")

                    stack.enter_context(
                        patch.object(rollout, "_record_transaction_step", side_effect=inject)
                    )
                    with self.assertRaisesRegex(
                        rollout.RolloutError,
                        "install_transaction_failed_after_",
                    ):
                        rollout.install(
                            config,
                            apply=True,
                            confirmation=rollout.INSTALL_CONFIRMATION,
                        )
                state = rollout._load_state(config)
                self.assertEqual(state["phase"], "install_failed_rolled_back")
                self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
                self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
                self.assertTrue(services[config.service_label])
                self.assertTrue(services[config.shadow_launch_agent_label])
                self.assertEqual(ui, {"exists": True, "value": True})


    def test_final_installed_state_write_failure_restores_old_runtime(self) -> None:
        case_root = self.root / "install-final-state-write-fault"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        original_write_state = rollout._write_state

        def fail_only_final_installed_state(_config, state):
            if state.get("phase") == "installed":
                raise rollout.RolloutError("injected_final_state_fsync_failure")
            return original_write_state(_config, state)

        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            stack.enter_context(
                patch.object(
                    rollout,
                    "_write_state",
                    side_effect=fail_only_final_installed_state,
                )
            )
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "install_transaction_failed_after_install_complete",
            ):
                rollout.install(
                    config,
                    apply=True,
                    confirmation=rollout.INSTALL_CONFIRMATION,
                )

        state = rollout._load_state(config)
        self.assertEqual(state["phase"], "install_failed_rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_install_compensates_when_core_rename_succeeds_then_parent_fsync_fails(self) -> None:
        case_root = self.root / "install-rename-parent-fsync-fault"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        state = rollout._load_state(config)
        artifacts = rollout._rollout_artifacts(config, state["run_id"])
        durable_rename = rollout._durable_rename
        injected = False

        def rename_then_fail(source: Path, destination: Path) -> None:
            nonlocal injected
            durable_rename(source, destination)
            if (
                not injected
                and source == config.live_core
                and destination == artifacts.old_core
            ):
                injected = True
                raise rollout.RolloutError("durability_directory_fsync_failed")

        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            stack.enter_context(
                patch.object(
                    rollout,
                    "_durable_rename",
                    side_effect=rename_then_fail,
                )
            )
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "install_transaction_failed_after_",
            ):
                rollout.install(
                    config,
                    apply=True,
                    confirmation=rollout.INSTALL_CONFIRMATION,
                )

        self.assertTrue(injected)
        recovered = rollout._load_state(config)
        self.assertEqual(recovered["phase"], "install_failed_rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_install_rejects_core_injection_after_activation_before_bootstrap(self) -> None:
        case_root = self.root / "install-post-activation-injection"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        state = rollout._load_state(config)
        artifacts = rollout._rollout_artifacts(config, state["run_id"])
        durable_rename = rollout._durable_rename
        injected = False

        def rename_then_inject(source: Path, destination: Path) -> None:
            nonlocal injected
            durable_rename(source, destination)
            if (
                not injected
                and source == artifacts.incoming_core
                and destination == config.live_core
            ):
                shadow = config.live_core / "sqlite3.py"
                shadow.write_text(
                    "raise RuntimeError('must never execute')\n",
                    encoding="utf-8",
                )
                shadow.chmod(0o644)
                injected = True

        def reject_injected_live_core(core_root: Path, _expected: object) -> None:
            if core_root == config.live_core and (core_root / "sqlite3.py").exists():
                raise rollout.RolloutError("materialized_core_identity_mismatch")

        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            stack.enter_context(
                patch.object(
                    rollout,
                    "_durable_rename",
                    side_effect=rename_then_inject,
                )
            )
            stack.enter_context(
                patch.object(
                    rollout,
                    "_assert_materialized_core_identity",
                    side_effect=reject_injected_live_core,
                )
            )
            bootstrap = stack.enter_context(patch.object(rollout, "_bootstrap"))
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "install_transaction_failed_after_",
            ):
                rollout.install(
                    config,
                    apply=True,
                    confirmation=rollout.INSTALL_CONFIRMATION,
                )

        self.assertTrue(injected)
        bootstrap.assert_not_called()
        recovered = rollout._load_state(config)
        self.assertEqual(recovered["phase"], "install_failed_rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertTrue((artifacts.failed_core / "sqlite3.py").is_file())
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_rollback_completes_forward_when_quarantine_rename_succeeds_then_fsync_fails(self) -> None:
        case_root = self.root / "rollback-rename-parent-fsync-fault"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            state = rollout._load_state(config)
            artifacts = rollout._rollout_artifacts(config, state["run_id"])
            durable_rename = rollout._durable_rename
            injected = False

            def quarantine_then_fail(source: Path, destination: Path) -> None:
                nonlocal injected
                durable_rename(source, destination)
                if (
                    not injected
                    and source == config.live_core
                    and destination == artifacts.failed_core
                ):
                    injected = True
                    raise rollout.RolloutError("durability_directory_fsync_failed")

            stack.enter_context(
                patch.object(
                    rollout,
                    "_durable_rename",
                    side_effect=quarantine_then_fail,
                )
            )
            result = rollout.rollback(
                config,
                apply=True,
                confirmation=rollout.ROLLBACK_CONFIRMATION,
            )

        self.assertTrue(injected)
        recovered = rollout._load_state(config)
        self.assertEqual(result["status"], "rolled_back_after_failure")
        self.assertEqual(recovered["phase"], "rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertTrue(services["app"])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_rollback_fault_injection_compensates_to_consistent_release(self) -> None:
        boundaries = (
            "rollback_app_stopped",
            "rollback_core_stopped_directly",
            "rollback_shadow_stopped",
            "rollback_database_family_has_no_handles",
            "release_features_disabled_for_quarantine",
            "release_core_quarantined",
            "rollback_core_activated",
            "rollback_schema28_database_ready",
            "release_app_quarantined",
            "rollback_app_activated",
            "release_plist_quarantined",
            "rollback_plist_activated",
            "release_shadow_plist_quarantined",
            "rollback_shadow_plist_activated",
            "original_ui_preference_restored",
            "rollback_identity_verified_before_bootstrap",
        )
        original_record = rollout._record_transaction_step
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                case_root = self.root / f"rollback-fault-{index}"
                case_root.mkdir()
                config, services, ui = self._prepare_transaction_fixture(case_root)
                sealed_database_hash = rollout._sha256_file(config.live_database)
                with ExitStack() as stack:
                    self._enter_transaction_mocks(stack, config, services, ui)
                    rollout.install(
                        config,
                        apply=True,
                        confirmation=rollout.INSTALL_CONFIRMATION,
                    )

                    def inject(_config, journal, step, *, target=boundary):
                        original_record(_config, journal, step)
                        if step == target and not getattr(inject, "injected", False):
                            inject.injected = True
                            raise rollout.RolloutError("injected_rollback_fault")

                    stack.enter_context(
                        patch.object(rollout, "_record_transaction_step", side_effect=inject)
                    )
                    result = rollout.rollback(
                        config,
                        apply=True,
                        confirmation=rollout.ROLLBACK_CONFIRMATION,
                    )
                state = rollout._load_state(config)
                self.assertEqual(result["status"], "rolled_back_after_failure")
                self.assertEqual(state["phase"], "rolled_back")
                self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
                self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
                self.assertTrue(services[config.service_label])
                self.assertTrue(services[config.shadow_launch_agent_label])
                self.assertTrue(services["app"])
                self.assertEqual(ui, {"exists": True, "value": True})
                self.assertEqual(rollout._sha256_file(config.live_database), sealed_database_hash)
                restored = sqlite3.connect(f"file:{config.live_database}?mode=ro", uri=True)
                try:
                    self.assertEqual(
                        restored.execute(
                            "SELECT value FROM settings WHERE key='legacy_setting'"
                        ).fetchone()[0],
                        "preserved",
                    )
                finally:
                    restored.close()


    def test_process_control_exceptions_restore_safely_before_original_reraise(self) -> None:
        original_record = rollout._record_transaction_step
        cases = (
            ("install", "new_core_activated"),
            ("rollback", "rollback_core_activated"),
        )
        failures: tuple[BaseException, ...] = (
            KeyboardInterrupt("injected_rollout_interrupt"),
            SystemExit(91),
        )
        for operation_index, (operation, boundary) in enumerate(cases):
            for failure_index, failure in enumerate(failures):
                with self.subTest(operation=operation, exception=type(failure).__name__):
                    case_root = self.root / (
                        f"process-control-{operation_index}-{failure_index}"
                    )
                    case_root.mkdir()
                    config, services, ui = self._prepare_transaction_fixture(case_root)
                    with ExitStack() as stack:
                        self._enter_transaction_mocks(stack, config, services, ui)
                        if operation == "rollback":
                            rollout.install(
                                config,
                                apply=True,
                                confirmation=rollout.INSTALL_CONFIRMATION,
                            )

                        def interrupt_once(_config, journal, step, *, target=boundary):
                            original_record(_config, journal, step)
                            if step == target and not getattr(
                                interrupt_once,
                                "injected",
                                False,
                            ):
                                interrupt_once.injected = True
                                raise failure

                        with patch.object(
                            rollout,
                            "_record_transaction_step",
                            side_effect=interrupt_once,
                        ):
                            with self.assertRaises(type(failure)) as raised:
                                if operation == "install":
                                    rollout.install(
                                        config,
                                        apply=True,
                                        confirmation=rollout.INSTALL_CONFIRMATION,
                                    )
                                else:
                                    rollout.rollback(
                                        config,
                                        apply=True,
                                        confirmation=rollout.ROLLBACK_CONFIRMATION,
                                    )

                    self.assertIs(raised.exception, failure)
                    self.assertEqual(raised.exception.args, failure.args)
                    state = rollout._load_state(config)
                    self.assertEqual(
                        state["phase"],
                        "install_failed_rolled_back" if operation == "install" else "rolled_back",
                    )
                    self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
                    self.assertEqual(
                        (config.installed_app / "marker.txt").read_text(),
                        "old-app",
                    )
                    self.assertTrue(services[config.service_label])
                    self.assertTrue(services[config.shadow_launch_agent_label])
                    self.assertTrue(services["app"])
                    self.assertEqual(ui, {"exists": True, "value": True})


    def test_incomplete_rollback_process_control_failure_is_durably_failed_closed(self) -> None:
        failures: tuple[BaseException, ...] = (
            KeyboardInterrupt("injected_incomplete_rollback_interrupt"),
            SystemExit(92),
        )
        for index, failure in enumerate(failures):
            with self.subTest(exception=type(failure).__name__):
                case_root = self.root / f"incomplete-process-control-{index}"
                case_root.mkdir()
                config, _services, _ui = self._prepare_transaction_fixture(case_root)
                state = rollout._load_state(config)
                state["phase"] = "rollback_prepared"
                rollout._write_state(config, state)

                def durable_fail_closed(_config, journal):
                    journal["phase"] = "rollback_failed_recovery_required"
                    journal["active_step"] = "rollback_fail_closed"
                    rollout._write_state(_config, journal)

                with patch.object(rollout, "_assert_state_artifacts"), patch.object(
                    rollout,
                    "_complete_rollback_forward",
                    side_effect=failure,
                ), patch.object(
                    rollout,
                    "_fail_closed_rollback",
                    side_effect=durable_fail_closed,
                ), patch.object(
                    rollout,
                    "_storage_headroom",
                    return_value={"status": "ready", "volumes": []},
                ):
                    with self.assertRaises(type(failure)) as raised:
                        rollout.rollback(
                            config,
                            apply=True,
                            confirmation=rollout.ROLLBACK_CONFIRMATION,
                        )

                self.assertIs(raised.exception, failure)
                self.assertEqual(raised.exception.args, failure.args)
                persisted = rollout._load_state(config)
                self.assertEqual(
                    persisted["phase"],
                    "rollback_failed_recovery_required",
                )
                self.assertEqual(persisted["active_step"], "rollback_fail_closed")


    def test_rollback_succeeds_after_core_and_ui_features_are_enabled(self) -> None:
        case_root = self.root / "enabled-rollback"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        gate_writes: list[tuple[str, bool]] = []
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            ui.update({"exists": True, "value": True})
            stack.enter_context(
                patch.object(
                    rollout,
                    "_verify_release_before_rollback",
                    return_value={
                        "current_quarantine_paths": {
                            "core_plist_present": True,
                            "shadow_plist_present": True,
                        },
                        "release_gate_status": {"context_core_gate": "true"},
                        "release_core_identity_status": "valid",
                        "release_ui_preference": {"exists": True, "value": True},
                        "release_app_was_running": True,
                        "release_core_was_loaded": True,
                        "release_shadow_was_loaded": False,
                    },
                )
            )

            def write_gate(_database, value, *, required):
                gate_writes.append((value, required))
                return "applied"

            stack.enter_context(
                patch.object(
                    rollout,
                    "_write_context_core_gate_for_rollback",
                    side_effect=write_gate,
                )
            )
            result = rollout.rollback(
                config,
                apply=True,
                confirmation=rollout.ROLLBACK_CONFIRMATION,
            )
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(gate_writes, [("false", False)])
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual(ui, {"exists": True, "value": True})
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertTrue(services["app"])


    def test_corrupt_release_full_rollback_uses_real_preflight_and_sealed_schema28(self) -> None:
        case_root = self.root / "corrupt-full-rollback"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        sealed_hash = rollout._sha256_file(config.live_database)
        real_preflight = rollout._verify_release_before_rollback
        real_gate_write = rollout._write_context_core_gate_for_rollback
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            self.assertNotEqual(rollout._sha256_file(config.live_database), sealed_hash)
            ui.update({"exists": True, "value": True})
            stack.enter_context(
                patch.object(
                    rollout,
                    "_verify_release_before_rollback",
                    side_effect=real_preflight,
                )
            )
            stack.enter_context(
                patch.object(
                    rollout,
                    "_write_context_core_gate_for_rollback",
                    side_effect=real_gate_write,
                )
            )
            result = rollout.rollback(
                config,
                apply=True,
                confirmation=rollout.ROLLBACK_CONFIRMATION,
            )
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertEqual(rollout._sha256_file(config.live_database), sealed_hash)
        conn = sqlite3.connect(f"file:{config.live_database}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "28")
            self.assertEqual(
                conn.execute("SELECT value FROM settings WHERE key='legacy_setting'").fetchone()[0],
                "preserved",
            )
        finally:
            conn.close()
        self.assertEqual(config.launch_agent_plist.read_text(), config.service_label)
        self.assertEqual(
            config.shadow_launch_agent_plist.read_text(),
            config.shadow_launch_agent_label,
        )
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertTrue(services["app"])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_rollback_quarantines_static_core_injection_and_data_symlink_without_following(self) -> None:
        for corruption in ("static_import_shadow", "data_symlink"):
            with self.subTest(corruption=corruption):
                case_root = self.root / f"rollback-corruption-{corruption}"
                case_root.mkdir()
                config, services, ui = self._prepare_transaction_fixture(case_root)
                real_preflight = rollout._verify_release_before_rollback
                real_materialized = rollout._materialized_core_identity
                real_assert_materialized = rollout._assert_materialized_core_identity
                external = case_root / "external-target"
                external.mkdir(mode=0o700)
                external_marker = external / "must-not-change"
                external_marker.write_text("outside", encoding="utf-8")
                external_marker.chmod(0o600)

                with ExitStack() as stack:
                    self._enter_transaction_mocks(stack, config, services, ui)
                    rollout.install(
                        config,
                        apply=True,
                        confirmation=rollout.INSTALL_CONFIRMATION,
                    )
                    state = rollout._load_state(config)
                    artifacts = rollout._rollout_artifacts(config, state["run_id"])
                    if corruption == "static_import_shadow":
                        injected = config.live_core / "sqlite3.py"
                        injected.write_text(
                            "raise RuntimeError('must never execute')\n",
                            encoding="utf-8",
                        )
                        injected.chmod(0o644)
                    else:
                        live_data = config.live_core / "data"
                        displaced_data = config.live_core / "data-corrupt-preserved"
                        live_data.rename(displaced_data)
                        live_data.symlink_to(external, target_is_directory=True)

                    with patch.object(
                        rollout,
                        "_materialized_core_identity",
                        side_effect=real_materialized,
                    ), patch.object(
                        rollout,
                        "_assert_materialized_core_identity",
                        side_effect=real_assert_materialized,
                    ), patch.object(
                        rollout,
                        "_verify_release_before_rollback",
                        side_effect=real_preflight,
                    ):
                        preflight_identity = rollout._verify_release_before_rollback(
                            config,
                            state,
                            artifacts,
                        )
                    self.assertEqual(
                        preflight_identity["release_core_identity_status"],
                        "invalid_quarantine_only",
                    )
                    stack.enter_context(
                        patch.object(
                            rollout,
                            "_verify_release_before_rollback",
                            return_value=preflight_identity,
                        )
                    )
                    result = rollout.rollback(
                        config,
                        apply=True,
                        confirmation=rollout.ROLLBACK_CONFIRMATION,
                    )

                self.assertEqual(result["status"], "rolled_back")
                self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
                self.assertEqual(external_marker.read_text(encoding="utf-8"), "outside")
                self.assertEqual(rollout._mode(external_marker), 0o600)
                self.assertTrue(artifacts.failed_core.is_dir())
                if corruption == "static_import_shadow":
                    self.assertTrue((artifacts.failed_core / "sqlite3.py").is_file())
                else:
                    quarantined_link = artifacts.failed_core / "data"
                    self.assertTrue(quarantined_link.is_symlink())
                    self.assertEqual(quarantined_link.resolve(), external.resolve())


    def test_enabled_schema31_full_rollback_uses_real_preflight_and_preserves_old_db(self) -> None:
        case_root = self.root / "enabled-real-preflight"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        sealed_hash = rollout._sha256_file(config.live_database)
        real_preflight = rollout._verify_release_before_rollback
        real_gate_write = rollout._write_context_core_gate_for_rollback
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            # Recreate a valid schema-31 candidate with the governed Core feature
            # explicitly enabled. Its required migration-backup receipt is absent,
            # so normal candidate acceptance would fail, but rollback must proceed.
            config.live_database.write_bytes(b"")
            conn = ensure_ready(config.live_database)
            try:
                conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
                conn.execute("UPDATE meta SET value='31' WHERE key='schema_version'")
                conn.execute(
                    "UPDATE settings SET value='true' WHERE key=?",
                    (rollout.CONTEXT_CORE_GATE,),
                )
                for key in rollout.PERMANENT_SAFETY_GATES:
                    conn.execute("UPDATE settings SET value='false' WHERE key=?", (key,))
                conn.commit()
            finally:
                conn.close()
            config.live_database.chmod(0o600)
            self.assertEqual(
                rollout._read_release_gate_status_for_rollback(config.live_database)[
                    "context_core_gate"
                ],
                "true",
            )
            ui.update({"exists": True, "value": True})
            stack.enter_context(
                patch.object(
                    rollout,
                    "_verify_release_before_rollback",
                    side_effect=real_preflight,
                )
            )
            stack.enter_context(
                patch.object(
                    rollout,
                    "_write_context_core_gate_for_rollback",
                    side_effect=real_gate_write,
                )
            )
            result = rollout.rollback(
                config,
                apply=True,
                confirmation=rollout.ROLLBACK_CONFIRMATION,
            )
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertEqual(rollout._sha256_file(config.live_database), sealed_hash)
        conn = sqlite3.connect(f"file:{config.live_database}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "28")
            self.assertEqual(
                conn.execute("SELECT value FROM settings WHERE key='legacy_setting'").fetchone()[0],
                "preserved",
            )
        finally:
            conn.close()
        self.assertEqual(config.launch_agent_plist.read_text(), config.service_label)
        self.assertEqual(
            config.shadow_launch_agent_plist.read_text(),
            config.shadow_launch_agent_label,
        )
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertTrue(services["app"])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_missing_current_app_and_core_restore_from_sealed_old_artifacts(self) -> None:
        case_root = self.root / "missing-current-full-rollback"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        sealed_hash = rollout._sha256_file(config.live_database)
        real_preflight = rollout._verify_release_before_rollback
        absent_core = case_root / "externally-displaced-v040-core"
        absent_app = case_root / "externally-displaced-v040-app"
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            rollout._durable_rename(config.live_core, absent_core)
            rollout._durable_rename(config.installed_app, absent_app)
            self.assertFalse(config.live_core.exists())
            self.assertFalse(config.installed_app.exists())
            stack.enter_context(
                patch.object(
                    rollout,
                    "_verify_release_before_rollback",
                    side_effect=real_preflight,
                )
            )
            result = rollout.rollback(
                config,
                apply=True,
                confirmation=rollout.ROLLBACK_CONFIRMATION,
            )

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((config.live_core / "marker.txt").read_text(), "old-core")
        self.assertEqual((config.installed_app / "marker.txt").read_text(), "old-app")
        self.assertEqual(rollout._sha256_file(config.live_database), sealed_hash)
        self.assertTrue(absent_core.exists())
        self.assertTrue(absent_app.exists())
        self.assertTrue(services[config.service_label])
        self.assertTrue(services[config.shadow_launch_agent_label])
        self.assertTrue(services["app"])
        self.assertEqual(ui, {"exists": True, "value": True})


    def test_rollback_double_failure_stays_stopped_and_recovery_required(self) -> None:
        case_root = self.root / "rollback-fail-closed"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        original_record = rollout._record_transaction_step
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )

            def inject(_config, journal, step):
                original_record(_config, journal, step)
                if step == "release_core_quarantined":
                    raise rollout.RolloutError("injected_rollback_fault")

            stack.enter_context(
                patch.object(rollout, "_record_transaction_step", side_effect=inject)
            )
            stack.enter_context(
                patch.object(
                    rollout,
                    "_complete_rollback_forward",
                    side_effect=rollout.RolloutError("injected_forward_failure"),
                )
            )
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "rollback_failed_recovery_required",
            ):
                rollout.rollback(
                    config,
                    apply=True,
                    confirmation=rollout.ROLLBACK_CONFIRMATION,
                )
        state = rollout._load_state(config)
        self.assertEqual(state["phase"], "rollback_failed_recovery_required")
        self.assertFalse(services[config.service_label])
        self.assertFalse(services[config.shadow_launch_agent_label])
        self.assertFalse(services["app"])
        self.assertEqual(ui, {"exists": True, "value": False})
        self.assertTrue(Path(state["old_core"]).exists())
        self.assertTrue(Path(state["failed_core"]).exists())


    def test_rollback_respects_original_unloaded_main_and_shadow_states(self) -> None:
        case_root = self.root / "unloaded-rollback"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            state = rollout._load_state(config)
            state["core_was_loaded"] = False
            state["shadow_was_loaded"] = False
            rollout._write_state(config, state)
            result = rollout.rollback(
                config,
                apply=True,
                confirmation=rollout.ROLLBACK_CONFIRMATION,
            )
        self.assertEqual(result["status"], "rolled_back")
        self.assertFalse(services[config.service_label])
        self.assertFalse(services[config.shadow_launch_agent_label])


    def test_shadow_runtime_requires_positive_pid_and_canonical_paths(self) -> None:
        config = self._config()
        identity = {
            "program": "/trusted/python3",
            "arguments": [
                "/trusted/python3", "-m", "activity_mvp", "--db", "/shadow/db",
                "task-sources", "serve-receipts", "--socket", "/shadow/socket",
            ],
            "working_directory": "/shadow/core",
        }
        missing_pid = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(identity, pid=None),
            stderr="",
        )
        with patch.object(
            rollout,
            "_verify_shadow_launch_agent_path",
            return_value=identity,
        ), patch.object(rollout, "_run", return_value=missing_pid), patch.object(
            rollout.time,
            "sleep",
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "shadow_launch_agent_not_running",
            ):
                rollout._verify_shadow_runtime(config)

        wrong_path = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(
                identity,
                pid=99,
                working_directory=str(self.root / "wrong-shadow"),
            ),
            stderr="",
        )
        with patch.object(
            rollout,
            "_verify_shadow_launch_agent_path",
            return_value=identity,
        ), patch.object(rollout, "_run", return_value=wrong_path) as runner:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_runtime_identity_mismatch",
            ):
                rollout._verify_shadow_runtime(config)
        self.assertEqual(runner.call_count, 1)


    def test_main_and_shadow_runtime_retry_starting_state_until_pid_exists(self) -> None:
        config = self._config()
        shadow_identity = {
            "program": "/trusted/python3",
            "arguments": [
                "/trusted/python3", "-m", "activity_mvp", "--db", "/shadow/db",
                "task-sources", "serve-receipts", "--socket", "/shadow/socket",
            ],
            "working_directory": str(self.root / "shadow-core"),
            "database": str(self.root / "shadow-data" / "activity.sqlite"),
        }
        shadow_starting = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(shadow_identity, pid=None),
            stderr="",
        )
        shadow_ready = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(shadow_identity, pid=456),
            stderr="",
        )
        with patch.object(
            rollout,
            "_verify_shadow_launch_agent_path",
            return_value=shadow_identity,
        ), patch.object(
            rollout,
            "_run",
            side_effect=[shadow_starting, shadow_ready, shadow_ready],
        ), patch.object(rollout, "_verify_pid_os_identity"), patch.object(
            rollout.time, "sleep"
        ):
            shadow = rollout._verify_shadow_runtime(config)
        self.assertEqual(shadow["pid"], 456)
        self.assertTrue(shadow["independent_database"])

        main_identity = {
            "program": "/trusted/python3",
            "arguments": [
                "/trusted/python3", "-m", "activity_mvp", "--db",
                str(config.live_database), "run-daemon",
            ],
            "working_directory": str(config.live_core),
        }
        main_starting = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(main_identity, pid=None),
            stderr="",
        )
        main_ready = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(main_identity, pid=789),
            stderr="",
        )
        with patch.object(
            rollout,
            "_verify_core_launch_agent_path",
            return_value=main_identity,
        ), patch.object(
            rollout,
            "_run",
            side_effect=[main_starting, main_ready, main_ready],
        ), patch.object(rollout, "_verify_pid_os_identity"), patch.object(
            rollout.time, "sleep"
        ):
            main = rollout._verify_launch_agent(config)
        self.assertEqual(main["pid"], 789)
        self.assertTrue(main["canonical_core"])

        wrong_main = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout=self._launchctl_output(
                main_identity,
                pid=789,
                working_directory=str(self.root / "wrong-core"),
            ),
            stderr="",
        )
        with patch.object(
            rollout,
            "_verify_core_launch_agent_path",
            return_value=main_identity,
        ), patch.object(rollout, "_run", return_value=wrong_main) as runner:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_runtime_identity_mismatch",
            ):
                rollout._verify_launch_agent(config)
        self.assertEqual(runner.call_count, 1)


    def test_force_stop_falls_back_to_exact_pid_when_quit_fails(self) -> None:
        config = self._config()
        running = {321}
        killed: list[tuple[int, signal.Signals]] = []

        def app_pids(_config):
            return set(running)

        def kill(pid, requested_signal):
            killed.append((pid, requested_signal))
            running.discard(pid)

        with patch.object(
            rollout,
            "_quit_app",
            side_effect=rollout.RolloutError("app_quit_failed"),
        ), patch.object(rollout, "_app_pids", side_effect=app_pids), patch.object(
            rollout,
            "_verify_app_pid_for_signal",
        ), patch.object(
            rollout.os,
            "kill",
            side_effect=kill,
        ), patch.object(rollout.time, "sleep"):
            rollout._stop_app(
                config,
                force=True,
                allowed_stale_snapshots=(("/fixture", 1, 2, 3, 4, "a" * 64),),
            )
        self.assertEqual(killed, [(321, signal.SIGTERM)])
        self.assertFalse(running)


    def test_force_stop_accepts_only_the_recorded_stale_executable_snapshot(self) -> None:
        config = self._config()
        stale = ("/sealed/old-app", 11, 22, 33, 44, "a" * 64)
        running = {654}
        verified: list[tuple[int, tuple[object, ...], bool, bool]] = []
        killed: list[tuple[int, signal.Signals]] = []

        def verify(
            _config,
            pid: int,
            expected: tuple[object, ...],
            *,
            require_current: bool,
            require_path: bool,
        ) -> None:
            verified.append((pid, expected, require_current, require_path))
            if expected != stale:
                raise rollout.RolloutError("installed_app_process_image_mismatch")

        def kill(pid: int, requested_signal: signal.Signals) -> None:
            killed.append((pid, requested_signal))
            running.discard(pid)

        with patch.object(
            rollout,
            "_quit_app",
            side_effect=rollout.RolloutError("app_quit_failed"),
        ), patch.object(
            rollout,
            "_app_pids",
            side_effect=lambda _config: set(running),
        ), patch.object(
            rollout,
            "_verify_app_pid_executable",
            side_effect=verify,
        ), patch.object(
            rollout.os,
            "kill",
            side_effect=kill,
        ), patch.object(rollout.time, "sleep"):
            rollout._stop_app(
                config,
                force=True,
                allowed_stale_snapshots=(stale,),
            )

        self.assertEqual(verified, [(654, stale, False, False)])
        self.assertEqual(killed, [(654, signal.SIGTERM)])
        self.assertFalse(running)


    def test_context_gate_writer_never_follows_database_symlink(self) -> None:
        target = self.root / "outside.sqlite"
        target.write_bytes(b"outside")
        link = self.root / "activity.sqlite"
        link.symlink_to(target)
        with patch.object(rollout.sqlite3, "connect") as connect:
            self.assertEqual(
                rollout._write_context_core_gate_for_rollback(
                    link,
                    "false",
                    required=False,
                ),
                "unavailable",
            )
        connect.assert_not_called()
        self.assertEqual(target.read_bytes(), b"outside")


    def test_schema31_probe_separates_context_feature_from_permanent_safety_gates(self) -> None:
        database = self.root / "schema31.sqlite"
        conn = ensure_ready(database)
        try:
            conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
            conn.execute("UPDATE meta SET value='31' WHERE key='schema_version'")
            for key in rollout.PERMANENT_SAFETY_GATES:
                conn.execute("UPDATE settings SET value='false' WHERE key=?", (key,))
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?, 'true', datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value='true'",
                (rollout.CONTEXT_CORE_GATE,),
            )
            conn.commit()
        finally:
            conn.close()
        database.chmod(0o600)
        with self.assertRaisesRegex(rollout.RolloutError, "context_core_gate_enabled"):
            rollout._sqlite_probe(database, expected_schema=31)
        self.assertEqual(
            rollout._sqlite_probe(
                database,
                expected_schema=31,
                require_context_disabled=False,
            )["gates"][rollout.CONTEXT_CORE_GATE],
            "true",
        )
        conn = sqlite3.connect(database)
        try:
            conn.execute(
                "UPDATE settings SET value='true' WHERE key=?",
                (rollout.PERMANENT_SAFETY_GATES[0],),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(rollout.RolloutError, "permanent_safety_gate_enabled"):
            rollout._sqlite_probe(
                database,
                expected_schema=31,
                require_context_disabled=False,
            )


    def test_rollback_preflight_tolerates_corrupt_current_database_but_checks_old_artifacts(self) -> None:
        case_root = self.root / "corrupt-current"
        case_root.mkdir()
        config, _services, _ui = self._prepare_transaction_fixture(case_root)
        state = rollout._load_state(config)
        artifacts = rollout._rollout_artifacts(config, state["run_id"])
        state.update(rollout._artifact_state(artifacts))
        state["rollback_identity"] = {"sealed": True}
        config.live_database.write_bytes(b"not a sqlite database")
        config.live_database.chmod(0o600)
        with patch.object(
            rollout,
            "_verify_old_release_artifacts",
            return_value={"sealed": True},
        ), patch.object(
            rollout,
            "_assert_materialized_core_identity",
        ), patch.object(rollout, "_read_ui_flag", return_value={"exists": True, "value": True}), patch.object(
            rollout,
            "_launch_agent_loaded",
            return_value=False,
        ), patch.object(
            rollout,
            "_app_running",
            return_value=False,
        ):
            evidence = rollout._verify_release_before_rollback(config, state, artifacts)
        self.assertEqual(evidence["release_gate_status"]["context_core_gate"], "unknown")


    def test_rollback_rejects_plist_cross_volume_before_mutation(self) -> None:
        case_root = self.root / "plist-volume"
        case_root.mkdir()
        config, _services, _ui = self._prepare_transaction_fixture(case_root)
        state = rollout._load_state(config)
        artifacts = rollout._rollout_artifacts(config, state["run_id"])
        state.update(rollout._artifact_state(artifacts))
        state["rollback_identity"] = {"sealed": True}
        with patch.object(rollout, "_same_volume", return_value=False), patch.object(
            rollout,
            "_verify_old_release_artifacts",
            return_value={"sealed": True},
        ), patch.object(
            rollout,
            "_assert_materialized_core_identity",
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "rollback_plist_rename_cross_volume",
            ):
                rollout._verify_release_before_rollback(config, state, artifacts)


    def test_release_paths_use_closed_disjoint_matrix_before_any_mutation(self) -> None:
        config = self._config()
        cases = (
            (
                "candidate_equals_source",
                replace(config, candidate_app=config.source_root),
                "candidate_app_must_not_equal_source_root",
            ),
            (
                "source_inside_candidate",
                replace(config, candidate_app=config.source_root.parent),
                "source_inside_candidate_forbidden",
            ),
            (
                "live_inside_source",
                replace(
                    config,
                    live_core=config.source_root / "unsafe-live-core",
                    live_database=config.source_root
                    / "unsafe-live-core/data/activity.sqlite",
                ),
                "source_root_live_core_overlap_forbidden",
            ),
            (
                "installed_inside_candidate",
                replace(config, installed_app=config.candidate_app / "installed.app"),
                "candidate_app_installed_app_overlap_forbidden",
            ),
            (
                "core_inside_installed",
                replace(
                    config,
                    installed_app=config.live_core.parent,
                ),
                "live_core_installed_app_overlap_forbidden",
            ),
            (
                "noncanonical_database",
                replace(config, live_database=config.live_core / "wrong.sqlite"),
                "live_database_not_canonical",
            ),
            (
                "core_plist_inside_live",
                replace(config, launch_agent_plist=config.live_core / "core.plist"),
                "launch_agent_plist_inside_rename_tree",
            ),
            (
                "shadow_plist_inside_installed",
                replace(
                    config,
                    shadow_launch_agent_plist=config.installed_app / "shadow.plist",
                ),
                "shadow_launch_agent_plist_inside_rename_tree",
            ),
            (
                "shared_plist",
                replace(
                    config,
                    shadow_launch_agent_plist=config.launch_agent_plist,
                ),
                "shadow_and_core_launch_agents_must_differ",
            ),
        )
        for label, invalid, error in cases:
            with self.subTest(label=label), patch.object(
                rollout,
                "_git_output",
            ) as git_check, patch.object(
                rollout,
                "_ensure_private_directory",
            ) as directory_create, patch.object(
                rollout,
                "_durable_rename",
            ) as rename:
                with self.assertRaisesRegex(rollout.RolloutError, error):
                    rollout._assert_release_identity(invalid)
                git_check.assert_not_called()
                directory_create.assert_not_called()
                rename.assert_not_called()

        inside_source = replace(
            config,
            candidate_app=config.source_root / "build/Personal AI Monitor.app",
        )
        rollout._validate_path_relationships(inside_source)


    def test_python_interpreter_trusts_signed_root_symlink_chain_and_detects_drift(self) -> None:
        def signed_identity(
            *,
            target: str = "python3.12",
            team: str = "BMM5U3QVKW",
            final_uid: int = 0,
            hashes: tuple[str, ...] = ("a" * 64, "a" * 64, "a" * 64),
        ) -> dict[str, object]:
            directory = SimpleNamespace(
                st_mode=rollout.stat.S_IFDIR | 0o775,
                st_uid=0,
                st_gid=rollout.PYTHON_ADMIN_GROUP_ID,
                st_dev=1,
                st_ino=1,
            )
            link = SimpleNamespace(
                st_mode=rollout.stat.S_IFLNK | 0o775,
                st_uid=0,
                st_gid=rollout.PYTHON_ADMIN_GROUP_ID,
                st_dev=1,
                st_ino=2,
            )
            binary = SimpleNamespace(
                st_mode=rollout.stat.S_IFREG | 0o775,
                st_uid=final_uid,
                st_gid=rollout.PYTHON_ADMIN_GROUP_ID,
                st_dev=1,
                st_ino=3,
                st_size=123,
                st_mtime_ns=456,
            )

            def lstat(path: Path):
                if path == Path("/trusted"):
                    return directory
                if path == Path("/trusted/python3"):
                    return link
                if path == Path("/trusted") / target:
                    return binary
                raise FileNotFoundError(path)

            def command_result(command: list[str], **_kwargs):
                if "--version" in command:
                    self.fail("candidate Python was executed before trust was established")
                if command[:3] == ["codesign", "--verify", "--strict"]:
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if command[:3] == ["codesign", "-dv", "--verbose=4"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="",
                        stderr=(
                            "Identifier=python3\n"
                            f"TeamIdentifier={team}\n"
                            f"CDHash={'c' * 40}\n"
                            + "".join(
                                f"Authority={authority}\n"
                                for authority in rollout.PYTHON_SIGNING_AUTHORITY_CHAIN
                            )
                        ),
                    )
                self.fail(f"unexpected trust command: {command!r}")

            process_version = f"{target.removeprefix('python')}.6"
            process_image = {
                "path": "/trusted/Resources/Python.app/Contents/MacOS/Python",
                "sha256": "d" * 64,
                "device": 1,
                "inode": 4,
                "size": 122,
                "version": process_version,
                "identifier": rollout.PYTHON_PROCESS_IDENTIFIER,
                "team_identifier": rollout.PYTHON_TEAM_IDENTIFIER,
                "cdhash": "e" * 40,
            }
            with patch.object(rollout.os, "lstat", side_effect=lstat), patch.object(
                rollout.os,
                "readlink",
                return_value=target,
            ), patch.object(rollout.os, "access", return_value=True), patch.object(
                Path,
                "stat",
                return_value=binary,
            ), patch.object(
                rollout,
                "_sha256_file",
                side_effect=list(hashes),
            ), patch.object(rollout, "_run", side_effect=command_result), patch.object(
                rollout,
                "_trusted_python_process_image",
                return_value=process_image,
            ):
                return rollout._trusted_python_interpreter("/trusted/python3")

        trusted = signed_identity()
        self.assertEqual(trusted["resolved_path"], "/trusted/python3.12")
        self.assertEqual(trusted["team_identifier"], "BMM5U3QVKW")
        self.assertEqual(trusted["version"], "3.12.6")
        self.assertEqual(trusted["identity_format"], "python_org_dual_image_v1")
        self.assertEqual(
            trusted["process_image"]["identifier"],
            rollout.PYTHON_PROCESS_IDENTIFIER,
        )

        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_interpreter_untrusted",
        ):
            signed_identity(final_uid=501)
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_interpreter_signature_identity_mismatch",
        ):
            signed_identity(team="UNTRUSTED")
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_interpreter_identity_changed",
        ):
            signed_identity(hashes=("a" * 64, "b" * 64, "b" * 64))

        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_interpreter_identity_changed",
        ):
            signed_identity(hashes=("a" * 64, "a" * 64, "b" * 64))

        redirected = signed_identity(
            target="python3.13",
            hashes=("b" * 64, "b" * 64, "b" * 64),
        )
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_interpreter_identity_mismatch",
        ):
            rollout._assert_matching_interpreter_identity(
                {"interpreter": trusted},
                {"interpreter": redirected},
                expected=trusted,
            )
        config = self._config()
        with patch.object(
            rollout,
            "_verify_core_launch_agent_path",
            return_value={"interpreter": redirected},
        ), patch.object(rollout, "_run") as command:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_interpreter_identity_drift",
            ):
                rollout._verify_launch_agent(
                    config,
                    expected_interpreter=trusted,
                )
        command.assert_not_called()
        with patch.object(
            rollout,
            "_verify_shadow_launch_agent_path",
            return_value={"interpreter": redirected},
        ), patch.object(rollout, "_run") as command:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_interpreter_identity_drift",
            ):
                rollout._verify_shadow_runtime(
                    config,
                    expected_interpreter=trusted,
                )
        command.assert_not_called()


    def test_launch_agent_contracts_reject_shadow_aliases_and_arbitrary_commands(self) -> None:
        config = self._config()
        config.live_database.parent.mkdir(parents=True, mode=0o700)
        config.live_database.write_bytes(b"live")
        config.live_database.chmod(0o600)
        config.launch_agent_plist.parent.mkdir(parents=True, exist_ok=True)
        interpreter = {
            "program": "/trusted/python3",
            "resolved_path": "/trusted/python3.12",
            "sha256": "a" * 64,
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
        }

        def write_plist(path: Path, payload: dict[str, object]) -> None:
            with path.open("wb") as handle:
                plistlib.dump(payload, handle)
            path.chmod(0o600)

        main_arguments = [
            "/trusted/python3",
            "-m",
            "activity_mvp",
            "--db",
            str(config.live_database),
            "run-daemon",
        ]
        main_payload = {
            "Label": config.service_label,
            "WorkingDirectory": str(config.live_core),
            "EnvironmentVariables": {
                "PATH": rollout.LAUNCH_AGENT_PATH,
                "PYTHONPATH": str(config.live_core),
                "PAM_PRIVACY_PROFILE": config.expected_privacy_profile,
            },
            "ProgramArguments": main_arguments,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(self.root / "logs/core.out"),
            "StandardErrorPath": str(self.root / "logs/core.err"),
        }
        write_plist(config.launch_agent_plist, main_payload)
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ):
            self.assertEqual(
                rollout._verify_core_launch_agent(config)["arguments"],
                main_arguments,
            )
            invalid_main = dict(main_payload)
            invalid_main["ProgramArguments"] = main_arguments[:-1] + ["task-sources"]
            write_plist(config.launch_agent_plist, invalid_main)
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_canonical_path_mismatch",
            ):
                rollout._verify_core_launch_agent(config)

        shadow_core = self.root / "shadow-core"
        shadow_core.mkdir()
        shadow_database = self.root / "shadow-data/activity.sqlite"
        shadow_database.parent.mkdir()
        shadow_database.write_bytes(b"shadow")
        shadow_database.chmod(0o600)
        shadow_arguments = [
            "/trusted/python3",
            "-m",
            "activity_mvp",
            "--db",
            str(shadow_database),
            "task-sources",
            "serve-receipts",
            "--socket",
            str(self.root / "shadow.sock"),
        ]
        shadow_payload = {
            "Label": config.shadow_launch_agent_label,
            "WorkingDirectory": str(shadow_core),
            "EnvironmentVariables": {
                "PATH": rollout.LAUNCH_AGENT_PATH,
                "PYTHONPATH": str(shadow_core),
            },
            "ProgramArguments": shadow_arguments,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(self.root / "logs/shadow.out"),
            "StandardErrorPath": str(self.root / "logs/shadow.err"),
            "ProcessType": "Background",
            "ThrottleInterval": 2,
        }
        write_plist(config.shadow_launch_agent_plist, shadow_payload)
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(
            rollout,
            "_shadow_core_database_pair",
            return_value={"pair_sha256": "a" * 64},
        ):
            self.assertEqual(
                rollout._verify_shadow_launch_agent(config)["arguments"],
                shadow_arguments,
            )
            invalid_shadow = dict(shadow_payload)
            invalid_shadow["ProgramArguments"] = shadow_arguments[:-4] + ["run-daemon"]
            write_plist(config.shadow_launch_agent_plist, invalid_shadow)
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "shadow_launch_agent_canonical_path_mismatch",
            ):
                rollout._verify_shadow_launch_agent(config)

            write_plist(config.shadow_launch_agent_plist, shadow_payload)
            shadow_database.unlink()
            os.link(config.live_database, shadow_database)
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "shadow_launch_agent_database_not_independent",
            ):
                rollout._verify_shadow_launch_agent(config)

            shadow_database.unlink()
            shadow_database.write_bytes(b"shadow-two")
            shadow_database.chmod(0o600)
            aliased_core = dict(shadow_payload)
            aliased_core["WorkingDirectory"] = str(config.live_core)
            aliased_core["EnvironmentVariables"] = {
                "PATH": rollout.LAUNCH_AGENT_PATH,
                "PYTHONPATH": str(config.live_core),
            }
            write_plist(config.shadow_launch_agent_plist, aliased_core)
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "shadow_launch_agent_core_not_independent",
            ):
                rollout._verify_shadow_launch_agent(config)


    def test_python_process_image_is_fixed_to_signed_matching_framework_bundle(self) -> None:
        version_root = self.root / "Python.framework" / "Versions" / "3.12"
        launcher = version_root / "bin" / "python3.12"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"launcher")
        launcher.chmod(0o755)
        bundle = version_root / "Resources" / "Python.app"
        process_image = bundle / "Contents" / "MacOS" / "Python"
        process_image.parent.mkdir(parents=True)
        process_image.write_bytes(b"process-image")
        process_image.chmod(0o755)
        info_plist = bundle / "Contents" / "Info.plist"
        with info_plist.open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": rollout.PYTHON_PROCESS_IDENTIFIER,
                    "CFBundleExecutable": "Python",
                    "CFBundleShortVersionString": "3.12.6",
                    "CFBundleVersion": "3.12.6",
                },
                handle,
            )

        # Fixture identities require public bundle metadata even when the
        # background verifier inherits the production-private umask.
        info_plist.chmod(0o644)

        def trusted_snapshot(path: Path, **_kwargs):
            metadata = path.stat()
            return (
                (
                    str(path),
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                    int(metadata.st_uid),
                    int(metadata.st_gid),
                    0o755 if os.access(path, os.X_OK) else 0o644,
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                ),
            )

        def signed(command: list[str], **_kwargs):
            if "--version" in command:
                self.fail("candidate Python was executed during static identity probing")
            if command[:3] == ["codesign", "-dv", "--verbose=4"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="",
                    stderr=(
                        f"Identifier={rollout.PYTHON_PROCESS_IDENTIFIER}\n"
                        f"TeamIdentifier={rollout.PYTHON_TEAM_IDENTIFIER}\n"
                        f"CDHash={'f' * 40}\n"
                        + "".join(
                            f"Authority={authority}\n"
                            for authority in rollout.PYTHON_SIGNING_AUTHORITY_CHAIN
                        )
                    ),
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(
            rollout,
            "_root_owned_regular_path_snapshot",
            side_effect=trusted_snapshot,
        ), patch.object(rollout, "_run", side_effect=signed), patch.object(
            rollout.plistlib,
            "loads",
            wraps=plistlib.loads,
        ) as plist_parser:
            identity = rollout._trusted_python_process_image(launcher)
        self.assertEqual(identity["path"], str(process_image))
        self.assertEqual(identity["bundle_path"], str(bundle))
        self.assertEqual(identity["identifier"], rollout.PYTHON_PROCESS_IDENTIFIER)
        self.assertEqual(identity["team_identifier"], rollout.PYTHON_TEAM_IDENTIFIER)
        self.assertEqual(identity["cdhash"], "f" * 40)
        self.assertEqual(
            identity["bundle_identifier"],
            rollout.PYTHON_PROCESS_IDENTIFIER,
        )
        self.assertEqual(
            identity["bundle_team_identifier"],
            rollout.PYTHON_TEAM_IDENTIFIER,
        )
        self.assertEqual(identity["bundle_cdhash"], "f" * 40)
        self.assertEqual(identity["device"], process_image.stat().st_dev)
        self.assertEqual(identity["inode"], process_image.stat().st_ino)
        parsed_plist_bytes = plist_parser.call_args.args[0]
        self.assertEqual(
            identity["info_plist_sha256"],
            rollout.hashlib.sha256(parsed_plist_bytes).hexdigest(),
        )

        def wrong_bundle_identity(command: list[str], **_kwargs):
            result = signed(command)
            if (
                command[:3] == ["codesign", "-dv", "--verbose=4"]
                and command[-1] == str(bundle)
            ):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="",
                    stderr=result.stderr.replace(
                        rollout.PYTHON_TEAM_IDENTIFIER,
                        "UNTRUSTED",
                    ),
                )
            return result

        with patch.object(
            rollout,
            "_root_owned_regular_path_snapshot",
            side_effect=trusted_snapshot,
        ), patch.object(rollout, "_run", side_effect=wrong_bundle_identity):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_process_image_signature_identity_mismatch",
            ):
                rollout._trusted_python_process_image(launcher)

        def rejected_apple_requirement(command: list[str], **_kwargs):
            if any(argument.startswith("-R=anchor apple generic") for argument in command):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="designated requirement not satisfied",
                )
            return signed(command)

        with patch.object(
            rollout,
            "_root_owned_regular_path_snapshot",
            side_effect=trusted_snapshot,
        ), patch.object(rollout, "_run", side_effect=rejected_apple_requirement):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_process_image_signature_identity_mismatch",
            ):
                rollout._trusted_python_process_image(launcher)

        def incomplete_authority_chain(command: list[str], **_kwargs):
            result = signed(command)
            if command[:3] == ["codesign", "-dv", "--verbose=4"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="",
                    stderr=result.stderr.replace("Authority=Apple Root CA\n", ""),
                )
            return result

        with patch.object(
            rollout,
            "_root_owned_regular_path_snapshot",
            side_effect=trusted_snapshot,
        ), patch.object(rollout, "_run", side_effect=incomplete_authority_chain):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_process_image_signature_identity_mismatch",
            ):
                rollout._trusted_python_process_image(launcher)

        for drift_target in (bundle, process_image):
            with self.subTest(final_codesign_identity_drift=str(drift_target)):
                detail_counts: dict[str, int] = {}

                def changed_final_identity(command: list[str], **_kwargs):
                    result = signed(command)
                    if command[:3] == ["codesign", "-dv", "--verbose=4"]:
                        target = command[-1]
                        detail_counts[target] = detail_counts.get(target, 0) + 1
                        if target == str(drift_target) and detail_counts[target] == 2:
                            return subprocess.CompletedProcess(
                                command,
                                0,
                                stdout="",
                                stderr=result.stderr.replace("f" * 40, "e" * 40),
                            )
                    return result

                with patch.object(
                    rollout,
                    "_root_owned_regular_path_snapshot",
                    side_effect=trusted_snapshot,
                ), patch.object(
                    rollout,
                    "_run",
                    side_effect=changed_final_identity,
                ):
                    with self.assertRaisesRegex(
                        rollout.RolloutError,
                        "launch_agent_process_image_identity_changed",
                    ):
                        rollout._trusted_python_process_image(launcher)

        def wrong_identifier(command: list[str], **_kwargs):
            result = signed(command)
            if command[:3] == ["codesign", "-dv", "--verbose=4"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="",
                    stderr=result.stderr.replace(
                        rollout.PYTHON_PROCESS_IDENTIFIER,
                        "org.python.untrusted",
                    ),
                )
            return result

        with patch.object(
            rollout,
            "_root_owned_regular_path_snapshot",
            side_effect=trusted_snapshot,
        ), patch.object(rollout, "_run", side_effect=wrong_identifier):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_process_image_signature_identity_mismatch",
            ):
                rollout._trusted_python_process_image(launcher)

        def write_versions(short_version: object, bundle_version: object) -> None:
            with info_plist.open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": rollout.PYTHON_PROCESS_IDENTIFIER,
                        "CFBundleExecutable": "Python",
                        "CFBundleShortVersionString": short_version,
                        "CFBundleVersion": bundle_version,
                    },
                    handle,
                )

        for label, short_version, bundle_version in (
            ("version-disagreement", "3.12.6", "3.12.7"),
            ("launcher-major-minor-mismatch", "3.13.6", "3.13.6"),
            ("unsupported-version", "3.11.9", "3.11.9"),
            ("non-canonical-version", "03.12.6", "03.12.6"),
            ("non-ascii-version", "3.1٢.6", "3.1٢.6"),
            ("oversized-version", "3.1234567.6", "3.1234567.6"),
            ("malformed-version", "3.12.6;unsafe", "3.12.6;unsafe"),
        ):
            with self.subTest(plist_version_case=label):
                write_versions(short_version, bundle_version)
                with patch.object(
                    rollout,
                    "_root_owned_regular_path_snapshot",
                    side_effect=trusted_snapshot,
                ), patch.object(rollout, "_run") as command:
                    with self.assertRaisesRegex(
                        rollout.RolloutError,
                        "launch_agent_process_image_bundle_mismatch",
                    ):
                        rollout._trusted_python_process_image(launcher)
                command.assert_not_called()

        write_versions("3.12.6", "3.12.6")
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_process_image_invalid",
        ):
            rollout._trusted_python_process_image(
                self.root
                / "Untrusted.framework"
                / "Versions"
                / "3.12"
                / "bin"
                / "python3.12",
            )

        with patch.object(
            rollout,
            "_root_owned_regular_path_snapshot",
            side_effect=trusted_snapshot,
        ), patch.object(rollout, "_run", side_effect=signed), patch.object(
            rollout,
            "_sha256_file",
            side_effect=["a" * 64, "b" * 64, "c" * 64, "b" * 64],
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_process_image_identity_changed",
            ):
                rollout._trusted_python_process_image(launcher)


    def test_info_plist_nofollow_reader_rejects_symlink_and_fstat_drift(self) -> None:
        payload = b"stable plist bytes"
        source = self.root / "Info.plist"
        source.write_bytes(payload)
        observed, metadata = rollout._read_stable_nofollow_file(
            source,
            max_bytes=1024,
            error="plist_untrusted",
        )
        self.assertEqual(observed, payload)
        self.assertEqual(metadata.st_ino, source.stat().st_ino)

        symlink = self.root / "Info-link.plist"
        symlink.symlink_to(source)
        with self.assertRaisesRegex(rollout.RolloutError, "plist_untrusted"):
            rollout._read_stable_nofollow_file(
                symlink,
                max_bytes=1024,
                error="plist_untrusted",
            )

        original = source.stat()
        stable = SimpleNamespace(
            st_dev=original.st_dev,
            st_ino=original.st_ino,
            st_mode=original.st_mode,
            st_uid=original.st_uid,
            st_gid=original.st_gid,
            st_nlink=original.st_nlink,
            st_size=original.st_size,
            st_mtime_ns=original.st_mtime_ns,
            st_ctime_ns=original.st_ctime_ns,
        )
        changed = SimpleNamespace(**vars(stable))
        changed.st_mtime_ns += 1
        with patch.object(rollout.os, "fstat", side_effect=[stable, changed]):
            with self.assertRaisesRegex(rollout.RolloutError, "plist_untrusted"):
                rollout._read_stable_nofollow_file(
                    source,
                    max_bytes=1024,
                    error="plist_untrusted",
                )


    def test_process_image_path_snapshot_rejects_symlink_and_non_root_owner(self) -> None:
        directory = SimpleNamespace(
            st_mode=rollout.stat.S_IFDIR | 0o755,
            st_uid=0,
            st_gid=0,
            st_dev=1,
            st_ino=1,
        )
        symlink = SimpleNamespace(
            st_mode=rollout.stat.S_IFLNK | 0o755,
            st_uid=0,
            st_gid=0,
            st_dev=1,
            st_ino=2,
        )
        non_root = SimpleNamespace(
            st_mode=rollout.stat.S_IFREG | 0o755,
            st_uid=501,
            st_gid=0,
            st_dev=1,
            st_ino=3,
            st_size=1,
            st_mtime_ns=1,
        )
        world_writable = SimpleNamespace(
            st_mode=rollout.stat.S_IFREG | 0o777,
            st_uid=0,
            st_gid=rollout.PYTHON_ADMIN_GROUP_ID,
            st_dev=1,
            st_ino=4,
            st_size=1,
            st_mtime_ns=1,
        )
        untrusted_group = SimpleNamespace(
            st_mode=rollout.stat.S_IFREG | 0o775,
            st_uid=0,
            st_gid=501,
            st_dev=1,
            st_ino=5,
            st_size=1,
            st_mtime_ns=1,
        )

        def path_metadata(final):
            def lookup(path: Path):
                return final if path == Path("/trusted/Python") else directory

            return lookup

        for label, final in (
            ("symlink", symlink),
            ("non-root", non_root),
            ("world-writable", world_writable),
            ("untrusted-group", untrusted_group),
        ):
            with self.subTest(label=label), patch.object(
                rollout.os,
                "lstat",
                side_effect=path_metadata(final),
            ):
                with self.assertRaisesRegex(
                    rollout.RolloutError,
                    "launch_agent_process_image_untrusted",
                ):
                    rollout._root_owned_regular_path_snapshot(
                        Path("/trusted/Python"),
                        executable=True,
                        error="launch_agent_process_image_untrusted",
                    )


    def test_runtime_pid_proof_rejects_inode_and_argv_smuggling(self) -> None:
        launcher = self.root / "python3.12"
        launcher.write_bytes(b"signed-python-launcher-fixture")
        launcher.chmod(0o755)
        process_image_path = self.root / "Python"
        process_image_path.write_bytes(b"signed-python-process-image-fixture")
        process_image_path.chmod(0o755)
        metadata = process_image_path.stat()
        working_directory = self.root / "core-runtime"
        working_directory.mkdir()
        process_image = {
            "path": str(process_image_path),
            "sha256": rollout._sha256_file(process_image_path),
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "size": int(metadata.st_size),
            "version": "3.12.6",
            "identifier": rollout.PYTHON_PROCESS_IDENTIFIER,
            "team_identifier": rollout.PYTHON_TEAM_IDENTIFIER,
            "cdhash": "e" * 40,
        }
        interpreter = {
            "program": "/trusted/python3",
            "resolved_path": str(launcher),
            "sha256": rollout._sha256_file(launcher),
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
            "process_image": process_image,
        }
        expected = {
            "program": interpreter["program"],
            "resolved_program": str(launcher),
            "arguments": [
                interpreter["program"],
                "-m",
                "activity_mvp",
                "--db",
                "/private/db",
                "run-daemon",
            ],
            "working_directory": str(working_directory),
            "interpreter": interpreter,
        }
        lsof_ok = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout=(
                f"fcwd\nD{metadata.st_dev:#x}\ni{working_directory.stat().st_ino}\n"
                f"n{working_directory}\n"
                f"ftxt\nD{metadata.st_dev:#x}\ni{metadata.st_ino}\nn{process_image_path}\n"
            ),
            stderr="",
        )
        ps_ok = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout=(
                f"{os.getuid()} {process_image_path} "
                f"{' '.join(expected['arguments'][1:])}\n"
            ),
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(rollout, "_run", side_effect=[lsof_ok, ps_ok]):
            rollout._verify_pid_os_identity(123, expected)

        wrong_cwd_identity = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout=lsof_ok.stdout.replace(
                f"i{working_directory.stat().st_ino}\n",
                f"i{working_directory.stat().st_ino + 1}\n",
                1,
            ),
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(rollout, "_run", return_value=wrong_cwd_identity):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_pid_file_identity_mismatch",
            ):
                rollout._verify_pid_os_identity(123, expected)

        lsof_wrong_inode = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout=lsof_ok.stdout.replace(f"i{metadata.st_ino}", f"i{metadata.st_ino + 1}"),
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(rollout, "_run", return_value=lsof_wrong_inode):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_pid_file_identity_mismatch",
            ):
                rollout._verify_pid_os_identity(123, expected)

        lsof_launcher_instead_of_process_image = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout=lsof_ok.stdout.replace(str(process_image_path), str(launcher)),
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(
            rollout,
            "_run",
            return_value=lsof_launcher_instead_of_process_image,
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_pid_file_identity_mismatch",
            ):
                rollout._verify_pid_os_identity(123, expected)

        ps_smuggled = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout=(
                f"{os.getuid()} {process_image_path} "
                f"{' '.join(expected['arguments'][1:])} --unsafe\n"
            ),
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(rollout, "_run", side_effect=[lsof_ok, ps_smuggled]):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_pid_process_identity_mismatch",
            ):
                rollout._verify_pid_os_identity(123, expected)

        ps_uses_launcher = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout=f"{os.getuid()} {' '.join(expected['arguments'])}\n",
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(rollout, "_run", side_effect=[lsof_ok, ps_uses_launcher]):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_pid_process_identity_mismatch",
            ):
                rollout._verify_pid_os_identity(123, expected)

        smuggled_launchctl = self._launchctl_output(
            expected,
            pid=123,
            working_directory="/wrong/core",
        ) + f"note = {expected['working_directory']} {' '.join(expected['arguments'])}\n"
        with patch.object(rollout, "_verify_pid_os_identity") as pid_check:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_runtime_identity_mismatch",
            ):
                rollout._verify_launchctl_runtime(
                    smuggled_launchctl,
                    expected,
                    target="gui/501/app.personalai.monitor.core",
                )
        pid_check.assert_not_called()

        initial_launchctl = self._launchctl_output(expected, pid=123)
        changed_identities: list[tuple[str, str]] = []
        changed_program = dict(expected)
        changed_program["program"] = "/trusted/other-python3"
        changed_identities.append(
            ("program", self._launchctl_output(changed_program, pid=123))
        )
        changed_arguments = dict(expected)
        changed_arguments["arguments"] = [*expected["arguments"], "--unsafe"]
        changed_identities.append(
            ("arguments", self._launchctl_output(changed_arguments, pid=123))
        )
        changed_identities.append(
            (
                "working_directory",
                self._launchctl_output(
                    expected,
                    pid=123,
                    working_directory="/different/core",
                ),
            )
        )
        changed_identities.append(("pid", self._launchctl_output(expected, pid=124)))
        target = "gui/501/app.personalai.monitor.core"
        for field, changed_output in changed_identities:
            with self.subTest(second_launchctl_changed=field), patch.object(
                rollout,
                "_verify_pid_os_identity",
            ) as pid_check, patch.object(
                rollout,
                "_run",
                return_value=subprocess.CompletedProcess(
                    ["launchctl"],
                    0,
                    stdout=changed_output,
                    stderr="",
                ),
            ) as runner:
                with self.assertRaisesRegex(
                    rollout.RolloutError,
                    "launch_agent_runtime_identity_changed",
                ):
                    rollout._verify_launchctl_runtime(
                        initial_launchctl,
                        expected,
                        target=target,
                    )
                pid_check.assert_called_once_with(123, expected)
                runner.assert_called_once_with(
                    ["launchctl", "print", target],
                    timeout=10.0,
                )

        with patch.object(
            rollout,
            "_verify_pid_os_identity",
        ), patch.object(
            rollout,
            "_run",
            return_value=subprocess.CompletedProcess(
                ["launchctl"],
                1,
                stdout="",
                stderr="service disappeared",
            ),
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_runtime_identity_recheck_unavailable",
            ):
                rollout._verify_launchctl_runtime(
                    initial_launchctl,
                    expected,
                    target=target,
                )


    def test_migration_uses_authenticated_runtime_interpreter_and_rejects_drift(self) -> None:
        config = self._config()
        candidate_core = self.root / "candidate-core"
        candidate_core.mkdir()
        candidate_core.chmod(0o755)
        expected_git_identity = rollout._materialized_core_identity(candidate_core)
        git_manifest_identity = {
            **expected_git_identity,
            "revision": config.expected_revision,
            "core_tree_object": "1" * 40,
            "manifest_sha256": "2" * 64,
            "core_code_sha256": config.expected_core_code_sha256,
        }
        agents = candidate_core / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        agents_sha256 = rollout._sha256_file(agents)
        expected_identity = rollout._materialized_core_identity(candidate_core)
        database = self.root / "migration.sqlite"
        interpreter = {
            "program": "/trusted/python3",
            "resolved_path": "/trusted/python3.12",
            "sha256": "a" * 64,
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
        }
        completed = subprocess.CompletedProcess(
            [interpreter["program"]],
            0,
            stdout="",
            stderr="",
        )
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(
            rollout,
            "_git_core_manifest",
            return_value=(),
        ), patch.object(
            rollout,
            "_git_core_manifest_identity",
            return_value=git_manifest_identity,
        ), patch.object(rollout.subprocess, "run", return_value=completed) as runner:
            rollout._migrate_with_candidate_core(
                config,
                database,
                interpreter_identity=interpreter,
                core_root=candidate_core,
                expected_materialized_identity=expected_identity,
                expected_git_materialized_identity=expected_git_identity,
                expected_agents_sha256=agents_sha256,
            )
        self.assertEqual(runner.call_args.args[0][0], interpreter["program"])
        self.assertNotEqual(runner.call_args.args[0][0], sys.executable)

        redirected = {**interpreter, "resolved_path": "/attacker/python3.12"}
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=redirected,
        ), patch.object(
            rollout,
            "_git_core_manifest",
            return_value=(),
        ), patch.object(
            rollout,
            "_git_core_manifest_identity",
            return_value=git_manifest_identity,
        ), patch.object(rollout.subprocess, "run") as runner:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "migration_interpreter_identity_drift",
            ):
                rollout._migrate_with_candidate_core(
                    config,
                    database,
                    interpreter_identity=interpreter,
                    core_root=candidate_core,
                    expected_materialized_identity=expected_identity,
                    expected_git_materialized_identity=expected_git_identity,
                    expected_agents_sha256=agents_sha256,
                )
        runner.assert_not_called()

        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(rollout.subprocess, "run") as runner:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "migration_worktree_core_forbidden",
            ):
                rollout._migrate_with_candidate_core(
                    config,
                    database,
                    interpreter_identity=interpreter,
                    core_root=config.source_root / "core",
                    expected_materialized_identity=expected_identity,
                    expected_git_materialized_identity=expected_git_identity,
                    expected_agents_sha256=agents_sha256,
                )
        runner.assert_not_called()


    def test_launch_agent_renderer_uses_sealed_plists_and_detects_core_mutation(self) -> None:
        config = self._config()
        artifacts = rollout._rollout_artifacts(config, "1" * 32)
        artifacts.incoming_core.mkdir(parents=True, mode=0o755)
        artifacts.incoming_core.chmod(0o755)
        payload_file = artifacts.incoming_core / "pam_isolated_launcher.py"
        payload_file.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        payload_file.chmod(0o755)
        expected_git_identity = rollout._materialized_core_identity(
            artifacts.incoming_core
        )
        agents = artifacts.incoming_core / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        agents_sha256 = rollout._sha256_file(agents)
        expected_identity = rollout._materialized_core_identity(
            artifacts.incoming_core
        )
        interpreter = {
            "program": "/trusted/python3",
            "resolved_path": "/trusted/python3.12",
            "sha256": "a" * 64,
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
        }
        core_identity = {
            "interpreter": interpreter,
            "program": interpreter["program"],
            "environment": {
                "PATH": rollout.LAUNCH_AGENT_PATH,
                "PAM_PRIVACY_PROFILE": "personal_high_precision",
            },
        }
        shadow_identity = {
            "interpreter": interpreter,
            "program": interpreter["program"],
            "environment": {"PATH": rollout.LAUNCH_AGENT_PATH},
            "working_directory": str(self.root / "shadow-core"),
            "database": str(self.root / "shadow.sqlite"),
            "socket": str(self.root / "shadow.sock"),
            "shadow_pair": {"pair_sha256": "b" * 64},
        }
        observed_plists: list[Path] = []

        def verify_core(_config, _payload, **_kwargs):
            return core_identity

        def verify_shadow(_config, _payload, **_kwargs):
            return shadow_identity

        def plist_payload(
            path: Path,
            *,
            expected_label: str,
            expected_sha256: str | None = None,
        ):
            self.assertIsInstance(expected_sha256, str)
            observed_plists.append(path)
            return {
                "Label": expected_label,
                "StandardOutPath": str(self.root / "stdout.log"),
                "StandardErrorPath": str(self.root / "stderr.log"),
            }

        def mutate_core(*_args, **_kwargs):
            payload_file.write_text("#!/usr/bin/env python3\n# drift\n", encoding="utf-8")
            return {}

        with patch.object(
            rollout,
            "_assert_recorded_git_core_identity",
            return_value=expected_git_identity,
        ), patch.object(
            rollout,
            "_verify_core_launch_agent_payload",
            side_effect=verify_core,
        ), patch.object(
            rollout,
            "_verify_shadow_launch_agent_payload",
            side_effect=verify_shadow,
        ), patch.object(
            rollout,
            "_plist_payload",
            side_effect=plist_payload,
        ), patch.object(
            rollout,
            "_render_staged_launch_agent",
            side_effect=mutate_core,
        ), patch.object(
            rollout,
            "_write_private_plist",
        ) as writer:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "staged_git_core_identity_drift",
            ):
                rollout._stage_isolated_launch_agents(
                    config,
                    artifacts,
                    expected_core_identity=expected_identity,
                    expected_git_core_identity=expected_git_identity,
                    expected_agents_sha256=agents_sha256,
                    source_identity={
                        "launch_agent_plist_sha256": "1" * 64,
                        "shadow_launch_agent_plist_sha256": "2" * 64,
                    },
                )
        writer.assert_not_called()
        self.assertTrue(observed_plists)
        self.assertEqual(
            set(observed_plists),
            {artifacts.old_plist, artifacts.old_shadow_plist},
        )
        self.assertNotIn(config.launch_agent_plist, observed_plists)
        self.assertNotIn(config.shadow_launch_agent_plist, observed_plists)


    def test_sealed_launch_agent_plist_is_hash_bound_and_swap_during_read_fails(self) -> None:
        sealed = self.root / "sealed.plist"
        original = plistlib.dumps(
            {"Label": "app.personalai.monitor.core", "ProgramArguments": ["safe"]},
            sort_keys=True,
        )
        tampered = plistlib.dumps(
            {"Label": "app.personalai.monitor.core", "ProgramArguments": ["tampered"]},
            sort_keys=True,
        )
        sealed.write_bytes(original)
        sealed.chmod(0o600)
        expected_sha256 = hashlib.sha256(original).hexdigest()
        self.assertEqual(
            rollout._plist_payload(
                sealed,
                expected_label="app.personalai.monitor.core",
                expected_sha256=expected_sha256,
            )["ProgramArguments"],
            ["safe"],
        )

        sealed.write_bytes(tampered)
        sealed.chmod(0o600)
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "launch_agent_plist_identity_drift",
        ):
            rollout._plist_payload(
                sealed,
                expected_label="app.personalai.monitor.core",
                expected_sha256=expected_sha256,
            )

        sealed.write_bytes(original)
        sealed.chmod(0o600)
        displaced = self.root / "sealed.original"
        real_read = os.read
        swapped = False

        def swap_after_open(descriptor: int, size: int) -> bytes:
            nonlocal swapped
            payload = real_read(descriptor, size)
            if not swapped:
                swapped = True
                os.replace(sealed, displaced)
                sealed.write_bytes(tampered)
                sealed.chmod(0o600)
            return payload

        with patch.object(rollout.os, "read", side_effect=swap_after_open):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "launch_agent_plist_invalid",
            ):
                rollout._plist_payload(
                    sealed,
                    expected_label="app.personalai.monitor.core",
                    expected_sha256=expected_sha256,
                )


    def test_migration_rejects_tracked_core_mutation_even_if_full_identity_is_captured_late(self) -> None:
        _repository, config = self._core_git_fixture()
        materialized = self.root / "late-seal" / "core"
        materialized.parent.mkdir()
        copied = rollout._copy_candidate_core(config, materialized)
        (materialized / "notes.txt").write_text(
            "mutated before AGENTS copy\n",
            encoding="utf-8",
        )
        agents = materialized / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        late_full_identity = rollout._materialized_core_identity(materialized)
        git_manifest = rollout._git_core_manifest(config)
        git_identity = rollout._git_core_manifest_identity(config, git_manifest)
        interpreter = {
            "program": "/trusted/python3",
            "resolved_path": "/trusted/python3.12",
            "sha256": "a" * 64,
            "version": "3.12.6",
            "identifier": "python3",
            "team_identifier": "BMM5U3QVKW",
        }
        with patch.object(
            rollout,
            "_trusted_python_interpreter",
            return_value=interpreter,
        ), patch.object(
            rollout,
            "_git_core_manifest",
            return_value=git_manifest,
        ), patch.object(
            rollout,
            "_git_core_manifest_identity",
            return_value=git_identity,
        ), patch.object(rollout.subprocess, "run") as runner:
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "staged_git_core_identity_drift",
            ):
                rollout._migrate_with_candidate_core(
                    config,
                    self.root / "late-seal.sqlite",
                    interpreter_identity=interpreter,
                    core_root=materialized,
                    expected_materialized_identity=late_full_identity,
                    expected_git_materialized_identity=copied["materialized"],
                    expected_agents_sha256=rollout._sha256_file(agents),
                )
        runner.assert_not_called()


    def test_schema28_rollback_database_rebuild_is_quarantined_and_idempotent(self) -> None:
        config, state, artifacts, source_probe, backup_probe = (
            self._prepare_database_recovery_case("rebuild")
        )
        original_inode = config.live_database.stat().st_ino
        healthy = rollout._restore_schema28_database_from_backup(
            config,
            state,
            artifacts,
            core_root=config.live_core,
        )
        self.assertEqual(healthy["status"], "original_or_recovered")
        self.assertEqual(config.live_database.stat().st_ino, original_inode)

        config.live_database.write_bytes(b"truncated")
        config.live_database.chmod(0o600)
        Path(f"{config.live_database}-wal").write_bytes(b"bad-wal")
        Path(f"{config.live_database}-shm").write_bytes(b"bad-shm")
        rebuilt = rollout._restore_schema28_database_from_backup(
            config,
            state,
            artifacts,
            core_root=config.live_core,
        )
        self.assertEqual(rebuilt["status"], "recovered_from_independent_backup")
        recovered = rollout._sqlite_probe(
            config.live_database,
            expected_schema=rollout.SOURCE_SCHEMA,
        )
        self.assertEqual(recovered["sha256"], backup_probe["sha256"])
        self.assertEqual(recovered["aggregate_counts"], source_probe["aggregate_counts"])
        quarantine = artifacts.core_rollbacks / "database-quarantine"
        self.assertGreaterEqual(len(list(quarantine.iterdir())), 3)
        entries_before_retry = sorted(path.name for path in quarantine.iterdir())
        retried = rollout._restore_schema28_database_from_backup(
            config,
            state,
            artifacts,
            core_root=config.live_core,
        )
        self.assertEqual(retried["status"], "original_or_recovered")
        self.assertEqual(
            sorted(path.name for path in quarantine.iterdir()),
            entries_before_retry,
        )


    def test_schema28_recovery_fails_closed_on_bad_backup_and_recovers_partial_retry(self) -> None:
        config, state, artifacts, _source_probe, _backup_probe = (
            self._prepare_database_recovery_case("badbackup")
        )
        target_before = rollout._sha256_file(config.live_database)
        artifacts.rollback_database.write_bytes(b"corrupt-backup")
        artifacts.rollback_database.chmod(0o600)
        with self.assertRaises(rollout.RolloutError):
            rollout._restore_schema28_database_from_backup(
                config,
                state,
                artifacts,
                core_root=config.live_core,
            )
        self.assertEqual(rollout._sha256_file(config.live_database), target_before)
        self.assertFalse((artifacts.core_rollbacks / "database-quarantine").exists())

        config, state, artifacts, _source_probe, backup_probe = (
            self._prepare_database_recovery_case("partial")
        )
        config.live_database.write_bytes(b"bad-old-database")
        config.live_database.chmod(0o600)
        original_backup = rollout._sqlite_backup
        calls = 0

        def flaky_backup(source: Path, destination: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                destination.write_bytes(b"partial-rebuild")
                destination.chmod(0o600)
                raise rollout.RolloutError("injected_rebuild_failure")
            return original_backup(source, destination)

        with patch.object(rollout, "_sqlite_backup", side_effect=flaky_backup):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "injected_rebuild_failure",
            ):
                rollout._restore_schema28_database_from_backup(
                    config,
                    state,
                    artifacts,
                    core_root=config.live_core,
                )
            recovered = rollout._restore_schema28_database_from_backup(
                config,
                state,
                artifacts,
                core_root=config.live_core,
            )
        self.assertEqual(recovered["status"], "recovered_from_independent_backup")
        self.assertEqual(
            rollout._sqlite_probe(
                config.live_database,
                expected_schema=rollout.SOURCE_SCHEMA,
            )["sha256"],
            backup_probe["sha256"],
        )
        quarantine = artifacts.core_rollbacks / "database-quarantine"
        self.assertGreaterEqual(len(list(quarantine.iterdir())), 2)


    def test_writer_barrier_survives_core_rename_and_fsync_rejects_special_entries(self) -> None:
        config = self._config()
        self._prepare_schema28(config.live_database)
        config.live_database.parent.chmod(0o700)
        config.live_database.chmod(0o600)
        old_core = config.live_core.parent / "old-core"
        with patch.object(rollout, "_assert_database_handles_allowed"):
            with rollout._live_database_writer_barrier(config.live_database) as barrier:
                self.assertTrue(barrier.connection.in_transaction)
                barrier.verify()
                os.rename(config.live_core, old_core)
        moved_database = old_core / "data/activity.sqlite"
        self.assertEqual(
            rollout._sqlite_probe(
                moved_database,
                expected_schema=rollout.SOURCE_SCHEMA,
            )["schema"],
            rollout.SOURCE_SCHEMA,
        )

        config = self._config(root=self.root / "external-writer")
        self._prepare_schema28(config.live_database)
        config.live_database.chmod(0o600)
        with patch.object(
            rollout,
            "_database_handle_pids",
            return_value={os.getpid(), 99999},
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "database_has_active_handles",
            ):
                with rollout._live_database_writer_barrier(config.live_database):
                    self.fail("external writer was accepted")

        symlink_tree = self.root / "fsync-symlink"
        symlink_tree.mkdir()
        (symlink_tree / "target").write_text("x", encoding="utf-8")
        (symlink_tree / "alias").symlink_to("target")
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "durability_tree_special_entry",
        ):
            rollout._fsync_tree(symlink_tree)
        fifo_tree = self.root / "fsync-fifo"
        fifo_tree.mkdir()
        os.mkfifo(fifo_tree / "pipe")
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "durability_tree_special_entry",
        ):
            rollout._fsync_tree(fifo_tree)


    def test_mutating_rollout_lock_is_exclusive(self) -> None:
        config = self._config()
        config.workspace.parent.mkdir(parents=True, exist_ok=True)
        with rollout._rollout_lock(config):
            with self.assertRaisesRegex(rollout.RolloutError, "rollout_already_in_progress"):
                with rollout._rollout_lock(config):
                    self.fail("second lock unexpectedly acquired")


    def test_swift_source_extraction_tracks_active_toolchain_expected_set(self) -> None:
        xctest_source = (
            REPO_ROOT
            / "apps/PersonalAIMonitorMenuBar/Tests/PAMInteractionModelTests"
            / "Phase2ContextModelsTests.swift"
        )
        swift_testing_source = (
            REPO_ROOT
            / "apps/PersonalAIMonitorMenuBar/Tests/PAMInteractionModelTests"
            / "Phase2ContextModelsSwiftTestingTests.swift"
        )
        declared = swift_report.extract_expected_tests(
            xctest_source,
            swift_testing_source,
        )
        baseline = swift_report.load_baseline_tests(
            REPO_ROOT / "tools/swift-release-test-baseline.json"
        )
        self.assertEqual(declared, baseline)
        xctests = [test for test in declared if test.kind == "xctest"]
        swift_tests = [test for test in declared if test.kind == "swift-testing"]
        self.assertTrue(xctests)
        self.assertTrue(swift_tests)
        self.assertTrue(all(test.identifier.startswith("test") for test in xctests))
        self.assertTrue(all(test.display_name for test in swift_tests))

        available = self.root / "expected-available.json"
        swift_report.write_expected_tests(
            available,
            declared,
            declared_tests=declared,
            swift_testing_module="available",
        )
        self.assertEqual(swift_report.load_expected_tests(available), declared)
        available_payload = json.loads(available.read_text(encoding="utf-8"))
        self.assertEqual(available_payload["swift_testing_module"], "available")
        self.assertEqual(
            available_payload["active_counts"],
            available_payload["declared_counts"],
        )

        unavailable = self.root / "expected-unavailable.json"
        swift_report.write_expected_tests(
            unavailable,
            xctests,
            declared_tests=declared,
            swift_testing_module="unavailable",
        )
        self.assertEqual(swift_report.load_expected_tests(unavailable), xctests)
        unavailable_payload = json.loads(unavailable.read_text(encoding="utf-8"))
        self.assertEqual(unavailable_payload["active_counts"]["swift-testing"], 0)
        self.assertEqual(
            unavailable_payload["declared_counts"]["swift-testing"],
            len(swift_tests),
        )


    def test_swift_list_gate_rejects_missing_duplicate_and_ambiguous_tests(self) -> None:
        expected = [
            swift_report.ExpectedTest("xctest", "testOne"),
            swift_report.ExpectedTest(
                "swift-testing",
                "phaseSwift",
                "Phase Swift display",
            ),
        ]
        listing = self.root / "swift-list.log"
        listing.write_text(
            "\n".join(test.listing_id for test in expected) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(swift_report.verify_listing(listing, expected), 2)
        listing.write_text(expected[0].listing_id + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "expected_swift_test_not_discovered",
        ):
            swift_report.verify_listing(listing, expected)
        listing.write_text(
            expected[0].listing_id + "\n" + expected[0].listing_id + "\n"
            + expected[1].listing_id + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "expected_swift_test_discovery_duplicate",
        ):
            swift_report.verify_listing(listing, expected)


    def test_swift_xunit_gate_proves_all_kinds_and_rejects_bad_evidence(self) -> None:
        report = self.root / "swift.xml"
        log = self.root / "swift.log"
        xctest_expected = [
            swift_report.ExpectedTest("xctest", "testOne"),
            swift_report.ExpectedTest("xctest", "testTwo"),
        ]
        swift_testing_expected = [
            swift_report.ExpectedTest(
                "swift-testing",
                "phaseSwift",
                "Phase Swift display",
            ),
        ]
        xctest_class = 'classname="PAMInteractionModelTests.Phase2ContextModelsTests"'
        valid_cases = (
            f'<testcase {xctest_class} name="testOne" />'
            f'<testcase {xctest_class} name="testTwo" />'
        )

        def xunit_document(
            cases: str,
            *,
            tests: int,
            failures: int = 0,
            errors: int = 0,
            skipped: int = 0,
        ) -> str:
            return (
                "<testsuites>"
                f'<testsuite tests="{tests}" failures="{failures}" '
                f'errors="{errors}" skipped="{skipped}">'
                f"{cases}</testsuite></testsuites>"
            )

        report.write_text(
            xunit_document(valid_cases, tests=2),
            encoding="utf-8",
        )
        log.write_text("Executed 2 tests, with 0 failures\n", encoding="utf-8")
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=xctest_expected),
            2,
        )
        report.write_text(
            xunit_document(
                '<testcase classname="PAMInteractionModelTests" '
                'name="phaseSwift()" />',
                tests=1,
            ),
            encoding="utf-8",
        )
        log.write_text(
            "✔ Test run with 1 test passed after 0.01 seconds.\n",
            encoding="utf-8",
        )
        self.assertEqual(
            swift_report.verify(
                report,
                log,
                1,
                expected_tests=swift_testing_expected,
            ),
            1,
        )

        report.write_text(
            xunit_document(
                f'<testcase {xctest_class} name="testOne" />'
                f'<testcase {xctest_class} name="testOne" />',
                tests=2,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "expected_swift_test_duplicate",
        ):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)

        report.write_text(
            xunit_document(
                f'<testcase {xctest_class} name="testOne" />'
                f'<testcase {xctest_class} name="other" />',
                tests=2,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "unexpected_swift_test_executed",
        ):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)

        report.write_text(
            xunit_document(
                f'<testcase {xctest_class} name="testOne" status="notrun" />'
                f'<testcase {xctest_class} name="testTwo" />',
                tests=2,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_not_executed"):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)

        report.write_text(
            xunit_document(
                f'<testcase {xctest_class} name="testOne"><skipped />'
                f'</testcase><testcase {xctest_class} name="testTwo" />',
                tests=2,
                skipped=1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_skipped"):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)

        report.write_text(
            xunit_document(
                f'<testcase {xctest_class} name="testOne"><failure />'
                f'</testcase><testcase {xctest_class} name="testTwo" />',
                tests=2,
                failures=1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_failed"):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)

        report.write_text(
            xunit_document(
                f'<testcase {xctest_class} name="testOne"><error />'
                f'</testcase><testcase {xctest_class} name="testTwo" />',
                tests=2,
                errors=1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_failed"):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)

        report.write_text(
            xunit_document("", tests=0),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_test_count_not_exact"):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)
        report.write_text(
            xunit_document(valid_cases, tests=2),
            encoding="utf-8",
        )
        log.write_text("Test run: 1 test skipped\n", encoding="utf-8")
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_skipped"):
            swift_report.verify(report, log, 2, expected_tests=xctest_expected)


    def test_release_privacy_scan_redacts_high_confidence_secret_findings(self) -> None:
        candidates = {
            "sk_api_token": "sk-" + ("a" * 24),
            "github_token": "ghp_" + ("A" * 36),
            "gitlab_token": "glpat-" + ("B" * 24),
            "aws_access_key": "AKIA" + ("C" * 16),
            "google_api_key": "AIza" + ("D" * 35),
            "slack_token": "xoxb-" + ("1" * 12) + "-" + ("E" * 20),
            "stripe_live_secret": "sk_live_" + ("F" * 24),
            "npm_token": "npm_" + ("G" * 36),
            "private_key_header": "-----BEGIN " + "PRIVATE KEY-----",
        }
        for expected_rule, candidate in candidates.items():
            findings = privacy_scan._secret_findings(
                "synthetic/fixture.txt",
                "safe line\ncredential=" + candidate + "\n",
            )
            self.assertTrue(
                any(expected_rule in finding for finding in findings),
                (expected_rule, findings),
            )
            self.assertTrue(all(candidate not in finding for finding in findings))
            self.assertTrue(
                all("synthetic/fixture.txt:2" in finding for finding in findings)
            )
        self.assertEqual(
            privacy_scan._secret_findings(
                "synthetic/safe.txt",
                "sk-example\nTOKEN_REDACTED\ngithub_pat_placeholder\n",
            ),
            [],
        )
        self.assertTrue(privacy_scan._is_forbidden_environment_file("private/.env"))
        self.assertTrue(
            privacy_scan._is_forbidden_environment_file("private/.env.production")
        )
        self.assertFalse(
            privacy_scan._is_forbidden_environment_file("config/.env.example")
        )
        synthetic = "sk-" + ("y" * 24)
        synthetic_findings = privacy_scan._secret_findings(
            "core/tests/test_core.py",
            "value=" + synthetic,
        )
        self.assertTrue(synthetic_findings)
        self.assertTrue(all(synthetic not in row for row in synthetic_findings))
        alternate = "sk-" + ("z" * 24)
        alternate_findings = privacy_scan._secret_findings(
            "core/tests/test_core.py",
            "fake=" + synthetic + " real=" + alternate,
        )
        self.assertTrue(alternate_findings)
        self.assertTrue(all(alternate not in row for row in alternate_findings))


    def test_git_object_core_materializer_ignores_worktree_and_preserves_modes(self) -> None:
        repository, config = self._core_git_fixture()
        (repository / "core" / "notes.txt").write_text(
            "tampered worktree bytes\n", encoding="utf-8"
        )
        (repository / "core" / "ignored.tmp").write_text(
            "ignored debris\n", encoding="utf-8"
        )
        (repository / "core" / "untracked.tmp").write_text(
            "untracked debris\n", encoding="utf-8"
        )
        incoming = self.root / "materialized" / "core"
        incoming.parent.mkdir()
        previous_umask = os.umask(0)
        try:
            identity = rollout._copy_candidate_core(config, incoming)
        finally:
            os.umask(previous_umask)
        self.assertEqual(
            (incoming / "notes.txt").read_text(encoding="utf-8"),
            "committed bytes\n",
        )
        self.assertFalse((incoming / "ignored.tmp").exists())
        self.assertFalse((incoming / "untracked.tmp").exists())
        self.assertEqual(rollout._mode(incoming), 0o755)
        self.assertEqual(rollout._mode(incoming / "bin"), 0o755)
        self.assertEqual(rollout._mode(incoming / "notes.txt"), 0o644)
        self.assertEqual(rollout._mode(incoming / "bin" / "tool.sh"), 0o755)
        self.assertEqual(
            identity["materialized"],
            {
                key: value
                for key, value in identity["git"].items()
                if key in {"entry_count", "logical_bytes", "content_mode_manifest_sha256"}
            },
        )


    def test_git_object_core_index_flags_fail_before_service_stop(self) -> None:
        repository, config = self._core_git_fixture()
        for enable, disable in (
            ("--skip-worktree", "--no-skip-worktree"),
            ("--assume-unchanged", "--no-assume-unchanged"),
        ):
            with self.subTest(flag=enable):
                self._git(repository, "update-index", enable, "core/notes.txt")
                with self.assertRaisesRegex(
                    rollout.RolloutError, "git_core_index_flags_unsafe"
                ):
                    rollout._git_core_manifest(config)
                self._git(repository, "update-index", disable, "core/notes.txt")
        install_source = inspect.getsource(rollout.install)
        self.assertLess(
            install_source.index("_copy_candidate_core"),
            install_source.index("_stop_app"),
        )


    def test_git_object_core_rejects_special_and_portability_collisions(self) -> None:
        config = self._config()
        object_id = "1" * 40
        payload = b"fixture"
        cases = {
            "symlink": f"120000 blob {object_id}\tcore/link",
            "gitlink": f"160000 commit {object_id}\tcore/submodule",
            "runtime_agents": f"100644 blob {object_id}\tcore/AGENTS.md",
            "runtime_data": f"100644 blob {object_id}\tcore/data/activity.sqlite",
            "non_nfc": f"100644 blob {object_id}\tcore/cafe\u0301.py",
            "unicode_control": f"100644 blob {object_id}\tcore/bad\u2060name.py",
            "case_collision": (
                f"100644 blob {object_id}\tcore/Name.py\0"
                f"100644 blob {object_id}\tcore/name.py"
            ),
        }
        for name, listing in cases.items():
            def fake_output(_config, *arguments, _listing=listing):
                if arguments[0:2] == ("rev-parse", "--show-object-format"):
                    return "sha1"
                if arguments[0] == "ls-tree":
                    return _listing
                if arguments[0] == "ls-files":
                    return ""
                raise AssertionError(arguments)

            with self.subTest(case=name), patch.object(
                rollout, "_git_output", side_effect=fake_output
            ), patch.object(rollout, "_git_blob", return_value=payload):
                with self.assertRaisesRegex(
                    rollout.RolloutError, "git_core_manifest_invalid"
                ):
                    rollout._git_core_manifest(config)


    def test_materialized_core_detects_content_and_mode_tampering(self) -> None:
        _repository, config = self._core_git_fixture()
        incoming = self.root / "twice" / "core"
        incoming.parent.mkdir()
        identity = rollout._copy_candidate_core(config, incoming)["materialized"]
        notes = incoming / "notes.txt"
        notes.write_text("changed after staging\n", encoding="utf-8")
        with self.assertRaisesRegex(
            rollout.RolloutError, "staged_git_core_identity_drift"
        ):
            rollout._assert_materialized_core_identity(incoming, identity)
        notes.write_text("committed bytes\n", encoding="utf-8")
        notes.chmod(0o666)
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "staged_git_core_file_permissions_invalid|staged_git_core_identity_drift",
        ):
            rollout._assert_materialized_core_identity(incoming, identity)

        notes.chmod(0o644)
        root_injection = incoming / "sqlite3.py"
        root_injection.write_text("raise RuntimeError('injected')\n", encoding="utf-8")
        root_injection.chmod(0o644)
        with self.assertRaisesRegex(
            rollout.RolloutError, "staged_git_core_identity_drift"
        ):
            rollout._assert_materialized_core_identity(incoming, identity)
        root_injection.unlink()
        cache = incoming / "activity_mvp" / "__pycache__"
        cache.mkdir(parents=True, mode=0o755)
        cache.parent.chmod(0o755)
        cache.chmod(0o755)
        bytecode = cache / "daemon.cpython-312.pyc"
        bytecode.write_bytes(b"injected bytecode")
        bytecode.chmod(0o644)
        with self.assertRaisesRegex(
            rollout.RolloutError, "staged_git_core_identity_drift"
        ):
            rollout._assert_materialized_core_identity(incoming, identity)


    def test_legacy_core_full_seal_rejects_import_shadows_native_code_and_orphan_pyc(self) -> None:
        def legal_core(name: str) -> Path:
            core = self.root / name
            package = core / "activity_mvp"
            cache = package / "__pycache__"
            cache.mkdir(parents=True)
            core.chmod(0o755)
            package.chmod(0o755)
            cache.chmod(0o755)
            for relative, payload in (
                ("AGENTS.md", "owner rules\n"),
                ("pyproject.toml", "[project]\nname='pam'\n"),
                ("activity_mvp/__init__.py", '__version__ = "0.3.22"\n'),
                ("activity_mvp/module.py", "VALUE = 1\n"),
            ):
                path = core / relative
                path.write_text(payload, encoding="utf-8")
                path.chmod(0o644)
            bytecode = cache / "module.cpython-312.pyc"
            bytecode.write_bytes(b"sealed bytecode fixture")
            bytecode.chmod(0o644)
            return core

        baseline = legal_core("baseline-core")
        identity_before = rollout._runtime_core_full_identity(baseline)
        (baseline / "activity_mvp" / "module.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        identity_after = rollout._runtime_core_full_identity(baseline)
        self.assertNotEqual(identity_after, identity_before)

        attacks = (
            ("sitecustomize", "sitecustomize.py", b"raise SystemExit\n", "runtime_core_root_import_shadow"),
            ("stdlib-file", "sqlite3.py", b"raise SystemExit\n", "runtime_core_root_import_shadow"),
            ("native-package", "activity_mvp/rogue.dylib", b"native", "runtime_core_native_extension_forbidden"),
            ("root-bytecode", "rogue.pyc", b"bytecode", "runtime_core_root_import_shadow"),
            (
                "orphan-bytecode",
                "activity_mvp/__pycache__/orphan.cpython-312.pyc",
                b"bytecode",
                "runtime_core_orphan_bytecode",
            ),
        )
        for name, relative, payload, error in attacks:
            with self.subTest(name=name):
                core = legal_core(f"attack-{name}")
                attack = core / relative
                attack.parent.mkdir(parents=True, exist_ok=True)
                attack.write_bytes(payload)
                attack.chmod(0o644)
                with self.assertRaisesRegex(rollout.RolloutError, error):
                    rollout._runtime_core_full_identity(core)

        stdlib_package = legal_core("attack-stdlib-package")
        shadow = stdlib_package / "json"
        shadow.mkdir(mode=0o755)
        (shadow / "__init__.py").write_text("raise SystemExit\n", encoding="utf-8")
        (shadow / "__init__.py").chmod(0o644)
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "runtime_core_root_allowlist_violation",
        ):
            rollout._runtime_core_full_identity(stdlib_package)


    def test_app_payload_identity_rejects_group_or_world_writable_entries(self) -> None:
        app = self.root / "Unsafe.app"
        app.mkdir(mode=0o755)
        payload = app / "payload"
        payload.write_text("safe bytes", encoding="utf-8")
        payload.chmod(0o666)
        with self.assertRaisesRegex(
            rollout.RolloutError, "identity_tree_permissions_invalid"
        ):
            rollout._tree_content_mode_hash(app)
        payload.chmod(0o644)
        app.chmod(0o777)
        with self.assertRaisesRegex(
            rollout.RolloutError, "identity_tree_permissions_invalid"
        ):
            rollout._tree_content_mode_hash(app)


    def test_runtime_database_identity_rejects_symlinked_data_root(self) -> None:
        core = self.root / "canonical-core"
        core.mkdir(mode=0o755)
        external = self.root / "external-data"
        external.mkdir(mode=0o700)
        database = external / "activity.sqlite"
        database.write_bytes(b"fixture")
        database.chmod(0o600)
        (core / "data").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(
            rollout.RolloutError, "runtime_database_path_invalid"
        ):
            rollout._canonical_runtime_database_identity(
                core, core / "data" / "activity.sqlite"
            )
        (core / "data").unlink()
        data = core / "data"
        data.mkdir(mode=0o700)
        canonical = data / "activity.sqlite"
        canonical.write_bytes(b"fixture")
        canonical.chmod(0o600)
        identity = rollout._canonical_runtime_database_identity(core, canonical)
        self.assertEqual(identity[2], 0o700)
        wal = Path(f"{canonical}-wal")
        shm = Path(f"{canonical}-shm")
        wal.write_bytes(b"live wal generation one")
        shm.write_bytes(b"live shm generation one")
        wal.chmod(0o600)
        shm.chmod(0o600)
        with_sidecars = rollout._canonical_runtime_database_identity(core, canonical)
        wal.write_bytes(b"live wal generation two with different size")
        shm.write_bytes(b"live shm generation two with different size")
        self.assertEqual(
            rollout._canonical_runtime_database_identity(core, canonical),
            with_sidecars,
        )
        replacement = data / "activity.sqlite.replacement"
        replacement.write_bytes(b"replacement database inode")
        replacement.chmod(0o600)
        os.replace(replacement, canonical)
        replaced = rollout._canonical_runtime_database_identity(core, canonical)
        self.assertNotEqual(replaced, with_sidecars)
        canonical.chmod(0o644)
        with self.assertRaisesRegex(
            rollout.RolloutError, "runtime_database_path_invalid"
        ):
            rollout._canonical_runtime_database_identity(core, canonical)


    def test_app_pid_proof_binds_text_vnode_and_restore_relaunches_canonical(self) -> None:
        config = self._config()
        executable = (
            config.installed_app / "Contents" / "MacOS" / "PersonalAIMonitor"
        )
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"canonical app executable")
        executable.chmod(0o755)
        expected = rollout._app_executable_snapshot(config)
        right = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout=(
                f"ftxt\nD{expected[1]:#x}\ni{expected[2]}\nn{expected[0]}\n"
            ),
            stderr="",
        )
        wrong = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout=(
                f"ftxt\nD{expected[1]:#x}\ni{expected[2] + 1}\nn{expected[0]}\n"
            ),
            stderr="",
        )
        process_identity = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout=f"{os.getuid()} Mon Jan  1 00:00:00 2024\n",
            stderr="",
        )
        with patch.object(
            rollout,
            "_run",
            side_effect=[process_identity, right, process_identity],
        ):
            rollout._verify_app_pid_executable(config, 42, expected)
        with patch.object(
            rollout,
            "_run",
            side_effect=[process_identity, wrong, process_identity],
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError, "installed_app_process_image_mismatch"
            ):
                rollout._verify_app_pid_executable(config, 41, expected)

        stop = Mock()
        prove = Mock()
        open_result = subprocess.CompletedProcess(
            ["open"], 0, stdout="", stderr=""
        )
        with patch.object(
            rollout, "_app_pids", side_effect=[{41}, {42}]
        ), patch.object(
            rollout, "_stop_app", stop
        ), patch.object(
            rollout, "_assert_app_stopped"
        ), patch.object(
            rollout, "_app_executable_snapshot", return_value=expected
        ), patch.object(
            rollout, "_verify_app_pid_executable", prove
        ), patch.object(
            rollout, "_run", return_value=open_result
        ):
            rollout._restore_app_state(config, True)
            stop.assert_called_once_with(
                config,
                force=True,
                allowed_stale_snapshots=(),
            )
        prove.assert_called_once_with(config, 42, expected)


    def test_storage_headroom_aggregates_bulk_recovery_and_exact_reserve(self) -> None:
        config = self._config()
        anchor = self.root
        gib = 1024 * 1024 * 1024
        requirements = [
            rollout.StorageRequirement(anchor / "one", 100, 20, 400),
            rollout.StorageRequirement(anchor / "two", 20, 0, 0),
        ]
        with patch.object(
            rollout, "_storage_requirement_entries", return_value=requirements
        ), patch.object(
            rollout, "_nearest_existing_parent", return_value=anchor
        ), patch.object(
            rollout.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=gib + 140, f_frsize=1),
        ):
            result = rollout._storage_headroom(config, "install")
        volume = result["volumes"][0]
        self.assertEqual(volume["required_bytes"], 120)
        self.assertEqual(volume["recovery_bytes"], 20)
        self.assertEqual(volume["reserve_bytes"], gib)
        self.assertEqual(volume["available_after_bytes"], gib)
        with patch.object(
            rollout, "_storage_requirement_entries", return_value=requirements
        ), patch.object(
            rollout, "_nearest_existing_parent", return_value=anchor
        ), patch.object(
            rollout.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=gib + 139, f_frsize=1),
        ):
            with self.assertRaisesRegex(
                rollout.RolloutError, "insufficient_storage_headroom"
            ):
                rollout._storage_headroom(config, "install")
        large = [rollout.StorageRequirement(anchor, 1, 0, 8 * gib)]
        with patch.object(
            rollout, "_storage_requirement_entries", return_value=large
        ), patch.object(
            rollout, "_nearest_existing_parent", return_value=anchor
        ), patch.object(
            rollout.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=3 * gib, f_frsize=1),
        ):
            large_result = rollout._storage_headroom(config, "install")
        self.assertEqual(large_result["volumes"][0]["reserve_bytes"], 2 * gib)


    def test_rollback_storage_uses_one_database_copy_only_when_required(self) -> None:
        config = self._config()
        artifacts = rollout._rollout_artifacts(config, "a" * 32)
        artifacts.rollback_database.parent.mkdir(parents=True, mode=0o700)
        self._prepare_schema28(artifacts.rollback_database)
        probe = rollout._sqlite_probe(
            artifacts.rollback_database, expected_schema=rollout.SOURCE_SCHEMA
        )
        state = {
            "phase": "installed",
            "rollback_database_sha256": probe["sha256"],
        }
        healthy = rollout._rollback_storage_requirement_entries(
            config,
            state,
            artifacts,
            {"rollback_database_recovery_required": False},
        )
        rebuild = rollout._rollback_storage_requirement_entries(
            config,
            state,
            artifacts,
            {"rollback_database_recovery_required": True},
        )
        healthy_core = next(row for row in healthy if row.destination == config.live_core.parent)
        rebuild_core = next(row for row in rebuild if row.destination == config.live_core.parent)
        self.assertEqual((healthy_core.required_bytes, healthy_core.recovery_bytes), (0, 0))
        self.assertEqual(
            rebuild_core.required_bytes,
            rollout._database_snapshot_storage_bytes(artifacts.rollback_database),
        )
        self.assertEqual(rebuild_core.recovery_bytes, 0)
        with self.assertRaisesRegex(
            rollout.RolloutError, "rollback_storage_recovery_status_invalid"
        ):
            rollout._rollback_storage_requirement_entries(
                config,
                state,
                artifacts,
                {"rollback_database_recovery_required": "unknown"},
            )


    def test_second_rollback_capacity_gate_safe_aborts_without_forward_mutation(self) -> None:
        case_root = self.root / "rollback-capacity-safe-abort"
        case_root.mkdir()
        config, services, ui = self._prepare_transaction_fixture(case_root)
        with ExitStack() as stack:
            self._enter_transaction_mocks(stack, config, services, ui)
            rollout.install(
                config,
                apply=True,
                confirmation=rollout.INSTALL_CONFIRMATION,
            )
            before = dict(services)
            ready = {"status": "ready", "volumes": []}
            capacity = stack.enter_context(
                patch.object(
                    rollout,
                    "_storage_headroom",
                    side_effect=[
                        ready,
                        rollout.RolloutError("insufficient_storage_headroom"),
                    ],
                )
            )
            forward = stack.enter_context(
                patch.object(rollout, "_complete_rollback_forward")
            )
            rename = stack.enter_context(patch.object(rollout, "_durable_rename"))
            rebuild = stack.enter_context(
                patch.object(rollout, "_restore_schema28_database_from_backup")
            )
            stack.enter_context(
                patch.object(
                    rollout,
                    "_read_release_gate_status_for_rollback",
                    return_value={"context_core_gate": "false"},
                )
            )
            with self.assertRaisesRegex(
                rollout.RolloutError, "insufficient_storage_headroom"
            ):
                rollout.rollback(
                    config,
                    apply=True,
                    confirmation=rollout.ROLLBACK_CONFIRMATION,
                )
        self.assertEqual(capacity.call_count, 2)
        forward.assert_not_called()
        rename.assert_not_called()
        rebuild.assert_not_called()
        self.assertEqual(services, before)
        state = rollout._load_state(config)
        self.assertEqual(state["phase"], "installed")
        self.assertEqual(
            state["last_rollback_attempt"]["status"],
            "pre_mutation_capacity_blocked",
        )


    def test_private_json_is_exclusive_and_valid_drift_uses_journal(self) -> None:
        snapshot = self.root / "private" / "ui.json"
        snapshot.parent.mkdir(mode=0o700)
        rollout._write_private_json(snapshot, {"exists": True, "value": True})
        with self.assertRaisesRegex(rollout.RolloutError, "private_snapshot_exists"):
            rollout._write_private_json(snapshot, {"exists": False, "value": None})
        snapshot.write_text('{"exists":true,"value":false}\n', encoding="utf-8")
        snapshot.chmod(0o600)
        restored: list[dict[str, object]] = []
        with patch.object(
            rollout,
            "_restore_ui_value",
            side_effect=lambda value: restored.append(dict(value)),
        ):
            source = rollout._restore_ui_flag_with_journal_fallback(
                snapshot, {"exists": True, "value": True}
            )
        self.assertEqual(source, "journal_fallback_snapshot_drift")
        self.assertEqual(restored, [{"exists": True, "value": True}])
        self.assertEqual(
            json.loads(snapshot.read_text(encoding="utf-8")),
            {"exists": True, "value": False},
        )
        corrupt = self.root / "private" / "ui-corrupt.json"
        corrupt.write_bytes(b'{"exists":true')
        corrupt.chmod(0o600)
        before = corrupt.stat()
        before_bytes = corrupt.read_bytes()
        restored.clear()
        with patch.object(
            rollout,
            "_restore_ui_value",
            side_effect=lambda value: restored.append(dict(value)),
        ):
            source = rollout._restore_ui_flag_with_journal_fallback(
                corrupt, {"exists": False, "value": None}
            )
        after = corrupt.stat()
        self.assertEqual(source, "journal_fallback")
        self.assertEqual(restored, [{"exists": False, "value": None}])
        self.assertEqual(corrupt.read_bytes(), before_bytes)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))


    def test_runtime_agents_copy_is_stable_nofollow_and_hash_bound(self) -> None:
        source = self.root / "source-core"
        destination = self.root / "destination-core"
        source.mkdir()
        destination.mkdir()
        agents = source / "AGENTS.md"
        agents.write_text("owner rules\n", encoding="utf-8")
        agents.chmod(0o644)
        expected = rollout._sha256_file(agents)
        self.assertEqual(
            rollout._copy_live_runtime_agents(
                source, destination, expected_sha256=expected
            ),
            expected,
        )
        self.assertEqual((destination / "AGENTS.md").read_bytes(), agents.read_bytes())
        linked = self.root / "linked-core"
        linked.mkdir()
        (linked / "AGENTS.md").symlink_to(agents)
        with self.assertRaisesRegex(rollout.RolloutError, "runtime_agents_file_invalid"):
            rollout._verify_runtime_agents(linked)


    def test_isolated_entrypoints_require_no_site_and_safe_path(self) -> None:
        rollout_guide = (REPO_ROOT / "docs" / "PHASE3_ROLLOUT.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("python3 tools/phase3_rollout.py", rollout_guide)
        direct = subprocess.run(
            [str(ROLLOUT_PATH), "--help"], check=False, capture_output=True, text=True
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        unisolated = subprocess.run(
            [sys.executable, str(ROLLOUT_PATH), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(unisolated.returncode, 2)
        self.assertIn("rollout_isolated_runtime_required", unisolated.stderr)

        launcher = REPO_ROOT / "core" / rollout.ISOLATED_LAUNCHER_NAME
        request = {
            "kind": "core",
            "label": rollout.SERVICE_LABEL,
            "program": sys.executable,
            "core_root": str(REPO_ROOT / "core"),
            "database": str(self.root / "activity.sqlite"),
            "stdout_path": str(self.root / "core.out"),
            "stderr_path": str(self.root / "core.err"),
            "environment": {
                "PATH": rollout.LAUNCH_AGENT_PATH,
                "PAM_PRIVACY_PROFILE": "personal_high_precision",
            },
        }
        rendered = subprocess.run(
            [sys.executable, "-I", "-S", str(launcher), "--render-launch-agent"],
            input=json.dumps(request),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        payload = json.loads(rendered.stdout)
        self.assertEqual(payload["ProgramArguments"][1:3], ["-I", "-S"])
        self.assertNotIn("PYTHONPATH", payload["EnvironmentVariables"])

        marker = self.root / "dyld-constructor.marker"
        source = self.root / "dyld-constructor.c"
        library = self.root / "dyld-constructor.dylib"
        c_path = marker.as_posix().replace("\\", "\\\\").replace('"', '\\"')
        source.write_text(
            "#include <stdio.h>\n"
            "__attribute__((constructor)) static void pam_marker(void) {\n"
            f'  FILE *handle = fopen("{c_path}", "a");\n'
            '  if (handle != NULL) { fputs("loaded\\n", handle); fclose(handle); }\n'
            "}\n",
            encoding="utf-8",
        )
        compiled = subprocess.run(
            ["/usr/bin/clang", "-dynamiclib", str(source), "-o", str(library)],
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C", "DEVELOPER_DIR": "/Library/Developer/CommandLineTools", "TMPDIR": str(self.root), "xcrun_nocache": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        trusted_python = Path(
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
        )
        injected_environment = {
            **os.environ,
            "DYLD_INSERT_LIBRARIES": str(library),
            "PYTHONPATH": str(self.root / "attacker-pythonpath"),
        }
        positive = subprocess.run(
            [str(trusted_python), "-I", "-S", "-c", "pass"],
            env=injected_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(positive.returncode, 0, positive.stderr)
        self.assertTrue(marker.is_file(), "DYLD positive control did not execute")
        before = marker.read_text(encoding="utf-8").count("loaded")
        protected = subprocess.run(
            [str(ROLLOUT_PATH), "--help"],
            env=injected_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        after = marker.read_text(encoding="utf-8").count("loaded")
        self.assertEqual(after, before)
        if protected.returncode != 0:
            # GitHub's macOS hosted image runs with SIP disabled and ships an
            # arm64e /usr/bin/env while this positive-control library is
            # arm64.  dyld may therefore reject the injected image before the
            # env -i launcher starts.  That is an accepted fail-closed result:
            # the constructor and rollout code both remain unexecuted.  Any
            # other launcher failure still fails this test.
            self.assertEqual(protected.returncode, -signal.SIGABRT)
            self.assertEqual(protected.stdout, "")
            self.assertIn("dyld", protected.stderr.lower())
            self.assertIn("incompatible architecture", protected.stderr.lower())
            self.assertIn(str(library), protected.stderr)
        else:
            self.assertIn("usage:", protected.stdout.lower())


    def test_late_plist_compensation_uses_sealed_legacy_not_new_canonical(self) -> None:
        config = self._config()
        config.launch_agent_plist.parent.mkdir(parents=True)
        artifacts = rollout._rollout_artifacts(config, "b" * 32)
        artifacts.core_rollbacks.mkdir(parents=True, mode=0o700)
        old_main = b"legacy-main"
        old_shadow = b"legacy-shadow"
        for canonical, snapshot, old in (
            (config.launch_agent_plist, artifacts.old_plist, old_main),
            (config.shadow_launch_agent_plist, artifacts.old_shadow_plist, old_shadow),
        ):
            canonical.write_bytes(old)
            canonical.chmod(0o600)
            rollout._copy_private_file(canonical, snapshot)
            canonical.write_bytes(b"isolated-v0.4.0-" + old)
            canonical.chmod(0o600)
        source_identity = {
            "launch_agent_plist_sha256": hashlib.sha256(old_main).hexdigest(),
            "shadow_launch_agent_plist_sha256": hashlib.sha256(old_shadow).hexdigest(),
            "shadow_core_database_pair": {"pair_sha256": "c" * 64},
            "python_interpreter": {"program": "/trusted/python3"},
        }
        agent = {
            "interpreter": source_identity["python_interpreter"],
            "shadow_pair": source_identity["shadow_core_database_pair"],
        }
        with patch.object(
            rollout, "_verify_core_launch_agent_path", return_value=agent
        ), patch.object(
            rollout, "_verify_shadow_launch_agent_path", return_value=agent
        ):
            rollout._ensure_legacy_launch_agent_seals(
                config,
                source_identity,
                artifacts,
                require_canonical_legacy=False,
            )
        rollout._activate_sealed_old_plist(
            current=config.launch_agent_plist,
            snapshot=artifacts.old_plist,
            failed=artifacts.failed_plist,
            failure_stem="failed-main",
        )
        rollout._activate_sealed_old_plist(
            current=config.shadow_launch_agent_plist,
            snapshot=artifacts.old_shadow_plist,
            failed=artifacts.failed_shadow_plist,
            failure_stem="failed-shadow",
        )
        self.assertEqual(config.launch_agent_plist.read_bytes(), old_main)
        self.assertEqual(config.shadow_launch_agent_plist.read_bytes(), old_shadow)
        self.assertTrue(artifacts.failed_plist.read_bytes().startswith(b"isolated-v0.4.0"))
        self.assertTrue(
            artifacts.failed_shadow_plist.read_bytes().startswith(b"isolated-v0.4.0")
        )


    def test_exclusive_rename_fsync_failure_preserves_renamed_artifact(self) -> None:
        source = self.root / "rename-source" / "payload"
        destination = self.root / "rename-destination" / "payload"
        source.parent.mkdir()
        destination.parent.mkdir()
        source.write_text("durable-candidate", encoding="utf-8")
        with patch.object(
            rollout.os, "fsync", side_effect=OSError("injected fsync failure")
        ):
            with self.assertRaisesRegex(rollout.RolloutError, "durable_rename_failed"):
                rollout._durable_rename(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "durable-candidate")


    def test_release_build_wrapper_cleans_shell_path_git_and_compiler_environment(self) -> None:
        repository = self.root / "build-wrapper-fixture"
        scripts = repository / "scripts"
        core = repository / "core"
        scripts.mkdir(parents=True)
        core.mkdir()
        wrapper = scripts / "build-macos-app.sh"
        implementation = scripts / "build-macos-app-impl.sh"
        shutil.copy2(REPO_ROOT / "scripts" / wrapper.name, wrapper)
        shutil.copy2(REPO_ROOT / "scripts" / implementation.name, implementation)
        wrapper.chmod(0o755)
        implementation.chmod(0o644)
        (core / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        (core / "pyproject.toml").chmod(0o644)
        self._git(repository, "init", "-q")
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-q", "-m", "trusted build fixture")

        hooks = self.root / "hooks"
        hooks.mkdir()
        bash_marker = self.root / "bash-env-executed"
        function_marker = self.root / "imported-function-executed"
        path_marker = self.root / "fake-path-executed"
        bash_hook = hooks / "bash-hook.sh"
        bash_hook.write_text(
            f"/usr/bin/touch '{bash_marker}'\nexit 77\n", encoding="utf-8"
        )
        fake_git = hooks / "git"
        fake_git.write_text(
            f"#!/bin/sh\n/usr/bin/touch '{path_marker}'\nexit 78\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": str(hooks),
                "BASH_ENV": str(bash_hook),
                "ENV": str(bash_hook),
                "SDKROOT": "/tmp/untrusted-sdk",
                "GIT_INDEX_FILE": "/tmp/untrusted-index",
                "BASH_FUNC_cd%%": (
                    f"() {{ /usr/bin/touch '{function_marker}'; return 79; }}"
                ),
            }
        )
        isolated_build_root = self.root.resolve() / "isolated-build-scratch"
        isolated_build_root.mkdir(mode=0o700)
        result = subprocess.run(
            [str(wrapper), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git_dirty=false", result.stdout)
        self.assertFalse(bash_marker.exists())
        self.assertFalse(function_marker.exists())
        self.assertFalse(path_marker.exists())

        linked = self.root / "linked-build"
        linked.symlink_to(wrapper)
        linked_result = subprocess.run(
            [str(linked), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(linked_result.returncode, 2)
        self.assertIn("symlinked build entrypoint", linked_result.stderr)

        tracked_positions = (
            "core/pyproject.toml",
            "scripts/build-macos-app-impl.sh",
            "scripts/build-macos-app.sh",
        )
        for enable, disable in (
            ("--skip-worktree", "--no-skip-worktree"),
            ("--assume-unchanged", "--no-assume-unchanged"),
        ):
            for tracked in tracked_positions:
                with self.subTest(index_flag=enable, tracked=tracked):
                    self._git(repository, "update-index", enable, tracked)
                    blocked = subprocess.run(
                        [str(wrapper), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(blocked.returncode, 0)
                    self._git(repository, "update-index", disable, tracked)

        original = (core / "pyproject.toml").read_bytes()
        (core / "pyproject.toml").write_bytes(b"tampered tracked bytes\n")
        byte_drift = subprocess.run(
            [str(wrapper), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(byte_drift.returncode, 0)
        (core / "pyproject.toml").write_bytes(original)
        (core / "pyproject.toml").chmod(0o755)
        mode_drift = subprocess.run(
            [str(wrapper), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(mode_drift.returncode, 0)
        (core / "pyproject.toml").chmod(0o644)

        wrapper_text = wrapper.read_text(encoding="utf-8")
        implementation_text = implementation.read_text(encoding="utf-8")
        self.assertTrue(wrapper_text.startswith("#!/usr/bin/env -S -i "))
        self.assertIn("hash-object --no-filters", wrapper_text)
        self.assertIn("umask 022", implementation_text)
        self.assertIn(
            'export CLANG_MODULE_CACHE_PATH="$SWIFT_CACHE_ROOT/clang-module-cache"',
            implementation_text,
        )


    def test_ci_push_branch_and_cross_toolchain_gates_are_blocking(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [main, release/v0.4.0-context-console]", workflow)
        self.assertIn("runner: macos-14", workflow)
        self.assertIn("/Applications/Xcode_15.4.app/Contents/Developer", workflow)
        self.assertIn("runner: macos-15", workflow)
        self.assertIn("/Applications/Xcode_16.4.app/Contents/Developer", workflow)
        self.assertIn("bash ./tools/run-swift-release-tests.sh", workflow)
        self.assertIn("PAM_SWIFT_TEST_EVIDENCE_DIR", workflow)
        self.assertIn('require_swift_testing: "0"', workflow)
        self.assertIn('require_swift_testing: "1"', workflow)
        self.assertIn("PAM_REQUIRE_SWIFT_TESTING", workflow)
        self.assertIn(
            "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f # v6",
            workflow,
        )
        self.assertNotIn("actions/upload-artifact@v6", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertNotIn("continue-on-error", workflow)
        gate = (REPO_ROOT / "tools/run-swift-release-tests.sh").read_text(encoding="utf-8")
        self.assertIn("swift-release-test-baseline.json", gate)
        self.assertNotIn("MINIMUM_XCTESTS", gate)
        self.assertIn("PAM_SWIFT_TEST_EVIDENCE_DIR", gate)
        self.assertIn("PAM_REQUIRE_SWIFT_TESTING", gate)
        self.assertIn("probe_strategy=required_expected_set", gate)
        self.assertIn("validation=swiftpm_listing_and_exact_execution", gate)
        self.assertIn("swift_testing_policy=required", gate)
        self.assertIn("swift_testing_expected_set=available", gate)
        self.assertIn("verify_swift_testing_evidence", gate)
        self.assertIn("--swift-testing-module", gate)
        self.assertIn("--expected-kind xctest", gate)
        self.assertGreaterEqual(gate.count("verify_swift_test_report.py"), 4)
        self.assertEqual(gate.count("--parallel --num-workers 1"), 2)



if __name__ == "__main__": unittest.main()
