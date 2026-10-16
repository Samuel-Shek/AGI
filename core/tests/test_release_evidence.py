from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


swift_report = _load_module(
    "pam_release_evidence_swift_report",
    REPO_ROOT / "tools" / "verify_swift_test_report.py",
)
privacy_scan = _load_module(
    "pam_release_evidence_privacy_scan",
    REPO_ROOT / "tools" / "release_privacy_scan.py",
)


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source_paths(self) -> tuple[Path, Path]:
        tests = (
            REPO_ROOT
            / "apps/PersonalAIMonitorMenuBar/Tests/PAMInteractionModelTests"
        )
        return (
            tests / "Phase2ContextModelsTests.swift",
            tests / "Phase2ContextModelsSwiftTestingTests.swift",
        )

    def _write_xunit(
        self,
        path: Path,
        names: list[str],
        *,
        kind: str = "xctest",
        tests: int | None = None,
        failures: int = 0,
        errors: int = 0,
        skipped: int = 0,
        case_failure: bool = False,
        case_error: bool = False,
        case_skipped: bool = False,
        case_status: str = "",
    ) -> None:
        count = len(names) if tests is None else tests
        root = ET.Element(
            "testsuites",
            tests=str(count),
            failures=str(failures),
            errors=str(errors),
            skipped=str(skipped),
        )
        suite = ET.SubElement(
            root,
            "testsuite",
            name="PAMInteractionModelTests",
            tests=str(count),
            failures=str(failures),
            errors=str(errors),
            skipped=str(skipped),
        )
        for index, name in enumerate(names):
            if kind == "xctest":
                case_name = name
                classname = "PAMInteractionModelTests.Phase2ContextModelsTests"
            elif kind == "swift-testing":
                case_name = f"{name}()"
                classname = "PAMInteractionModelTests"
            else:
                raise AssertionError(f"unsupported synthetic xUnit kind: {kind}")
            attributes = {"name": case_name, "classname": classname}
            if case_status and index == 0:
                attributes["status"] = case_status
            case = ET.SubElement(suite, "testcase", attributes)
            if index == 0 and case_failure:
                ET.SubElement(case, "failure")
            if index == 0 and case_error:
                ET.SubElement(case, "error")
            if index == 0 and case_skipped:
                ET.SubElement(case, "skipped")
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

    def test_tracked_baseline_is_exactly_current_65_plus_31_source_set(self) -> None:
        baseline_path = REPO_ROOT / "tools" / "swift-release-test-baseline.json"
        baseline = swift_report.load_baseline_tests(baseline_path)
        xctest_source, swift_source = self._source_paths()
        declared = swift_report.extract_expected_tests(xctest_source, swift_source)
        self.assertEqual(
            swift_report.validate_source_against_baseline(declared, baseline),
            baseline,
        )
        self.assertEqual(sum(test.kind == "xctest" for test in baseline), 65)
        self.assertEqual(sum(test.kind == "swift-testing" for test in baseline), 31)

        available = self.root / "available.json"
        swift_report.write_expected_tests(
            available,
            baseline,
            declared_tests=baseline,
            swift_testing_module="available",
        )
        self.assertEqual(swift_report.load_expected_tests(available), baseline)

        unavailable_rows = [test for test in baseline if test.kind == "xctest"]
        unavailable = self.root / "unavailable.json"
        swift_report.write_expected_tests(
            unavailable,
            unavailable_rows,
            declared_tests=baseline,
            swift_testing_module="unavailable",
        )
        self.assertEqual(swift_report.load_expected_tests(unavailable), unavailable_rows)

        with self.assertRaisesRegex(
            swift_report.ReportError,
            "swift_test_source_baseline_mismatch",
        ):
            swift_report.validate_source_against_baseline(declared[:-1], baseline)

    def test_duplicate_swift_display_alias_is_rejected(self) -> None:
        rows = [
            swift_report.ExpectedTest("swift-testing", "firstTest", "Same display"),
            swift_report.ExpectedTest("swift-testing", "secondTest", "Same display"),
        ]
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "expected_test_alias_ambiguous",
        ):
            swift_report._validate_expected_tests(rows)

    def test_listing_requires_anchored_fully_qualified_exact_set(self) -> None:
        expected = [
            swift_report.ExpectedTest("xctest", "testOne"),
            swift_report.ExpectedTest(
                "swift-testing",
                "phaseSwift",
                "Phase Swift display",
            ),
        ]
        listing = self.root / "list.log"
        listing.write_text(
            "Build complete!\n"
            "PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n"
            "PAMInteractionModelTests.phaseSwift()\n",
            encoding="utf-8",
        )
        self.assertEqual(swift_report.verify_listing(listing, expected), 2)

        listing.write_text(
            "warning: PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n"
            "warning: PAMInteractionModelTests.phaseSwift()\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "expected_swift_test_not_discovered",
        ):
            swift_report.verify_listing(listing, expected)

        listing.write_text(
            "PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n"
            "PAMInteractionModelTests.phaseSwift()\n"
            "PAMInteractionModelTests.unexpectedTest()\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "unexpected_swift_test_discovered",
        ):
            swift_report.verify_listing(listing, expected)

        invalid_extra_lines = (
            "OtherModule.OtherSuite/testUnexpected",
            "\u200bPAMInteractionModelTests.OtherSuite/testUnexpected",
            "\u2060PAMInteractionModelTests.OtherSuite/testUnexpected",
            "\u00a0PAMInteractionModelTests.OtherSuite/testUnexpected",
            " PAMInteractionModelTests.OtherSuite/testUnexpected",
        )
        exact = (
            "PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n"
            "PAMInteractionModelTests.phaseSwift()\n"
        )
        for extra in invalid_extra_lines:
            with self.subTest(extra=extra.encode("unicode_escape").decode("ascii")):
                listing.write_text(exact + extra + "\n", encoding="utf-8")
                with self.assertRaises(swift_report.ReportError):
                    swift_report.verify_listing(listing, expected)

    def test_testing_probe_only_allows_exact_no_such_module_downgrade(self) -> None:
        probe = self.root / "probe.log"
        probe.write_text("", encoding="utf-8")
        self.assertEqual(swift_report.classify_testing_probe(probe, 0), "available")

        probe.write_text(
            "<stdin>:1:8: error: no such module 'Testing'\n"
            "import Testing\n"
            "       ^\n",
            encoding="utf-8",
        )
        self.assertEqual(swift_report.classify_testing_probe(probe, 1), "unavailable")

        probe.write_text(
            "<stdin>:1:8: error: no such module 'Testing'\n"
            "<stdin>:1:1: error: compiler service unavailable\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "swift_testing_module_probe_failed",
        ):
            swift_report.classify_testing_probe(probe, 1)

        probe.write_text(
            "<stdin>:1:1: error: compiler crashed while reading import Testing\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "swift_testing_module_probe_failed",
        ):
            swift_report.classify_testing_probe(probe, 1)

    def _write_fake_swift_release_tools(self, fake_bin: Path) -> None:
        fake_bin.mkdir()
        fake_xcrun = fake_bin / "xcrun"
        fake_swift = fake_bin / "swift"
        fake_xcrun.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >>"$PAM_FAKE_XCRUN_LOG"\n'
            'if [ "$1" = "--find" ] && [ "$2" = "xctest" ]; then\n'
            "  printf '/synthetic/xctest\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        fake_swift.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

