#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/apps/PersonalAIMonitorMenuBar"
XCTEST_SOURCE="$PACKAGE_DIR/Tests/PAMInteractionModelTests/Phase2ContextModelsTests.swift"
SWIFT_TESTING_SOURCE="$PACKAGE_DIR/Tests/PAMInteractionModelTests/Phase2ContextModelsSwiftTestingTests.swift"
BASELINE_TESTS="$ROOT_DIR/tools/swift-release-test-baseline.json"
PERSIST_EVIDENCE=0
SWIFT_ARGS=(--build-system native --package-path "$PACKAGE_DIR")
if [[ -n "${PAM_SWIFT_TEST_ISOLATED_ROOT:-}" ]]; then
  case "$PAM_SWIFT_TEST_ISOLATED_ROOT" in
    /*) ;;
    *) echo "Refusing Swift release acceptance: isolated root must be absolute." >&2; exit 2 ;;
  esac
  if [[ ! -d "$PAM_SWIFT_TEST_ISOLATED_ROOT" || -L "$PAM_SWIFT_TEST_ISOLATED_ROOT" \
     || -z "${PAM_SWIFT_TEST_SDK:-}" || "$PAM_SWIFT_TEST_SDK" != /* ]]; then
    echo "Refusing Swift release acceptance: isolated root or fixed SDK is invalid." >&2
    exit 2
  fi
  SWIFT_ARGS+=(--disable-sandbox --sdk "$PAM_SWIFT_TEST_SDK"
    --scratch-path "$PAM_SWIFT_TEST_ISOLATED_ROOT/swift-scratch"
    --cache-path "$PAM_SWIFT_TEST_ISOLATED_ROOT/cache"
    --config-path "$PAM_SWIFT_TEST_ISOLATED_ROOT/config"
    --security-path "$PAM_SWIFT_TEST_ISOLATED_ROOT/security"
    --enable-xctest)
fi
SWIFT_FULL_ARGS=("${SWIFT_ARGS[@]}")
if [[ -n "${PAM_SWIFT_TEST_ISOLATED_ROOT:-}" ]]; then
  SWIFT_FULL_ARGS+=(--enable-swift-testing)
fi

if [[ -n "${PAM_SWIFT_TEST_EVIDENCE_DIR:-}" ]]; then
  if [[ "$PAM_SWIFT_TEST_EVIDENCE_DIR" != /* ]]; then
    echo "Refusing Swift release acceptance: PAM_SWIFT_TEST_EVIDENCE_DIR must be absolute." >&2
    exit 2
  fi
  mkdir -p "$PAM_SWIFT_TEST_EVIDENCE_DIR"
  RESULT_DIR="$(cd "$PAM_SWIFT_TEST_EVIDENCE_DIR" && pwd -P)"
  if find "$RESULT_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "Refusing Swift release acceptance: persistent evidence directory is not empty: $RESULT_DIR" >&2
    exit 2
  fi
  PERSIST_EVIDENCE=1
else
  RESULT_DIR="$(mktemp -d -t pam-swift-release-tests.XXXXXX)"
fi

LIST_PRIMARY_OUTPUT="$RESULT_DIR/list-primary.log"
LIST_FALLBACK_OUTPUT="$RESULT_DIR/list-fallback.log"
LIST_OUTPUT="$RESULT_DIR/list.log"
RUN_OUTPUT="$RESULT_DIR/xctest.log"
RUN_STATUS="$RESULT_DIR/xctest-status.json"
RUN_XUNIT="$RESULT_DIR/xctest.xml"
RUN_SWIFT_TESTING_XUNIT="$RESULT_DIR/xctest-swift-testing.xml"
FULL_OUTPUT="$RESULT_DIR/full.log"
FULL_STATUS="$RESULT_DIR/full-status.json"
FULL_XCTEST_XUNIT="$RESULT_DIR/full.xml"
FULL_SWIFT_TESTING_XUNIT="$RESULT_DIR/full-swift-testing.xml"
EXPECTED_TESTS="$RESULT_DIR/expected-tests.json"
EXTRACTION_OUTPUT="$RESULT_DIR/expected-tests-summary.json"
TESTING_PROBE_OUTPUT="$RESULT_DIR/swift-testing-probe.log"
SUMMARY_OUTPUT="$RESULT_DIR/summary.json"
GATE_STATUS_OUTPUT="$RESULT_DIR/gate-status.json"
GATE_RESULT="failed"

cleanup() {
  local exit_code=$?
  printf '{"contract_version":"PAMSwiftReleaseGateStatusV1","exit_code":%s,"status":"%s"}\n' \
    "$exit_code" "$GATE_RESULT" >"$GATE_STATUS_OUTPUT"
  if [[ "$PERSIST_EVIDENCE" == "0" ]]; then
    rm -rf "$RESULT_DIR"
  fi
  return "$exit_code"
}
trap cleanup EXIT

if [[ "${PAM_REQUIRE_SWIFT_TESTING:-0}" != "0" \
  && "${PAM_REQUIRE_SWIFT_TESTING:-0}" != "1" ]]; then
  echo "Refusing Swift release acceptance: PAM_REQUIRE_SWIFT_TESTING must be 0 or 1." >&2
  exit 2
fi

if ! xcrun --find xctest >/dev/null 2>&1; then
  echo "Refusing Swift release acceptance: xctest is unavailable." >&2
  exit 2
fi

testing_probe_source=$'import Testing\n'
if [[ "${PAM_REQUIRE_SWIFT_TESTING:-0}" == "1" ]]; then
  # A standalone swiftc invocation does not inherit SwiftPM's package test
  # search paths on Xcode 16.4 and can falsely report that Testing is absent.
  # Treat it as a required expected set here; the exact SwiftPM listing,
  # execution logs and disjoint xUnit reports below must still prove all 19
  # versioned Swift Testing cases or the gate fails closed.
  SWIFT_TESTING_MODULE="available"
  {
    printf 'swift_testing_policy=required\n'
    printf 'probe_strategy=required_expected_set\n'
    printf 'validation=swiftpm_listing_and_exact_execution\n'
    printf 'swift_testing_expected_set=available\n'
  } >"$TESTING_PROBE_OUTPUT"
else
  set +e
  xcrun swiftc -typecheck - >"$TESTING_PROBE_OUTPUT" 2>&1 <<<"$testing_probe_source"
  testing_probe_status=$?
  set -e
  SWIFT_TESTING_MODULE="$(
    python3 "$ROOT_DIR/tools/verify_swift_test_report.py" \
      --probe-output "$TESTING_PROBE_OUTPUT" \
      --probe-exit-code "$testing_probe_status"
  )"
  {
    printf 'swift_testing_policy=optional\n'
    printf 'probe_strategy=standalone_toolchain_probe\n'
    printf 'swift_testing_module=%s\n' "$SWIFT_TESTING_MODULE"
  } >>"$TESTING_PROBE_OUTPUT"
fi
if [[ "${PAM_REQUIRE_SWIFT_TESTING:-0}" == "1" \
  && "$SWIFT_TESTING_MODULE" != "available" ]]; then
  echo "Refusing Swift release acceptance: this toolchain is required to import Testing." >&2
  exit 3
fi

python3 "$ROOT_DIR/tools/verify_swift_test_report.py" \
  --write-expected-tests "$EXPECTED_TESTS" \
  --xctest-source "$XCTEST_SOURCE" \
  --swift-testing-source "$SWIFT_TESTING_SOURCE" \
  --baseline "$BASELINE_TESTS" \
  --swift-testing-module "$SWIFT_TESTING_MODULE" \
  >"$EXTRACTION_OUTPUT"

read -r xctest_count swift_testing_count total_count declared_swift_testing_count < <(
  python3 - "$EXPECTED_TESTS" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
active = payload["active_counts"]
declared = payload["declared_counts"]
print(
    active["xctest"],
    active["swift-testing"],
    sum(active.values()),
    declared["swift-testing"],
)
PY
)

set +e
swift test "${SWIFT_FULL_ARGS[@]}" list >"$LIST_PRIMARY_OUTPUT" 2>"$RESULT_DIR/list-primary-stderr.log"
list_primary_status=$?
set -e
if [[ "$list_primary_status" == "0" ]]; then
  cp "$LIST_PRIMARY_OUTPUT" "$LIST_OUTPUT"
else
  set +e
  swift test "${SWIFT_FULL_ARGS[@]}" --list-tests >"$LIST_FALLBACK_OUTPUT" 2>"$RESULT_DIR/list-fallback-stderr.log"
  list_fallback_status=$?
  set -e
  if [[ "$list_fallback_status" != "0" ]]; then
    echo "Refusing Swift release acceptance: both SwiftPM test listing commands failed." >&2
    exit 3
  fi
  cp "$LIST_FALLBACK_OUTPUT" "$LIST_OUTPUT"
fi
python3 "$ROOT_DIR/tools/verify_swift_test_report.py" \
  --list-output "$LIST_OUTPUT" --expected-tests "$EXPECTED_TESTS" >/dev/null

run_swift_test() {
  local output_path="$1"
  local status_path="$2"
  shift 2
  local -a pipeline_status
  set +e
  "$@" 2>&1 | tee "$output_path"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  printf '{"command_exit_code":%s,"tee_exit_code":%s}\n' \
    "${pipeline_status[0]}" "${pipeline_status[1]}" >"$status_path"
  if [[ "${pipeline_status[0]}" != "0" || "${pipeline_status[1]}" != "0" ]]; then
    echo "Refusing Swift release acceptance: Swift test command pipeline failed." >&2
    return 3
  fi
}

run_swift_test "$RUN_OUTPUT" "$RUN_STATUS" \
  swift test "${SWIFT_ARGS[@]}" \
    --parallel --num-workers 1 \
    --disable-swift-testing --filter Phase2ContextModelsTests \
    --xunit-output "$RUN_XUNIT"
executed_xctest_count="$(python3 "$ROOT_DIR/tools/verify_swift_test_report.py" \
  --report "$RUN_XUNIT" --log "$RUN_OUTPUT" --minimum "$xctest_count" \
  --expected-tests "$EXPECTED_TESTS" --expected-kind xctest \
  --require-exact-progress)"
executed_filtered_swift_testing_count=0
# This pass explicitly runs XCTest only. Swift 6.4 suppresses the console
# summary for an empty, filtered Swift Testing run. Keep the exact 35-case
# XCTest proof and require the separate full pass to prove all 19 Swift tests.
if [[ -e "$RUN_SWIFT_TESTING_XUNIT" ]]; then
  echo "Refusing Swift release acceptance: disabled Swift Testing emitted unexpected evidence." >&2
  exit 3
fi

# SwiftPM writes XCTest and Swift Testing xUnit evidence separately on the
# pinned Xcode 16.4 toolchain. Both reports are required and verified against
# disjoint exact baseline sets; an unexpected future format fails closed.
run_swift_test "$FULL_OUTPUT" "$FULL_STATUS" \
  swift test "${SWIFT_FULL_ARGS[@]}" \
    --parallel --num-workers 1 \
    --xunit-output "$FULL_XCTEST_XUNIT"
executed_full_xctest_count="$(python3 "$ROOT_DIR/tools/verify_swift_test_report.py" \
  --report "$FULL_XCTEST_XUNIT" --log "$FULL_OUTPUT" --minimum "$xctest_count" \
  --expected-tests "$EXPECTED_TESTS" --expected-kind xctest \
  --require-exact-progress)"

executed_full_swift_testing_count=0
verify_swift_testing_evidence() {
  python3 "$ROOT_DIR/tools/verify_swift_test_report.py" \
    --report "$FULL_SWIFT_TESTING_XUNIT" --log "$FULL_OUTPUT" \
    --minimum "$swift_testing_count" --expected-tests "$EXPECTED_TESTS" \
    --expected-kind swift-testing
}
if [[ "${PAM_REQUIRE_SWIFT_TESTING:-0}" == "1" ]]; then
  # The required policy is authoritative here. The earlier "available" value
  # only selects the complete expected set; this package-context evidence is
  # what proves that Swift Testing was actually compiled and executed.
  executed_full_swift_testing_count="$(verify_swift_testing_evidence)"
elif [[ "$SWIFT_TESTING_MODULE" == "available" ]]; then
  executed_full_swift_testing_count="$(verify_swift_testing_evidence)"
elif [[ -e "$FULL_SWIFT_TESTING_XUNIT" ]]; then
  echo "Refusing Swift release acceptance: unavailable Testing module emitted unexpected evidence." >&2
  exit 3
fi
executed_full_count=$((executed_full_xctest_count + executed_full_swift_testing_count))
if (( executed_full_count != total_count )); then
  echo "Refusing Swift release acceptance: combined Swift report count is not exact." >&2
  exit 3
fi

python3 - "$SUMMARY_OUTPUT" "$SWIFT_TESTING_MODULE" "$xctest_count" \
  "$swift_testing_count" "$declared_swift_testing_count" \
  "$executed_xctest_count" "$executed_filtered_swift_testing_count" \
  "$executed_full_xctest_count" \
  "$executed_full_swift_testing_count" <<'PY'
import json
import sys
from pathlib import Path

(
    output,
    module,
    expected_xctest,
    expected_swift,
    declared_swift,
    executed_xctest,
    executed_filtered_swift,
    executed_full_xctest,
    executed_full_swift,
) = sys.argv[1:]
payload = {
    "contract_version": "PAMSwiftReleaseTestEvidenceV1",
    "status": "passed",
    "swift_testing_module": module,
    "declared_swift_testing": int(declared_swift),
    "expected_xctest": int(expected_xctest),
    "expected_swift_testing": int(expected_swift),
    "expected_total": int(expected_xctest) + int(expected_swift),
    "executed_filtered_xctest": int(executed_xctest),
    "executed_filtered_swift_testing": int(executed_filtered_swift),
    "executed_full_xctest": int(executed_full_xctest),
    "executed_full_swift_testing": int(executed_full_swift),
    "executed_full_total": int(executed_full_xctest) + int(executed_full_swift),
    "skipped": 0,
    "failures": 0,
    "errors": 0,
}
Path(output).write_text(
    json.dumps(payload, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY

GATE_RESULT="passed"
printf 'Swift release tests passed: testing_module=%s expected_xctest=%s expected_swift_testing=%s executed_filtered_xctest=%s executed_filtered_swift_testing=%s executed_full_xctest=%s executed_full_swift_testing=%s evidence=%s\n' \
  "$SWIFT_TESTING_MODULE" "$xctest_count" "$swift_testing_count" \
  "$executed_xctest_count" "$executed_filtered_swift_testing_count" \
  "$executed_full_xctest_count" \
  "$executed_full_swift_testing_count" "$RESULT_DIR"
