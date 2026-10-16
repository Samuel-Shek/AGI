from phase3_fixture import *


class Phase3NativeTests(Phase3Fixture):
    def test_force_stop_never_signals_forged_app_argv(self) -> None:
        config = self._config()
        executable = (
            config.installed_app / "Contents" / "MacOS" / "PersonalAIMonitor"
        )
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"trusted but not running")
        executable.chmod(0o755)
        snapshot = rollout._app_executable_snapshot(config)
        process = subprocess.Popen(
            [
                "/bin/bash",
                "-c",
                'exec -a "$1" /bin/sleep 30',
                "pam-forged-argv",
                str(executable),
            ]
        )
        try:
            time.sleep(0.1)
            with patch.object(
                rollout,
                "_quit_app",
                side_effect=rollout.RolloutError("app_quit_failed"),
            ), patch.object(rollout.time, "sleep"), patch.object(
                rollout.os, "kill"
            ) as kill:
                with self.assertRaisesRegex(
                    rollout.RolloutError, "app_process_identity_ambiguous"
                ):
                    rollout._stop_app(
                        config,
                        force=True,
                        allowed_stale_snapshots=(snapshot,),
                    )
            kill.assert_not_called()
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)



if __name__ == "__main__": unittest.main()