args = sys.argv[1:]
scenario = os.environ["PAM_FAKE_SWIFT_SCENARIO"]
baseline = json.loads(Path(os.environ["PAM_FAKE_BASELINE"]).read_text(encoding="utf-8"))
xctests = baseline["xctest"]
swift_tests = [row["identifier"] for row in baseline["swift_testing"]]
with Path(os.environ["PAM_FAKE_SWIFT_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")

def write_xunit(
    path,
    names,
    *,
    kind="xctest",
    case_status="",
    namespaced_case_status="",
    uppercase_case_status="",
):
    root = ET.Element(
        "testsuites",
        tests=str(len(names)),
        failures="0",
        errors="0",
        skipped="0",
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        name="PAMInteractionModelTests",
        tests=str(len(names)),
        failures="0",
        errors="0",
        skipped="0",
    )
    for index, name in enumerate(names):
        if kind == "xctest":
            rendered_name = name
            classname = "PAMInteractionModelTests.Phase2ContextModelsTests"
        else:
            rendered_name = f"{name}()"
            classname = "PAMInteractionModelTests"
        if scenario in {"unicode_xunit_identity", "combined_unicode_bypass"}:
            rendered_name = f"\u00a0{rendered_name}\u3000"
            classname = "OtherModule.OtherSuite"
        attributes = {"name": rendered_name, "classname": classname}
        if case_status and index == 0:
            attributes["status"] = case_status
        if namespaced_case_status and index == 0:
            attributes["{urn:pam:adversarial}status"] = namespaced_case_status
        if uppercase_case_status and index == 0:
            attributes["Status"] = uppercase_case_status
        ET.SubElement(suite, "testcase", attributes)
    if scenario == "extra_empty_suite":
        ET.SubElement(
            root,
            "testsuite",
            name="UnexpectedEmptySuite",
            tests="0",
            failures="0",
            errors="0",
            skipped="0",
        )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

if args[-1] in {"list", "--list-tests"}:
    if scenario == "listing_failure":
        raise SystemExit(7)
    print("Build complete! (0.01s)")
    for name in xctests:
        print(f"PAMInteractionModelTests.Phase2ContextModelsTests/{name}")
    for name in swift_tests:
        print(f"PAMInteractionModelTests.{name}()")
    if scenario in {"unicode_listing_bypass", "combined_unicode_bypass"}:
        print("\u200bPAMInteractionModelTests.OtherSuite/testUnexpected")
    raise SystemExit(0)

if "--xunit-output" not in args:
    raise SystemExit(8)
if scenario == "execution_nonzero":
    print("synthetic Swift test execution failure", file=sys.stderr)
    raise SystemExit(9)

report = Path(args[args.index("--xunit-output") + 1])
write_xunit(
    report,
    xctests,
    kind="xctest",
    case_status="failed" if scenario == "failed_xunit_status" else "",
    namespaced_case_status=(
        "failed" if scenario == "namespaced_failed_xunit_status" else ""
    ),
    uppercase_case_status=(
        "failed" if scenario == "uppercase_failed_xunit_status" else ""
    ),
)
if scenario != "summary_only_xctest":
    for index, name in enumerate(xctests, start=1):
        if scenario in {"unicode_progress_bypass", "combined_unicode_bypass"}:
            print(
                f"[{index}/{len(xctests)}]\u00a0Testing "
                f"OtherModule.OtherSuite/testUnexpected{index}"
            )
        else:
            print(
                f"[{index}/{len(xctests)}] Testing "
                f"PAMInteractionModelTests.Phase2ContextModelsTests/{name}"
            )
if scenario in {"unicode_progress_bypass", "combined_unicode_bypass"}:
    print(f"Executed {len(xctests)} tests, with 0 failures")
if scenario == "summary_only_xctest":
    print(f"Executed {len(xctests)} tests, with 0 failures")
if scenario in {"unicode_failure_bypass", "combined_unicode_bypass"}:
    print("\u00a0Testing failed: hidden semantic failure")
    print("\u00a0✘ hidden semantic failure")
if "--filter" in args:
    if "--disable-swift-testing" not in args or scenario in {"unexpected_empty_filtered_swift_report", "nonempty_filtered_swift_report"}:
        filtered_swift_tests = (
            swift_tests[:1]
            if scenario == "nonempty_filtered_swift_report"
            else []
        )
        write_xunit(
            report.with_name("xctest-swift-testing.xml"),
            filtered_swift_tests,
            kind="swift-testing",
        )
        print(
            f"✔ Test run with {len(filtered_swift_tests)} tests "
            "passed after 0.1 seconds."
        )
else:
    if scenario != "missing_swift_report":
        write_xunit(
            report.with_name("full-swift-testing.xml"),
            swift_tests,
            kind="swift-testing",
        )
    print(f"✔ Test run with {len(swift_tests)} tests in 1 suite passed after 0.1 seconds.")
""",
            encoding="utf-8",
        )
        fake_xcrun.chmod(0o755)
        fake_swift.chmod(0o755)

    def test_required_testing_policy_uses_exact_package_context_evidence(self) -> None:
        fake_bin = self.root / "bin"
        self._write_fake_swift_release_tools(fake_bin)
        scenarios = {
            "success": True,
            "listing_failure": False,
            "execution_nonzero": False,
            "unexpected_empty_filtered_swift_report": False,
            "nonempty_filtered_swift_report": False,
            "missing_swift_report": False,
            "failed_xunit_status": False,
            "namespaced_failed_xunit_status": False,
            "uppercase_failed_xunit_status": False,
            "unicode_xunit_identity": False,
            "unicode_listing_bypass": False,
            "unicode_progress_bypass": False,
            "unicode_failure_bypass": False,
            "combined_unicode_bypass": False,
            "extra_empty_suite": False,
            "summary_only_xctest": False,
        }
        for scenario, expected_success in scenarios.items():
            with self.subTest(scenario=scenario):
                evidence = self.root / f"evidence-{scenario}"
                evidence.mkdir()
                xcrun_log = self.root / f"xcrun-{scenario}.log"
                swift_log = self.root / f"swift-{scenario}.log"
                env = dict(os.environ)
                env.update(
                    {
                        "PATH": f"{fake_bin}:{env['PATH']}",
                        "PAM_FAKE_XCRUN_LOG": str(xcrun_log),
                        "PAM_FAKE_SWIFT_LOG": str(swift_log),
                        "PAM_FAKE_SWIFT_SCENARIO": scenario,
                        "PAM_FAKE_BASELINE": str(
                            REPO_ROOT / "tools/swift-release-test-baseline.json"
                        ),
                        "PAM_REQUIRE_SWIFT_TESTING": "1",
                        "PAM_SWIFT_TEST_EVIDENCE_DIR": str(evidence),
                    }
                )
                result = subprocess.run(
                    ["bash", str(REPO_ROOT / "tools/run-swift-release-tests.sh")],
                    cwd=REPO_ROOT,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, expected_success, result.stderr)
                self.assertNotIn("swiftc", xcrun_log.read_text(encoding="utf-8"))
                probe = (evidence / "swift-testing-probe.log").read_text(
                    encoding="utf-8"
                )
                self.assertIn("swift_testing_policy=required", probe)
                self.assertIn(
                    "validation=swiftpm_listing_and_exact_execution", probe
                )
                self.assertNotIn("swift_testing_module=available", probe)
                gate_status = json.loads(
                    (evidence / "gate-status.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    gate_status["status"], "passed" if expected_success else "failed"
                )
                if expected_success:
                    summary = json.loads(
                        (evidence / "summary.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(summary["expected_total"], 96)
                    self.assertEqual(summary["executed_filtered_swift_testing"], 0)
                    self.assertEqual(summary["executed_full_xctest"], 65)
                    self.assertEqual(summary["executed_full_swift_testing"], 31)
                elif scenario == "missing_swift_report":
                    self.assertFalse((evidence / "full-swift-testing.xml").exists())
                elif scenario == "unexpected_empty_filtered_swift_report":
                    self.assertTrue((evidence / "xctest-swift-testing.xml").exists())

    def test_verifier_real_cli_smoke_for_manifest_and_probe(self) -> None:
        verifier = REPO_ROOT / "tools" / "verify_swift_test_report.py"
        xctest_source, swift_source = self._source_paths()
        expected_path = self.root / "expected-tests.json"
        manifest = subprocess.run(
            [
                sys.executable,
                str(verifier),
                "--write-expected-tests",
                str(expected_path),
                "--xctest-source",
                str(xctest_source),
                "--swift-testing-source",
                str(swift_source),
                "--baseline",
                str(REPO_ROOT / "tools" / "swift-release-test-baseline.json"),
                "--swift-testing-module",
                "available",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(manifest.returncode, 0, manifest.stderr)
        self.assertEqual(
            json.loads(manifest.stdout),
            {
                "swift-testing": 31,
                "swift_testing_module": "available",
                "total": 96,
                "xctest": 65,
            },
        )
        self.assertEqual(len(swift_report.load_expected_tests(expected_path)), 96)

        probe_path = self.root / "probe-cli.log"
        probe_path.write_text(
            "<stdin>:1:8: error: no such module 'Testing'\n",
            encoding="utf-8",
        )
        probe = subprocess.run(
            [
                sys.executable,
                str(verifier),
                "--probe-output",
                str(probe_path),
                "--probe-exit-code",
                "1",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "unavailable")

    def test_verifier_cli_rejects_mixed_duplicate_and_abbreviated_modes(self) -> None:
        verifier = REPO_ROOT / "tools" / "verify_swift_test_report.py"
        xctest_source, swift_source = self._source_paths()
        expected_path = self.root / "expected-tests.json"
        declared = swift_report.extract_expected_tests(xctest_source, swift_source)
        swift_report.write_expected_tests(
            expected_path,
            declared,
            declared_tests=declared,
            swift_testing_module="available",
        )
        listing = self.root / "listing.log"
        listing.write_text("synthetic\n", encoding="utf-8")
        probe = self.root / "probe.log"
        probe.write_text("", encoding="utf-8")
        report = self.root / "report.xml"
        log = self.root / "run.log"
        names = self.root / "expected-names.txt"
        self._write_xunit(report, ["testSynthetic"])
        log.write_text(
            "[1/1] Testing "
            "PAMInteractionModelTests.Phase2ContextModelsTests/testSynthetic\n",
            encoding="utf-8",
        )
        names.write_text("testSynthetic\n", encoding="utf-8")

        invalid_argv = (
            (
                "--list-output",
                str(listing),
                "--expected-tests",
                str(expected_path),
                "--report",
                str(self.root / "missing.xml"),
                "--log",
                str(self.root / "missing.log"),
                "--require-exact-progress",
            ),
            (
                "--probe-output",
                str(probe),
                "--probe-exit-code",
                "0",
                "--list-output",
                str(self.root / "missing-list.log"),
            ),
            (
                "--report",
                str(self.root / "missing.xml"),
                "--report",
                str(report),
                "--log",
                str(log),
                "--minimum",
                "1",
                "--expected-names",
                str(names),
            ),
            (
                f"--report={self.root / 'missing.xml'}",
                f"--report={report}",
                "--log",
                str(log),
                "--minimum",
                "1",
                "--expected-names",
                str(names),
            ),
            (
                "--list-output",
                str(listing),
                "--expected-tests",
                str(expected_path),
                "--expected-names",
                str(names),
            ),
            (
                "--report",
                str(report),
                "--log",
                str(log),
                "--minimum",
                "1",
                "--expected-names",
                str(names),
                "--require-exact-progress",
                "--require-exact-progress",
            ),
            ("--expected-tests", str(expected_path)),
            ("--rep", str(report)),
            (
                "--help",
                "--probe-output",
                str(probe),
                "--probe-exit-code",
                "0",
            ),
        )
        for argv in invalid_argv:
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [sys.executable, str(verifier), *argv],
                    cwd=REPO_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_invalid_utf8_release_evidence_is_rejected(self) -> None:
        expected = [swift_report.ExpectedTest("xctest", "testOne")]
        listing = self.root / "listing-invalid-utf8.log"
        listing.write_bytes(
            b"PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n\xff"
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "swift_test_list_control_character",
        ):
            swift_report.verify_listing(listing, expected)

        report = self.root / "report-invalid-utf8.xml"
        report.write_bytes(b"<testsuites>\xff</testsuites>")
        log = self.root / "log-invalid-utf8.log"
        log.write_text(
            "[1/1] Testing "
            "PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "xunit_report_invalid"):
            swift_report.verify(report, log, 1, expected_tests=expected)

        self._write_xunit(report, ["testOne"])
        log.write_bytes(
            b"[1/1] Testing "
            b"PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n\xff"
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "test_log_control_character",
        ):
            swift_report.verify(report, log, 1, expected_tests=expected)

    def test_xunit_requires_legal_exact_aggregate_and_nonempty_log(self) -> None:
        report = self.root / "report.xml"
        log = self.root / "test.log"
        expected = [
            swift_report.ExpectedTest("xctest", "testOne"),
            swift_report.ExpectedTest("xctest", "testTwo"),
        ]
        self._write_xunit(report, ["testOne", "testTwo"])
        log.write_text(
            "Executed 2 tests, with 0 failures (0 unexpected) in 0.1 seconds\n",
            encoding="utf-8",
        )
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=expected),
            2,
        )

        for padding in (" ", "\u00a0", "\u2003", "\u202f", "\u3000"):
            with self.subTest(xunit_name_padding=padding.encode("unicode_escape")):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                case = tree.getroot().find("testsuite/testcase")
                self.assertIsNotNone(case)
                case.set("name", f"{padding}testOne{padding}")
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "unexpected_swift_test_executed",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        for classname in (None, "OtherModule.OtherSuite", " PAMInteractionModelTests.Phase2ContextModelsTests"):
            with self.subTest(classname=classname):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                case = tree.getroot().find("testsuite/testcase")
                self.assertIsNotNone(case)
                if classname is None:
                    case.attrib.pop("classname")
                else:
                    case.set("classname", classname)
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "xunit_report_invalid_testcase|unexpected_swift_test_executed",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        ET.SubElement(
            tree.getroot(),
            "testsuite",
            name="UnexpectedEmptySuite",
            tests="0",
            failures="0",
            errors="0",
            skipped="0",
        )
        tree.write(report, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_invalid_structure",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        original_suite = tree.getroot().find("testsuite")
        self.assertIsNotNone(original_suite)
        second_suite = ET.SubElement(
            tree.getroot(),
            "testsuite",
            name="SplitSuite",
            tests="1",
            failures="0",
            errors="0",
            skipped="0",
        )
        second_suite.append(list(original_suite)[1])
        original_suite.set("tests", "1")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_invalid_structure",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        forged = self.root / "forged.xml"
        forged.write_text(
            '<root><testcase name="testOne"/><testcase name="testTwo"/></root>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_invalid_structure",
        ):
            swift_report.verify(forged, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        empty_log = self.root / "empty.log"
        empty_log.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(swift_report.ReportError, "test_log_empty"):
            swift_report.verify(report, empty_log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "otherTest"])
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "unexpected_swift_test_executed",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"], tests=3)
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_aggregate_mismatch",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(
            report,
            ["testOne", "testTwo"],
            skipped=1,
            case_skipped=True,
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_skipped"):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(
            report,
            ["testOne", "testTwo"],
            failures=1,
            case_failure=True,
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_failed"):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(
            report,
            ["testOne", "testTwo"],
            case_status="notrun",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "swift_tests_not_executed",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        rejected_statuses = {
            "failed": "swift_tests_failed",
            "failure": "swift_tests_failed",
            "error": "swift_tests_failed",
            "skipped ": "xunit_report_untrusted_status_attribute",
            "passed": "xunit_report_untrusted_status_attribute",
        }
        for status, error in rejected_statuses.items():
            with self.subTest(testcase_status=status):
                self._write_xunit(
                    report,
                    ["testOne", "testTwo"],
                    case_status=status,
                )
                with self.assertRaisesRegex(swift_report.ReportError, error):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        for result in ("failed", "success", "error "):
            with self.subTest(testcase_result=result):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                case = tree.getroot().find("testsuite/testcase")
                self.assertIsNotNone(case)
                case.set("result", result)
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaises(swift_report.ReportError):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        for location in ("root", "suite"):
            for attribute in ("status", "result"):
                with self.subTest(location=location, attribute=attribute):
                    self._write_xunit(report, ["testOne", "testTwo"])
                    tree = ET.parse(report)
                    target = (
                        tree.getroot()
                        if location == "root"
                        else tree.getroot().find("testsuite")
                    )
                    self.assertIsNotNone(target)
                    target.set(attribute, "failed")
                    tree.write(report, encoding="utf-8", xml_declaration=True)
                    with self.assertRaisesRegex(
                        swift_report.ReportError,
                        "swift_tests_failed",
                    ):
                        swift_report.verify(report, log, 2, expected_tests=expected)

        for location in ("root", "suite", "testcase"):
            for attribute in ("status", "result", "disabled", "executed"):
                with self.subTest(
                    namespaced_location=location,
                    namespaced_attribute=attribute,
                ):
                    self._write_xunit(report, ["testOne", "testTwo"])
                    tree = ET.parse(report)
                    targets = {
                        "root": tree.getroot(),
                        "suite": tree.getroot().find("testsuite"),
                        "testcase": tree.getroot().find("testsuite/testcase"),
                    }
                    target = targets[location]
                    self.assertIsNotNone(target)
                    target.set(f"{{urn:pam:adversarial}}{attribute}", "failed")
                    tree.write(report, encoding="utf-8", xml_declaration=True)
                    with self.assertRaisesRegex(
                        swift_report.ReportError,
                        "xunit_report_untrusted_attribute",
                    ):
                        swift_report.verify(report, log, 2, expected_tests=expected)

        casefold_attributes = {
            "Status": "failed",
            "RESULT": "error",
            "Disabled": "1",
            "Executed": "0",
            "NotRun": "1",
        }
        for location in ("root", "suite", "testcase"):
            for attribute, value in casefold_attributes.items():
                with self.subTest(
                    casefold_location=location,
                    casefold_attribute=attribute,
                ):
                    self._write_xunit(report, ["testOne", "testTwo"])
                    tree = ET.parse(report)
                    targets = {
                        "root": tree.getroot(),
                        "suite": tree.getroot().find("testsuite"),
                        "testcase": tree.getroot().find("testsuite/testcase"),
                    }
                    target = targets[location]
                    self.assertIsNotNone(target)
                    target.set(attribute, value)
                    tree.write(report, encoding="utf-8", xml_declaration=True)
                    with self.assertRaisesRegex(
                        swift_report.ReportError,
                        "xunit_report_untrusted_attribute",
                    ):
                        swift_report.verify(report, log, 2, expected_tests=expected)

        for attribute in ("notrun", "not-run", "tests", "failures", "errors", "skipped"):
            with self.subTest(forbidden_testcase_attribute=attribute):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                case = tree.getroot().find("testsuite/testcase")
                self.assertIsNotNone(case)
                case.set(attribute, "1")
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "xunit_report_untrusted_attribute",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        for location in ("root", "suite"):
            for attribute in ("tests", "failures", "errors", "skipped"):
                with self.subTest(
                    namespaced_aggregate_location=location,
                    namespaced_aggregate_attribute=attribute,
                ):
                    self._write_xunit(report, ["testOne", "testTwo"])
                    tree = ET.parse(report)
                    target = (
                        tree.getroot()
                        if location == "root"
                        else tree.getroot().find("testsuite")
                    )
                    self.assertIsNotNone(target)
                    target.set(f"{{urn:pam:adversarial}}{attribute}", "1")
                    tree.write(report, encoding="utf-8", xml_declaration=True)
                    with self.assertRaises(swift_report.ReportError):
                        swift_report.verify(report, log, 2, expected_tests=expected)

        for location in ("root", "suite", "testcase"):
            with self.subTest(namespaced_element=location):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                targets = {
                    "root": tree.getroot(),
                    "suite": tree.getroot().find("testsuite"),
                    "testcase": tree.getroot().find("testsuite/testcase"),
                }
                target = targets[location]
                self.assertIsNotNone(target)
                target.tag = f"{{urn:pam:adversarial}}{target.tag}"
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "xunit_report_invalid_structure",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        invalid_booleans = {
            "disabled": (" true", "swift_tests_not_executed"),
            "executed": ("false ", "swift_tests_not_executed"),
        }
        for location in ("root", "suite", "testcase"):
            for attribute, (value, error) in invalid_booleans.items():
                with self.subTest(location=location, attribute=attribute):
                    self._write_xunit(report, ["testOne", "testTwo"])
                    tree = ET.parse(report)
                    targets = {
                        "root": tree.getroot(),
                        "suite": tree.getroot().find("testsuite"),
                        "testcase": tree.getroot().find("testsuite/testcase"),
                    }
                    target = targets[location]
                    self.assertIsNotNone(target)
                    target.set(attribute, value)
                    tree.write(report, encoding="utf-8", xml_declaration=True)
                    with self.assertRaisesRegex(swift_report.ReportError, error):
                        swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        case = tree.getroot().find("testsuite/testcase")
        self.assertIsNotNone(case)
        case.set("disabled", "0")
        case.set("executed", "true")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=expected),
            2,
        )

        summary_only = "Executed 2 tests, with 0 failures in 0.1 seconds\n"
        log.write_text(summary_only, encoding="utf-8")
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "test_log_summary_mismatch",
        ):
            swift_report.verify(
                report,
                log,
                2,
                expected_tests=expected,
                require_exact_progress=True,
            )

        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        tree.getroot().find("testsuite").set("disabled", "1")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "swift_tests_not_executed",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        log.write_text(
            "Executed 2 tests, with 0 failures (0 unexpected) in 0.1 seconds\n"
            "1 test skipped\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_skipped"):
            swift_report.verify(report, log, 2, expected_tests=expected)

    def test_xctest_xunit_accepts_omitted_zero_skipped_fail_closed(self) -> None:
        report = self.root / "xctest.xml"
        log = self.root / "xctest.log"
        expected = [
            swift_report.ExpectedTest("xctest", "testOne"),
            swift_report.ExpectedTest("xctest", "testTwo"),
        ]
        log.write_text(
            "Executed 2 tests, with 0 failures (0 unexpected) in 0.1 seconds\n",
            encoding="utf-8",
        )

        # Match the SwiftPM 5.10/6.1 XCTest generator: the root carries no
        # aggregate fields and the suite omits a zero-valued `skipped`.
        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        root = tree.getroot()
        suite = root.find("testsuite")
        self.assertIsNotNone(suite)
        for name in ("tests", "failures", "errors", "skipped"):
            root.attrib.pop(name, None)
        suite.attrib.pop("skipped")
        suite.set("time", "0.1")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=expected),
            2,
        )

        # A complete root aggregate may still sum a child suite whose omitted
        # `skipped` value is the canonical zero.
        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        suite = tree.getroot().find("testsuite")
        self.assertIsNotNone(suite)
        suite.attrib.pop("skipped")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=expected),
            2,
        )

        # Omission never masks an actual skipped/not-run test or a skipped log.
        ET.SubElement(suite.find("testcase"), "skipped")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_aggregate_mismatch",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        suite = tree.getroot().find("testsuite")
        self.assertIsNotNone(suite)
        suite.attrib.pop("skipped")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        log.write_text(
            "Executed 2 tests, with 0 failures (0 unexpected) in 0.1 seconds\n"
            "1 test skipped\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(swift_report.ReportError, "swift_tests_skipped"):
            swift_report.verify(report, log, 2, expected_tests=expected)

        log.write_text(
            "Executed 2 tests, with 0 failures (0 unexpected) in 0.1 seconds\n",
            encoding="utf-8",
        )
        for invalid in ("", "-1", "00", "not-a-number"):
            with self.subTest(skipped=invalid):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                suite = tree.getroot().find("testsuite")
                self.assertIsNotNone(suite)
                suite.set("skipped", invalid)
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "xunit_report_invalid_aggregate",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        for required in ("tests", "failures", "errors"):
            with self.subTest(required=required):
                self._write_xunit(report, ["testOne", "testTwo"])
                tree = ET.parse(report)
                suite = tree.getroot().find("testsuite")
                self.assertIsNotNone(suite)
                suite.attrib.pop(required)
                tree.write(report, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "xunit_report_invalid_aggregate",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

        self._write_xunit(report, ["testOne", "testTwo"])
        tree = ET.parse(report)
        tree.getroot().attrib.pop("skipped")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_invalid_aggregate",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

    def test_xctest_parallel_progress_is_exact_fail_closed_evidence(self) -> None:
        report = self.root / "parallel-xctest.xml"
        log = self.root / "parallel-xctest.log"
        expected = [
            swift_report.ExpectedTest("xctest", "testOne"),
            swift_report.ExpectedTest("xctest", "testTwo"),
        ]
        self._write_xunit(report, ["testOne", "testTwo"])
        exact_progress = (
            "[1/2] Testing "
            "PAMInteractionModelTests.Phase2ContextModelsTests/testOne\n"
            "[2/2] Testing "
            "PAMInteractionModelTests.Phase2ContextModelsTests/testTwo\n"
        )
        log.write_text(exact_progress, encoding="utf-8")
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=expected),
            2,
        )

        invalid_logs = {
            "missing": exact_progress.splitlines(keepends=True)[0],
            "duplicate_index": exact_progress.replace("[2/2]", "[1/2]"),
            "wrong_total": exact_progress.replace("[2/2]", "[2/3]"),
            "duplicate_test": exact_progress.replace("testTwo", "testOne"),
            "unexpected_test": exact_progress.replace("testTwo", "testOther"),
            "zero_index": exact_progress.replace("[1/2]", "[0/2]"),
            "zero_total": exact_progress.replace("[1/2]", "[1/0]"),
            "malformed_index": exact_progress.replace("[1/2]", "[x/2]"),
            "missing_identifier": exact_progress.replace(
                "PAMInteractionModelTests.Phase2ContextModelsTests/testOne",
                "",
            ),
            "prefixed_extra_suite": (
                exact_progress
                + "prefix [3/3] Testing OtherModule.OtherSuite/testUnexpected\n"
            ),
            "ansi_extra_suite": (
                exact_progress
                + "\x1b[31m[3/3] Testing OtherModule.OtherSuite/testUnexpected\n"
            ),
            "unexpected_suite": (
                exact_progress
                + "[3/3] Testing OtherModule.OtherSuite/testUnexpected\n"
            ),
            "contradictory_summary": (
                exact_progress + "Executed 0 tests, with 0 failures\n"
            ),
            "valid_summary_with_extra_suite": (
                exact_progress
                + "[3/3] Testing OtherModule.OtherSuite/testUnexpected\n"
                + "Executed 2 tests, with 0 failures\n"
            ),
            "ansi_contradictory_summary": (
                exact_progress
                + "\x1b[31mExecuted 0 tests, with 0 failures\x1b[0m\n"
            ),
            "incomplete_summary": exact_progress + "Executed 0 tests\n",
            "missing_summary_comma": (
                exact_progress + "Executed 0 tests with 0 failures\n"
            ),
            "nonnumeric_summary_failure": (
                exact_progress + "Executed 0 tests, with x failures\n"
            ),
            "zero_width_summary": (
                exact_progress + "Execu\u200bted 0 tests, with 0 failures\n"
            ),
            "same_line_duplicate_summary": (
                exact_progress
                + "Executed 2 tests, with 0 failures; "
                "Executed 0 tests, with 0 failures\n"
            ),
            "unicode_line_separator_summary": (
                exact_progress
                + "Executed 2 tests, with 0 failures\u2028"
                "Executed 0 tests, with 0 failures\n"
            ),
            "nbsp_progress_separator": (
                exact_progress.replace("] Testing ", "]\u00a0Testing ")
                + "Executed 2 tests, with 0 failures\n"
            ),
            "nbsp_hidden_failure": (
                exact_progress
                + "\u00a0Testing failed: hidden semantic failure\n"
                + "\u00a0✘ hidden semantic failure\n"
            ),
        }
        for name, invalid_log in invalid_logs.items():
            with self.subTest(name=name):
                log.write_text(invalid_log, encoding="utf-8")
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "test_log_(?:summary_mismatch|control_character)",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

    def test_swift_testing_xunit_uses_exact_pinned_identity(self) -> None:
        report = self.root / "swift-testing.xml"
        log = self.root / "test.log"
        expected = [
            swift_report.ExpectedTest("swift-testing", "firstTest", "First display"),
            swift_report.ExpectedTest("swift-testing", "secondTest", "Second display"),
        ]
        self._write_xunit(
            report,
            ["firstTest", "secondTest"],
            kind="swift-testing",
        )
        log.write_text(
            "✔ Test run with 2 tests passed after 0.001 seconds.\n",
            encoding="utf-8",
        )
        self.assertEqual(
            swift_report.verify(report, log, 2, expected_tests=expected),
            2,
        )

        log.write_text(
            "✔ Test run with 0 tests passed after 0.001 seconds.\n"
            "✔ Test run with 2 tests passed after 0.001 seconds.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "test_log_summary_mismatch",
        ):
            swift_report.verify(report, log, 2, expected_tests=expected)

        for failure_log in (
            "✘ Test run with 1 test failed after 0.001 seconds.\n",
            "Testing failed: 1 test failed.\n",
        ):
            with self.subTest(failure_log=failure_log):
                log.write_text(failure_log, encoding="utf-8")
                with self.assertRaisesRegex(
                    swift_report.ReportError,
                    "swift_tests_failed|test_log_summary_mismatch",
                ):
                    swift_report.verify(report, log, 2, expected_tests=expected)

    def test_filtered_swift_testing_companion_must_be_strictly_empty(self) -> None:
        report = self.root / "xctest-swift-testing.xml"
        log = self.root / "xctest.log"
        self._write_xunit(report, [])
        log.write_text(
            "✔ Test run with 0 tests passed after 0.001 seconds.\n",
            encoding="utf-8",
        )
        self.assertEqual(
            swift_report.verify_empty_swift_testing_companion(report, log),
            0,
        )

        self._write_xunit(report, ["unexpectedSwiftTest"])
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "unexpected_swift_test_executed",
        ):
            swift_report.verify_empty_swift_testing_companion(report, log)

        self._write_xunit(report, [])
        for invalid_log in (
            "",
            "✔ Test run with 1 test passed after 0.001 seconds.\n",
            "✔ Test run with 0 tests passed after 0.001 seconds.\n"
            "✔ Test run with 0 tests passed after 0.001 seconds.\n",
            "✔ Test run with 0 tests passed after 0.001 seconds.\n"
            "✔ Test run with 1 test passed after 0.001 seconds.\n",
            "✔ Test run with 0 tests passed after 0.001 seconds.\n"
            "\x1b[31m✔ Test run with 1 test passed after 0.001 seconds.\x1b[0m\n",
            "✔ Test run with 0 tests passed after 0.001 seconds.\n"
            "✘ Test run with 1 test failed after 0.001 seconds.\n",
            "✔ Test run with 0 tests passed after 0.001 seconds.\n"
            "Testing failed: 1 test failed.\n",
            "✔ Test run with 0 tests passed after 0.001 seconds; "
            "✔ Test run with 1 test passed after 0.001 seconds.\n",
        ):
            with self.subTest(invalid_log=invalid_log):
                log.write_text(invalid_log, encoding="utf-8")
                with self.assertRaises(swift_report.ReportError):
                    swift_report.verify_empty_swift_testing_companion(report, log)

    def test_xunit_metadata_cannot_hide_test_or_failure_evidence(self) -> None:
        report = self.root / "hidden-metadata.xml"
        log = self.root / "xctest.log"
        log.write_text(
            "✔ Test run with 0 tests passed after 0.001 seconds.\n",
            encoding="utf-8",
        )
        for metadata_name in ("properties", "system-out", "system-err"):
            for hidden_name in ("testcase", "failure"):
                with self.subTest(metadata=metadata_name, hidden=hidden_name):
                    self._write_xunit(report, [])
                    tree = ET.parse(report)
                    suite = tree.getroot().find("testsuite")
                    self.assertIsNotNone(suite)
                    metadata = ET.SubElement(suite, metadata_name)
                    ET.SubElement(metadata, hidden_name, name="hiddenTest")
                    tree.write(
                        report,
                        encoding="utf-8",
                        xml_declaration=True,
                    )
                    with self.assertRaisesRegex(
                        swift_report.ReportError,
                        "xunit_report_invalid_structure",
                    ):
                        swift_report.verify_empty_swift_testing_companion(report, log)

        expected = [swift_report.ExpectedTest("xctest", "testOne")]
        self._write_xunit(report, ["testOne"])
        tree = ET.parse(report)
        case = tree.getroot().find("testsuite/testcase")
        self.assertIsNotNone(case)
        system_out = ET.SubElement(case, "system-out")
        ET.SubElement(system_out, "failure")
        tree.write(report, encoding="utf-8", xml_declaration=True)
        log.write_text(
            "Executed 1 test, with 0 failures in 0.001 seconds.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            swift_report.ReportError,
            "xunit_report_invalid_testcase",
        ):
            swift_report.verify(report, log, 1, expected_tests=expected)

    def test_approved_icon_does_not_allow_substituted_or_unrelated_images(self) -> None:
        rel = "assets/agi/AGI-app-icon.png"
        original = REPO_ROOT / rel
        self.assertFalse(privacy_scan._is_forbidden_runtime_file(rel, original))
        self.assertTrue(privacy_scan._is_forbidden_runtime_file("assets/agi/screenshot.png", original))
        candidate = self.root / rel
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"unapproved replacement")
        self.assertTrue(privacy_scan._is_forbidden_runtime_file(rel, candidate))
        candidate.unlink()
        candidate.symlink_to(original)
        self.assertTrue(privacy_scan._is_forbidden_runtime_file(rel, candidate))

    def test_privacy_scan_redacts_values_and_has_no_fixture_whitelist(self) -> None:
        token = "sk-" + ("q" * 24)
        private_header = "-----BEGIN " + "PRIVATE KEY-----"
        text = "credential=" + token + "\nkey=" + private_header + "\n"
        findings = privacy_scan._secret_findings("core/tests/fixture.py", text)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(token not in finding for finding in findings))
        self.assertTrue(all(private_header not in finding for finding in findings))

        disguised = (
            'secret_path.write_text("'
            + private_header
            + '\\nactual-material\\nsecret\\n")'
        )
        disguised_findings = privacy_scan._secret_findings(
            "core/tests/test_core.py",
            disguised,
        )
        self.assertTrue(disguised_findings)
        self.assertTrue(all(private_header not in row for row in disguised_findings))

    def test_filesystem_scan_rejects_env_runtime_build_state_and_binary_credentials(self) -> None:
        (self.root / ".env").write_text("PASSWORD=not-a-pattern\n", encoding="utf-8")
        for relative in (
            "data/activity.sqlite",
            "build/output.bin",
            "state/runtime.json",
            "credentials/signing.p12",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic\0fixture")
        findings, checked, source = privacy_scan.scan_root(self.root)
        self.assertEqual(source, "filesystem")
        self.assertEqual(checked, 5)
        encoded = "\n".join(findings)
        self.assertIn("forbidden environment file: .env", encoded)
        self.assertIn("data/activity.sqlite", encoded)
        self.assertIn("build/output.bin", encoded)
        self.assertIn("state/runtime.json", encoded)
        self.assertIn("credentials/signing.p12", encoded)
        self.assertNotIn("PASSWORD=", encoded)

    def test_git_scan_includes_ignored_env_build_database_and_state(self) -> None:
        initialized = subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        (self.root / ".gitignore").write_text(
            ".env\nbuild/\ndata/\nstate/\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text("PASSWORD=ignored\n", encoding="utf-8")
        for relative in (
            "build/output.bin",
            "data/activity.sqlite",
            "state/runtime.json",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ignored synthetic fixture")

        findings, checked, source = privacy_scan.scan_root(self.root)
        self.assertEqual(source, "git+filesystem")
        self.assertEqual(checked, 5)
        encoded = "\n".join(findings)
        self.assertIn("forbidden environment file: .env", encoded)
        self.assertIn("build/output.bin", encoded)
        self.assertIn("data/activity.sqlite", encoded)
        self.assertIn("state/runtime.json", encoded)
        self.assertNotIn("PASSWORD=", encoded)

    def test_ci_uses_pinned_actions_read_only_permissions_and_early_evidence(self) -> None:
        for relative in (
            "tools/run-swift-release-tests.sh",
            "tools/verify_swift_test_report.py",
            "tools/release_privacy_scan.py",
        ):
            script = REPO_ROOT / relative
            self.assertTrue(script.stat().st_mode & 0o111, f"not executable: {relative}")
            self.assertTrue(
                script.read_bytes().startswith(b"#!"),
                f"missing shebang: {relative}",
            )

        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        checkout = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
        setup_python = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
        upload = "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertGreaterEqual(workflow.count(checkout), 3)
        self.assertIn(setup_python, workflow)
        self.assertIn(upload, workflow)
        self.assertNotIn("actions/checkout@v", workflow)
        self.assertNotIn("actions/setup-python@v", workflow)
        self.assertNotIn("actions/upload-artifact@v", workflow)
        self.assertLess(
            workflow.index("Initialize persistent Swift evidence"),
            workflow.index("Record selected Swift toolchain"),
        )
        self.assertIn("pam-swift-release-tests/tests", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertNotIn("continue-on-error", workflow)

        gate = (REPO_ROOT / "tools/run-swift-release-tests.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("swift-release-test-baseline.json", gate)
        self.assertNotIn("MINIMUM_XCTESTS", gate)
        self.assertNotIn("MINIMUM_SWIFT_TESTING_TESTS", gate)
        self.assertIn("testing_probe_source=$'import Testing", gate)
        self.assertIn("probe_strategy=required_expected_set", gate)
        self.assertIn("validation=swiftpm_listing_and_exact_execution", gate)
        self.assertIn("swift_testing_policy=required", gate)
        self.assertIn("swift_testing_expected_set=available", gate)
        self.assertIn('SWIFT_TESTING_MODULE="available"', gate)
        self.assertIn('if [[ "${PAM_REQUIRE_SWIFT_TESTING:-0}" == "1" ]]; then', gate)
        self.assertIn("verify_swift_testing_evidence", gate)
        self.assertIn("full-swift-testing.xml", gate)
        self.assertIn('pipeline_status=("${PIPESTATUS[@]}")', gate)


if __name__ == "__main__":
    unittest.main()
