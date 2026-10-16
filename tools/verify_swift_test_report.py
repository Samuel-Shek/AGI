#!/usr/bin/env python3
"""Build and verify fail-closed Swift test discovery/execution evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ReportError(RuntimeError):
    pass


_TEST_MODULE = "PAMInteractionModelTests"
_XCTEST_SUITE = "Phase2ContextModelsTests"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_XCTEST_RE = re.compile(
    r"^\s*func\s+(test[A-Za-z0-9_]+)\s*\(",
    re.MULTILINE,
)
_SWIFT_TESTING_RE = re.compile(
    r'^\s*@Test(?:\(\s*"(?P<display>(?:\\.|[^"\\])*)"(?:\s*,[^)]*)?\s*\)'
    r"|\([^)]*\))?\s*$\n"
    r"(?:\s*@[A-Za-z_][^\n]*\n)*"
    r"\s*(?:(?:private|internal|public)\s+)?func\s+"
    r"(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_NO_TESTING_MODULE_RE = re.compile(
    r"^(?:<stdin>|-):\d+:\d+: error: no such module ['‘]Testing['’]$"
)
_XCTEST_SUMMARY_RE = re.compile(
    r"^[ \t]*Executed\s+(?P<count>\d+)\s+tests?,\s+with\s+"
    r"(?P<failures>\d+)\s+failures?\b",
    re.MULTILINE,
)
_XCTEST_SUMMARY_CANDIDATE_RE = re.compile(
    r"^.*\bExecuted\b.*\btests?\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_XCTEST_SUMMARY_TOKEN_RE = re.compile(r"\bExecuted\b", re.IGNORECASE)
_XCTEST_PROGRESS_RE = re.compile(
    r"^[ \t]*\[(?P<index>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)\]"
    r"[ \t]+Testing[ \t]+(?P<identifier>\S+)[ \t]*$",
    re.MULTILINE,
)
_XCTEST_PROGRESS_CANDIDATE_RE = re.compile(
    r"^.*\][ \t]+Testing(?:[ \t]+.*)?[ \t]*$",
    re.MULTILINE,
)
_SWIFT_TESTING_SUMMARY_RE = re.compile(
    r"^[ \t]*✔\s+Test run with\s+(?P<count>\d+)\s+tests?"
    r"(?:\s+in\s+\d+\s+suites?)?\s+passed\s+after\s+.+$",
    re.MULTILINE,
)
_SWIFT_TESTING_SUMMARY_CANDIDATE_RE = re.compile(
    r"^.*\bTest run with\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_SWIFT_TESTING_SUMMARY_TOKEN_RE = re.compile(
    r"\bTest run with\b",
    re.IGNORECASE,
)
_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_LOG_SKIP_PATTERNS = (
    re.compile(r"(\d+)\s+tests?\s+skipped", re.IGNORECASE),
    re.compile(r"skipped\s+(\d+)\s+tests?", re.IGNORECASE),
    re.compile(r"↷\s*(\d+)\s+tests?", re.IGNORECASE),
)
_LOG_FAILURE_PATTERNS = (
    re.compile(r"Testing failed:", re.IGNORECASE),
    re.compile(r"\b[1-9][0-9]*\s+tests?\s+failed\b", re.IGNORECASE),
    re.compile(r"✘"),
)
_TEST_LISTING_LINE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"(?:/[A-Za-z_][A-Za-z0-9_]*)?(?:\(\))?$"
)
_ROOT_XUNIT_ATTRIBUTES = frozenset(
    {
        "tests",
        "failures",
        "errors",
        "skipped",
        "time",
        "name",
        "disabled",
        "executed",
        "notrun",
        "not-run",
        "status",
        "result",
    }
)
_SUITE_XUNIT_ATTRIBUTES = frozenset(
    {
        "name",
        "tests",
        "failures",
        "errors",
        "skipped",
        "time",
        "disabled",
        "executed",
        "notrun",
        "not-run",
        "status",
        "result",
    }
)
_CASE_XUNIT_ATTRIBUTES = frozenset(
    {
        "name",
        "classname",
        "time",
        "file",
        "line",
        "disabled",
        "executed",
        "status",
        "result",
    }
)


def _has_unsupported_log_character(text: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in "\t\r\n")
        or 0x7F <= ord(character) <= 0x9F
        or character == "\ufffd"
        or unicodedata.category(character) in {"Cf", "Zl", "Zp"}
        or (
            unicodedata.category(character) == "Zs"
            and character != " "
        )
        for character in text
    )


@dataclass(frozen=True)
class ExpectedTest:
    kind: str
    identifier: str
    display_name: str = ""

    @property
    def listing_id(self) -> str:
        if self.kind == "xctest":
            return f"{_TEST_MODULE}.{_XCTEST_SUITE}/{self.identifier}"
        return f"{_TEST_MODULE}.{self.identifier}"

    @property
    def xunit_aliases(self) -> frozenset[str]:
        aliases = {
            self.identifier,
            f"{self.identifier}()",
        }
        if self.kind == "xctest":
            aliases.update(
                {
                    f"{_XCTEST_SUITE}.{self.identifier}",
                    f"{_XCTEST_SUITE}/{self.identifier}",
                    f"{_TEST_MODULE}.{_XCTEST_SUITE}/{self.identifier}",
                }
            )
        elif self.display_name:
            aliases.update({self.display_name, f"{self.display_name}()"})
        return frozenset(aliases)

    @property
    def xunit_identity(self) -> tuple[str, str]:
        if self.kind == "xctest":
            return (
                f"{_TEST_MODULE}.{_XCTEST_SUITE}",
                self.identifier,
            )
        return (_TEST_MODULE, f"{self.identifier}()")


def _decode_swift_display_name(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        decoded = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return ""
    return decoded if isinstance(decoded, str) else ""


def _validate_expected_tests(tests: Iterable[ExpectedTest]) -> list[ExpectedTest]:
    rows = list(tests)
    if not rows:
        raise ReportError("expected_tests_empty")
    identifiers: set[str] = set()
    aliases: dict[str, ExpectedTest] = {}
    for test in rows:
        if (
            test.kind not in {"xctest", "swift-testing"}
            or not _IDENTIFIER_RE.fullmatch(test.identifier)
            or "\n" in test.display_name
            or "\r" in test.display_name
            or (test.kind == "swift-testing" and not test.display_name)
            or (test.kind == "xctest" and bool(test.display_name))
        ):
            raise ReportError("expected_tests_invalid")
        if test.identifier in identifiers:
            raise ReportError("expected_tests_duplicate")
        identifiers.add(test.identifier)
        for alias in test.xunit_aliases:
            previous = aliases.get(alias)
            if previous is not None and previous != test:
                raise ReportError("expected_test_alias_ambiguous")
            aliases[alias] = test
    return sorted(rows, key=lambda test: (test.kind, test.identifier))


def extract_expected_tests(
    xctest_source: Path,
    swift_testing_source: Path,
) -> list[ExpectedTest]:
    try:
        xctest_text = xctest_source.read_text(encoding="utf-8")
        swift_testing_text = swift_testing_source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("swift_test_source_unreadable") from exc
    tests = [
        ExpectedTest("xctest", identifier)
        for identifier in _XCTEST_RE.findall(xctest_text)
    ]
    tests.extend(
        ExpectedTest(
            "swift-testing",
            match.group("identifier"),
            _decode_swift_display_name(match.group("display")),
        )
        for match in _SWIFT_TESTING_RE.finditer(swift_testing_text)
    )
    return _validate_expected_tests(tests)


def load_baseline_tests(path: Path) -> list[ExpectedTest]:
    if not path.is_file():
        raise ReportError("swift_test_baseline_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError("swift_test_baseline_invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "xctest", "swift_testing"}
        or payload.get("version") != 1
        or not isinstance(payload.get("xctest"), list)
        or not isinstance(payload.get("swift_testing"), list)
    ):
        raise ReportError("swift_test_baseline_invalid")
    tests: list[ExpectedTest] = []
    for identifier in payload["xctest"]:
        if not isinstance(identifier, str):
            raise ReportError("swift_test_baseline_invalid")
        tests.append(ExpectedTest("xctest", identifier))
    for row in payload["swift_testing"]:
        if not isinstance(row, dict) or set(row) != {"identifier", "display_name"}:
            raise ReportError("swift_test_baseline_invalid")
        identifier = row.get("identifier")
        display_name = row.get("display_name")
        if not isinstance(identifier, str) or not isinstance(display_name, str):
            raise ReportError("swift_test_baseline_invalid")
        tests.append(ExpectedTest("swift-testing", identifier, display_name))
    return _validate_expected_tests(tests)


def validate_source_against_baseline(
    declared_tests: Iterable[ExpectedTest],
    baseline_tests: Iterable[ExpectedTest],
) -> list[ExpectedTest]:
    declared = _validate_expected_tests(declared_tests)
    baseline = _validate_expected_tests(baseline_tests)
    if declared != baseline:
        raise ReportError("swift_test_source_baseline_mismatch")
    return declared


def write_expected_tests(
    path: Path,
    tests: Iterable[ExpectedTest],
    *,
    declared_tests: Iterable[ExpectedTest],
    swift_testing_module: str,
) -> None:
    rows = _validate_expected_tests(tests)
    declared_rows = _validate_expected_tests(declared_tests)
    if swift_testing_module not in {"available", "unavailable"}:
        raise ReportError("swift_testing_module_probe_invalid")
    if swift_testing_module == "unavailable" and any(
        test.kind == "swift-testing" for test in rows
    ):
        raise ReportError("expected_tests_module_mismatch")
    active_counts = {
        kind: sum(test.kind == kind for test in rows)
        for kind in ("xctest", "swift-testing")
    }
    declared_counts = {
        kind: sum(test.kind == kind for test in declared_rows)
        for kind in ("xctest", "swift-testing")
    }
    payload = {
        "version": 1,
        "swift_testing_module": swift_testing_module,
        "declared_counts": declared_counts,
        "active_counts": active_counts,
        "tests": [
            {
                "kind": test.kind,
                "identifier": test.identifier,
                "display_name": test.display_name or None,
            }
            for test in rows
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_expected_tests(path: Path) -> list[ExpectedTest]:
    if not path.is_file():
        raise ReportError("expected_tests_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError("expected_tests_invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "version",
            "swift_testing_module",
            "declared_counts",
            "active_counts",
            "tests",
        }
        or payload.get("version") != 1
        or payload.get("swift_testing_module") not in {"available", "unavailable"}
        or not isinstance(payload.get("tests"), list)
    ):
        raise ReportError("expected_tests_invalid")
    tests: list[ExpectedTest] = []
    for row in payload["tests"]:
        if not isinstance(row, dict) or set(row) != {
            "kind",
            "identifier",
            "display_name",
        }:
            raise ReportError("expected_tests_invalid")
        kind = row.get("kind")
        identifier = row.get("identifier")
        display_name = row.get("display_name") or ""
        if not all(isinstance(value, str) for value in (kind, identifier, display_name)):
            raise ReportError("expected_tests_invalid")
        tests.append(ExpectedTest(kind, identifier, display_name))
    rows = _validate_expected_tests(tests)
    active_counts = {
        kind: sum(test.kind == kind for test in rows)
        for kind in ("xctest", "swift-testing")
    }
    if payload["active_counts"] != active_counts:
        raise ReportError("expected_tests_count_mismatch")
    declared_counts = payload["declared_counts"]
    if (
        not isinstance(declared_counts, dict)
        or set(declared_counts) != {"xctest", "swift-testing"}
        or any(not isinstance(value, int) or value < 0 for value in declared_counts.values())
        or declared_counts["xctest"] != active_counts["xctest"]
        or declared_counts["swift-testing"] < active_counts["swift-testing"]
    ):
        raise ReportError("expected_tests_count_mismatch")
    if payload["swift_testing_module"] == "available":
        if declared_counts != active_counts:
            raise ReportError("expected_tests_module_mismatch")
    elif active_counts["swift-testing"] != 0:
        raise ReportError("expected_tests_module_mismatch")
    return rows


def classify_testing_probe(output: Path, exit_code: int) -> str:
    if exit_code < 0 or exit_code > 255 or not output.is_file():
        raise ReportError("swift_testing_module_probe_invalid")
    text = output.read_text(encoding="utf-8", errors="replace")
    if exit_code == 0:
        return "available"
    error_lines = [line.strip() for line in text.splitlines() if "error:" in line]
    if len(error_lines) == 1 and _NO_TESTING_MODULE_RE.fullmatch(error_lines[0]):
        return "unavailable"
    raise ReportError("swift_testing_module_probe_failed")


def verify_listing(list_output: Path, expected_tests: Iterable[ExpectedTest]) -> int:
    if not list_output.is_file():
        raise ReportError("swift_test_list_missing")
    text = list_output.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ReportError("swift_test_list_empty")
    if _has_unsupported_log_character(text):
        raise ReportError("swift_test_list_control_character")
    expected = _validate_expected_tests(expected_tests)
    expected_ids = {test.listing_id: test for test in expected}
    observed: dict[ExpectedTest, int] = {test: 0 for test in expected}
    for raw_line in text.splitlines():
        candidate = raw_line.strip(" \t\r")
        if not _TEST_LISTING_LINE_RE.fullmatch(candidate):
            continue
        if raw_line != candidate:
            raise ReportError("swift_test_list_noncanonical")
        normalized = candidate[:-2] if candidate.endswith("()") else candidate
        test = expected_ids.get(normalized)
        if test is None:
            raise ReportError("unexpected_swift_test_discovered")
        observed[test] += 1
    if any(count == 0 for count in observed.values()):
        raise ReportError("expected_swift_test_not_discovered")
    if any(count != 1 for count in observed.values()):
        raise ReportError("expected_swift_test_discovery_duplicate")
    return len(expected)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _validate_plain_element(
    node: ET.Element,
    expected_tag: str,
    allowed_attributes: frozenset[str],
) -> None:
    # The pinned Xcode 15.4/16.4 xUnit artifacts use unqualified lowercase
    # structural tags and the exact attribute sets above.  A case variant or
    # namespace lookalike must not turn a failure marker into ignored metadata.
    if node.tag != expected_tag:
        raise ReportError("xunit_report_invalid_structure")
    if any(attribute not in allowed_attributes for attribute in node.attrib):
        raise ReportError("xunit_report_untrusted_attribute")


def _strict_unqualified_attribute(
    node: ET.Element,
    name: str,
    *,
    error: str = "xunit_report_untrusted_namespaced_attribute",
) -> str | None:
    matches = [
        (attribute, value)
        for attribute, value in node.attrib.items()
        if _local_name(attribute) == name
    ]
    if not matches:
        return None
    if len(matches) != 1 or matches[0][0] != name:
        raise ReportError(error)
    return matches[0][1]


def _required_nonnegative_int(node: ET.Element, name: str) -> int:
    raw = _strict_unqualified_attribute(node, name)
    if raw is None or not _INTEGER_RE.fullmatch(raw):
        raise ReportError("xunit_report_invalid_aggregate")
    return int(raw)


def _optional_nonnegative_int(node: ET.Element, name: str) -> int:
    raw = _strict_unqualified_attribute(node, name)
    if raw is None:
        return 0
    if not _INTEGER_RE.fullmatch(raw):
        raise ReportError("xunit_report_invalid_aggregate")
    return int(raw)


def _match_case(case: ET.Element, expected: list[ExpectedTest]) -> ExpectedTest:
    name = _strict_unqualified_attribute(case, "name")
    classname = _strict_unqualified_attribute(case, "classname")
    if not name or not classname:
        raise ReportError("xunit_report_invalid_testcase")
    matches = [
        test
        for test in expected
        if (classname, name) == test.xunit_identity
    ]
    if not matches:
        raise ReportError("unexpected_swift_test_executed")
    if len(matches) != 1:
        raise ReportError("xunit_test_name_ambiguous")
    return matches[0]


def _validate_execution_attributes(element: ET.Element) -> None:
    """Reject execution-state attributes not emitted by the pinned runners.

    The Xcode 15.4 and 16.4 artifacts used for this release do not emit
    ``status`` or ``result`` on testsuites, testsuite, or testcase elements.
    Accepting arbitrary values there would let a report explicitly describe a
    failed test while keeping the aggregate counters at zero.  Treat any such
    attribute as untrusted evidence.  Boolean execution markers are accepted
    only in their canonical successful forms and without whitespace folding.
    """

    for name in ("status", "result"):
        raw = _strict_unqualified_attribute(
            element,
            name,
            error="xunit_report_untrusted_status_attribute",
        )
        if raw is None:
            continue
        if raw != raw.strip():
            raise ReportError("xunit_report_untrusted_status_attribute")
        normalized = raw.lower()
        if normalized in {"notrun", "not-run", "disabled", "skipped"}:
            raise ReportError("swift_tests_not_executed")
        if normalized in {"failed", "failure", "error"}:
            raise ReportError("swift_tests_failed")
        raise ReportError("xunit_report_untrusted_status_attribute")

    disabled = _strict_unqualified_attribute(
        element,
        "disabled",
        error="xunit_report_untrusted_status_attribute",
    )
    if disabled is not None and disabled not in {"0", "false"}:
        raise ReportError("swift_tests_not_executed")
    executed = _strict_unqualified_attribute(
        element,
        "executed",
        error="xunit_report_untrusted_status_attribute",
    )
    if executed is not None and executed not in {"1", "true"}:
        raise ReportError("swift_tests_not_executed")


def _suite_cases(suite: ET.Element) -> list[ET.Element]:
    _validate_plain_element(suite, "testsuite", _SUITE_XUNIT_ATTRIBUTES)
    _validate_execution_attributes(suite)
    allowed_metadata = {"properties", "system-out", "system-err"}
    cases: list[ET.Element] = []
    for child in list(suite):
        name = child.tag
        if name == "testcase":
            _validate_plain_element(child, "testcase", _CASE_XUNIT_ATTRIBUTES)
            cases.append(child)
        elif name not in allowed_metadata:
            raise ReportError("xunit_report_invalid_structure")
        elif name == "properties":
            if any(
                metadata.tag != "property" or bool(list(metadata))
                for metadata in list(child)
            ):
                raise ReportError("xunit_report_invalid_structure")
        elif list(child):
            raise ReportError("xunit_report_invalid_structure")
    tests = _required_nonnegative_int(suite, "tests")
    failures = _required_nonnegative_int(suite, "failures")
    errors = _required_nonnegative_int(suite, "errors")
    # SwiftPM's XCTest xUnit generator omits the zero-valued `skipped`
    # attribute.  Treat only that omission as zero; the other aggregate
    # fields remain mandatory and every explicit value remains strict.
    skipped = _optional_nonnegative_int(suite, "skipped")
    for name in ("notrun", "not-run"):
        raw = _strict_unqualified_attribute(
            suite,
            name,
            error="xunit_report_untrusted_status_attribute",
        )
        if raw is not None and (
            not _INTEGER_RE.fullmatch(raw) or int(raw) != 0
        ):
            raise ReportError("swift_tests_not_executed")
    actual_failures = 0
    actual_errors = 0
    actual_skipped = 0
    for case in cases:
        _validate_execution_attributes(case)
        child_names = [child.tag for child in list(case)]
        if any(name not in {"failure", "error", "skipped", "system-out", "system-err"} for name in child_names):
            raise ReportError("xunit_report_invalid_testcase")
        if any(list(child) for child in list(case)):
            raise ReportError("xunit_report_invalid_testcase")
        actual_failures += child_names.count("failure")
        actual_errors += child_names.count("error")
        actual_skipped += child_names.count("skipped")
    if (
        tests != len(cases)
        or failures != actual_failures
        or errors != actual_errors
        or skipped != actual_skipped
    ):
        raise ReportError("xunit_report_aggregate_mismatch")
    if failures or errors:
        raise ReportError("swift_tests_failed")
    if skipped:
        raise ReportError("swift_tests_skipped")
    return cases


def _parse_xunit(report: Path) -> tuple[ET.Element, list[ET.Element], list[ET.Element]]:
    if not report.is_file() or report.stat().st_size == 0:
        raise ReportError("xunit_report_missing")
    try:
        root = ET.parse(report).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ReportError("xunit_report_invalid") from exc
    if root.tag != "testsuites":
        raise ReportError("xunit_report_invalid_structure")
    _validate_plain_element(root, "testsuites", _ROOT_XUNIT_ATTRIBUTES)
    suites = list(root)
    # Both pinned Xcode 15.4 and 16.4 reports contain exactly one direct
    # testsuite.  An empty or split second suite is still extra execution
    # evidence and must not disappear when testcase sets are flattened.
    if len(suites) != 1 or suites[0].tag != "testsuite":
        raise ReportError("xunit_report_invalid_structure")
    _validate_execution_attributes(root)
    for name in ("notrun", "not-run"):
        raw = _strict_unqualified_attribute(
            root,
            name,
            error="xunit_report_untrusted_status_attribute",
        )
        if raw is not None and (
            not _INTEGER_RE.fullmatch(raw) or int(raw) != 0
        ):
            raise ReportError("swift_tests_not_executed")
    cases = [case for suite in suites for case in _suite_cases(suite)]
    aggregate_names = {"tests", "failures", "errors", "skipped"}
    present = {name for name in aggregate_names if root.get(name) is not None}
    if present and present != aggregate_names:
        raise ReportError("xunit_report_invalid_aggregate")
    if present:
        totals = {
            name: sum(
                _optional_nonnegative_int(suite, name)
                if name == "skipped"
                else _required_nonnegative_int(suite, name)
                for suite in suites
            )
            for name in aggregate_names
        }
        if any(_required_nonnegative_int(root, name) != totals[name] for name in aggregate_names):
            raise ReportError("xunit_report_aggregate_mismatch")
    return root, suites, cases


def _verify_log(
    log: Path,
    expected: list[ExpectedTest],
    *,
    require_exact_progress: bool = False,
) -> None:
    if not log.is_file():
        raise ReportError("test_log_missing")
    text = log.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ReportError("test_log_empty")
    if _has_unsupported_log_character(text):
        raise ReportError("test_log_control_character")
    if any(
        int(value) > 0
        for pattern in _LOG_SKIP_PATTERNS
        for value in pattern.findall(text)
    ):
        raise ReportError("swift_tests_skipped")
    if any(pattern.search(text) for pattern in _LOG_FAILURE_PATTERNS):
        raise ReportError("swift_tests_failed")
    kinds = {test.kind for test in expected}
    if len(kinds) != 1:
        raise ReportError("expected_test_kinds_mixed")
    count = len(expected)
    if kinds == {"xctest"}:
        summaries = []
        for candidate in _XCTEST_SUMMARY_CANDIDATE_RE.finditer(text):
            candidate_text = candidate.group(0)
            match = _XCTEST_SUMMARY_RE.search(candidate_text)
            if (
                match is None
                or len(_XCTEST_SUMMARY_TOKEN_RE.findall(candidate_text)) != 1
            ):
                raise ReportError("test_log_summary_mismatch")
            summaries.append(
                (int(match.group("count")), int(match.group("failures")))
            )
        progress = []
        for candidate in _XCTEST_PROGRESS_CANDIDATE_RE.finditer(text):
            match = _XCTEST_PROGRESS_RE.fullmatch(candidate.group(0))
            if match is None:
                raise ReportError("test_log_summary_mismatch")
            progress.append(
                (
                    int(match.group("index")),
                    int(match.group("total")),
                    match.group("identifier"),
                )
            )
        if summaries and any(summary != (count, 0) for summary in summaries):
            raise ReportError("test_log_summary_mismatch")
        if progress:
            expected_ids = {test.listing_id for test in expected}
            if (
                len(progress) != count
                or {index for index, _, _ in progress} != set(range(1, count + 1))
                or any(total != count for _, total, _ in progress)
                or {identifier for _, _, identifier in progress} != expected_ids
                or len({identifier for _, _, identifier in progress}) != count
            ):
                raise ReportError("test_log_summary_mismatch")
        elif require_exact_progress or not summaries:
            raise ReportError("test_log_summary_mismatch")
    else:
        summaries = []
        for candidate in _SWIFT_TESTING_SUMMARY_CANDIDATE_RE.finditer(text):
            candidate_text = candidate.group(0)
            match = _SWIFT_TESTING_SUMMARY_RE.search(candidate_text)
            if (
                match is None
                or len(_SWIFT_TESTING_SUMMARY_TOKEN_RE.findall(candidate_text)) != 1
            ):
                raise ReportError("test_log_summary_mismatch")
            summaries.append(int(match.group("count")))
        if not summaries or any(summary != count for summary in summaries):
            raise ReportError("test_log_summary_mismatch")


def verify_empty_swift_testing_companion(report: Path, log: Path) -> int:
    """Prove a filtered XCTest run did not also execute Swift Testing tests."""
    _, _, cases = _parse_xunit(report)
    if cases:
        raise ReportError("unexpected_swift_test_executed")
    if not log.is_file():
        raise ReportError("test_log_missing")
    text = log.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ReportError("test_log_empty")
    if _has_unsupported_log_character(text):
        raise ReportError("test_log_control_character")
    if any(
        int(value) > 0
        for pattern in _LOG_SKIP_PATTERNS
        for value in pattern.findall(text)
    ):
        raise ReportError("swift_tests_skipped")
    if any(pattern.search(text) for pattern in _LOG_FAILURE_PATTERNS):
        raise ReportError("swift_tests_failed")
    summaries = []
    for candidate in _SWIFT_TESTING_SUMMARY_CANDIDATE_RE.finditer(text):
        candidate_text = candidate.group(0)
        match = _SWIFT_TESTING_SUMMARY_RE.search(candidate_text)
        if (
            match is None
            or len(_SWIFT_TESTING_SUMMARY_TOKEN_RE.findall(candidate_text)) != 1
        ):
            raise ReportError("test_log_summary_mismatch")
        summaries.append(int(match.group("count")))
    if summaries != [0]:
        raise ReportError("test_log_summary_mismatch")
    return 0


def verify(
    report: Path,
    log: Path,
    minimum: int,
    expected_names: set[str] | None = None,
    *,
    expected_tests: Iterable[ExpectedTest] | None = None,
    require_exact_progress: bool = False,
) -> int:
    if expected_names is not None and expected_tests is not None:
        raise ReportError("expected_test_inputs_conflict")
    expected = list(expected_tests or ())
    if expected_names is not None:
        expected = [ExpectedTest("xctest", name) for name in sorted(expected_names)]
    expected = _validate_expected_tests(expected)
    if minimum != len(expected):
        raise ReportError("expected_test_count_not_exact")
    _, _, cases = _parse_xunit(report)
    if len(cases) != len(expected):
        raise ReportError("swift_test_count_not_exact")
    observed: dict[ExpectedTest, int] = {test: 0 for test in expected}
    for case in cases:
        observed[_match_case(case, expected)] += 1
    if any(count != 1 for count in observed.values()):
        raise ReportError("expected_swift_test_duplicate")
    _verify_log(
        log,
        expected,
        require_exact_progress=require_exact_progress,
    )
    return len(cases)


def _filtered_expected_tests(path: Path, expected_kind: str | None) -> list[ExpectedTest]:
    tests = load_expected_tests(path)
    if expected_kind:
        tests = [test for test in tests if test.kind == expected_kind]
    return _validate_expected_tests(tests)


_CLI_OPTIONS = frozenset(
    {
        "--write-expected-tests",
        "--xctest-source",
        "--swift-testing-source",
        "--baseline",
        "--swift-testing-module",
        "--probe-output",
        "--probe-exit-code",
        "--list-output",
        "--report",
        "--log",
        "--minimum",
        "--expected-names",
        "--expected-tests",
        "--expected-kind",
        "--require-empty-swift-testing-companion",
        "--require-exact-progress",
    }
)


def _provided_cli_options(argv: list[str]) -> frozenset[str]:
    provided: set[str] = set()
    for token in argv:
        if not token.startswith("--"):
            continue
        option = token.split("=", 1)[0]
        if option not in _CLI_OPTIONS:
            # argparse owns unknown-option diagnostics.  Known options are
            # counted here so repeated --option and --option=value forms
            # cannot silently use the last value.
            continue
        if option in provided:
            raise ReportError("duplicate_argument")
        provided.add(option)
    return frozenset(provided)


def _cli_mode(provided: frozenset[str], args: argparse.Namespace) -> str:
    manifest = frozenset(
        {
            "--write-expected-tests",
            "--xctest-source",
            "--swift-testing-source",
            "--baseline",
            "--swift-testing-module",
        }
    )
    probe = frozenset({"--probe-output", "--probe-exit-code"})
    companion = frozenset(
        {"--require-empty-swift-testing-companion", "--report", "--log"}
    )
    allowed: dict[frozenset[str], str] = {
        manifest: "manifest",
        probe: "probe",
        companion: "empty-companion",
        frozenset({"--list-output", "--expected-tests"}): "listing",
        frozenset(
            {"--list-output", "--expected-tests", "--expected-kind"}
        ): "listing",
        frozenset({"--list-output", "--expected-names"}): "listing",
        frozenset(
            {"--report", "--log", "--minimum", "--expected-tests"}
        ): "report",
        frozenset(
            {
                "--report",
                "--log",
                "--minimum",
                "--expected-tests",
                "--expected-kind",
            }
        ): "report",
        frozenset(
            {
                "--report",
                "--log",
                "--minimum",
                "--expected-tests",
                "--require-exact-progress",
            }
        ): "report",
        frozenset(
            {
                "--report",
                "--log",
                "--minimum",
                "--expected-tests",
                "--expected-kind",
                "--require-exact-progress",
            }
        ): "report",
        frozenset(
            {"--report", "--log", "--minimum", "--expected-names"}
        ): "report",
        frozenset(
            {
                "--report",
                "--log",
                "--minimum",
                "--expected-names",
                "--require-exact-progress",
            }
        ): "report",
    }
    mode = allowed.get(provided)
    if mode is None:
        raise ReportError("cli_mode_arguments_invalid")
    if (
        args.require_exact_progress
        and args.expected_kind == "swift-testing"
    ):
        raise ReportError("exact_progress_only_valid_for_xctest")
    return mode


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--write-expected-tests", type=Path)
    parser.add_argument("--xctest-source", type=Path)
    parser.add_argument("--swift-testing-source", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--swift-testing-module",
        choices=("available", "unavailable"),
    )
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--probe-exit-code", type=int)
    parser.add_argument("--list-output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--minimum", type=int)
    parser.add_argument("--expected-names", type=Path)
    parser.add_argument("--expected-tests", type=Path)
    parser.add_argument("--expected-kind", choices=("xctest", "swift-testing"))
    parser.add_argument(
        "--require-empty-swift-testing-companion",
        action="store_true",
    )
    parser.add_argument(
        "--require-exact-progress",
        action="store_true",
    )
    raw_argv = sys.argv[1:]
    help_tokens = [token for token in raw_argv if token in {"-h", "--help"}]
    if help_tokens:
        if len(raw_argv) != 1:
            parser.error("help cannot be combined with release evidence arguments")
        parser.print_help()
        return 0
    args = parser.parse_args(raw_argv)
    try:
        mode = _cli_mode(_provided_cli_options(raw_argv), args)
        if mode == "empty-companion":
            print(verify_empty_swift_testing_companion(args.report, args.log))
            return 0
        if mode == "probe":
            print(classify_testing_probe(args.probe_output, args.probe_exit_code))
            return 0
        if mode == "manifest":
            baseline = load_baseline_tests(args.baseline)
            declared = validate_source_against_baseline(
                extract_expected_tests(args.xctest_source, args.swift_testing_source),
                baseline,
            )
            active = [
                test
                for test in declared
                if test.kind == "xctest" or args.swift_testing_module == "available"
            ]
            write_expected_tests(
                args.write_expected_tests,
                active,
                declared_tests=declared,
                swift_testing_module=args.swift_testing_module,
            )
            counts = {
                kind: sum(test.kind == kind for test in active)
                for kind in ("xctest", "swift-testing")
            }
            print(
                json.dumps(
                    {
                        "total": len(active),
                        "swift_testing_module": args.swift_testing_module,
                        **counts,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        if args.expected_tests is not None:
            expected = _filtered_expected_tests(args.expected_tests, args.expected_kind)
        elif args.expected_names is not None:
            if not args.expected_names.is_file():
                raise ReportError("expected_test_names_missing")
            try:
                expected_names_text = args.expected_names.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ReportError("expected_test_names_invalid") from exc
            if _has_unsupported_log_character(expected_names_text):
                raise ReportError("expected_test_names_invalid")
            names = []
            for row in expected_names_text.splitlines():
                if not row:
                    continue
                if row != row.strip(" \t"):
                    raise ReportError("expected_test_names_invalid")
                names.append(row)
            expected = _validate_expected_tests(
                ExpectedTest("xctest", name) for name in names
            )
        else:
            raise ReportError("expected_tests_missing")
        if mode == "listing":
            print(verify_listing(args.list_output, expected))
            return 0
        print(
            verify(
                args.report,
                args.log,
                args.minimum,
                expected_tests=expected,
                require_exact_progress=args.require_exact_progress,
            )
        )
    except ReportError as exc:
        parser.exit(2, f"Swift release test evidence rejected: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
