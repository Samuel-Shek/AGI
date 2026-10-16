"""Unchanged native Swift fixtures; neither helper imports editable Python."""
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from activity_mvp.swift_helpers import ensure_helper, compiled_helper_command


class NativeSwiftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)


    def tearDown(self) -> None:
        self.temp.cleanup()


    @unittest.skipUnless(sys.platform == "darwin", "Apple Swift helper requires macOS")
    def test_real_vision_helper_compiles_to_mode_0700(self) -> None:
        binary = ensure_helper(self.root, "vision_ocr")
        self.assertTrue(binary.is_file())
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o700)
        self.assertEqual(compiled_helper_command(self.root, "vision_ocr"), [str(binary.resolve())])

    def test_swift_helper_keeps_one_shot_output_and_serves_two_jsonl_requests(self) -> None:
        source = Path(__file__).resolve().parents[1] / "tools" / "session_state.swift"
        with tempfile.TemporaryDirectory() as tempdir:
            binary = Path(tempdir) / "session_state"
            compiled = subprocess.run(
                ["xcrun", "swiftc", str(source), "-o", str(binary)],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)

            one_shot = subprocess.run(
                [str(binary)],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(one_shot.returncode, 0, one_shot.stderr)
            self.assertIsInstance(json.loads(one_shot.stdout), dict)
            self.assertGreater(len(one_shot.stdout.splitlines()), 1)

            served = subprocess.run(
                [str(binary), "--serve"],
                input=(
                    '{"type":"probe","nonce":"0123456789abcdef0123456789abcdef"}\n'
                    '{"type":"probe","nonce":"fedcba9876543210fedcba9876543210"}\n'
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(served.returncode, 0, served.stderr)
            lines = served.stdout.splitlines()
            self.assertEqual(len(lines), 2)
            for expected_nonce, line in zip(
                (
                    "0123456789abcdef0123456789abcdef",
                    "fedcba9876543210fedcba9876543210",
                ),
                lines,
                strict=True,
            ):
                response = json.loads(line)
                self.assertEqual(set(response), {"nonce", "session"})
                self.assertEqual(response["nonce"], expected_nonce)
                payload = response["session"]
                self.assertEqual(
                    set(payload),
                    {
                        "state",
                        "locked",
                        "on_console",
                        "evidence_source",
                        "cgs_lock_key_present",
                        "io_console_locked_present",
                    },
                )


if __name__ == "__main__": unittest.main()
