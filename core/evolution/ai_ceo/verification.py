"""Frozen, version-bound regression replay in an independent process.

Only trusted product test code executes; this is not a generated-code sandbox.
The observer author cannot change a frozen fixture without invalidating the
release manifest. A production worker does not exist in this release.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from .policy import GateDenied, iso

PROJECT = Path(__file__).resolve().parent.parent
MANIFEST = PROJECT / "acceptance-baseline.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_manifest() -> dict:
    if not MANIFEST.is_file() or MANIFEST.is_symlink():
        raise GateDenied("baseline_unsealed", "固定验收基线尚未封存，不能运行或声称通过。")
    data = json.loads(MANIFEST.read_text())
    required = {"fixtures/sync_cases.json", "ai_ceo/observers.py", "ai_ceo/verification.py",
                "ai_ceo/policy.py", "ai_ceo/__init__.py"}
    if data.get("schema_version") != 1 or set(data.get("files", {})) != required:
        raise GateDenied("baseline_invalid", "验收基线结构错误。")
    for relative, expected in data["files"].items():
        path = PROJECT / relative
        if path.is_symlink() or not path.is_file() or sha(path) != expected:
            raise GateDenied("baseline_changed", "回放数据或判定代码已变化；旧基线不能用于证明通过。")
    return data


def child() -> None:
    from .observers import evaluate_sync_snapshot
    manifest = checked_manifest()
    dataset = json.loads((PROJECT / "fixtures/sync_cases.json").read_text())
    cases = dataset["cases"]
    if dataset.get("schema_version") != 1 or len(cases) != manifest["case_count"]:
        raise GateDenied("case_count_changed", "用例集与封存基线不一致。")
    results = []
    ids = set()
    for case in cases:
        if case.get("kind") != "synthetic_regression" or case["id"] in ids:
            raise GateDenied("fixture_invalid", "用例必须明确标记为合成回放且标识唯一。")
        ids.add(case["id"])
        result = evaluate_sync_snapshot(case["input"], datetime.fromisoformat(case["now"]))
        mismatches = [key for key, expected in case["expected"].items() if result.get(key) != expected]
        results.append({"id": case["id"], "passed": not mismatches, "mismatch_fields": mismatches})
    print(json.dumps({"cases": results, "total": len(results),
                      "passed": sum(r["passed"] for r in results)}, ensure_ascii=False))


def run_replay(state_dir: Path) -> dict:
    manifest = checked_manifest()
    expected_cases = json.loads((PROJECT / "fixtures/sync_cases.json").read_text())["cases"]
    expected_ids = [case["id"] for case in expected_cases]
    if not expected_ids or len(set(expected_ids)) != len(expected_ids) or len(expected_ids) != manifest["case_count"]:
        raise GateDenied("baseline_empty_or_invalid", "基线必须含完整且唯一的固定用例。")
    baseline_sha = sha(MANIFEST)
    start = time.monotonic()
    # Isolated interpreter with only this pinned local product on sys.path.
    source = "import sys;sys.path.insert(0,sys.argv[1]);from ai_ceo.verification import child;child()"
    env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(PROJECT)],
                              env=env, cwd=state_dir, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise GateDenied("replay_timeout", "本地回放超过时间限制，未产生通过结论。") from None
    checked_manifest()
    if sha(MANIFEST) != baseline_sha:
        raise GateDenied("baseline_changed_during_run", "运行期间基线变化，结果作废。")
    if proc.returncode or len(proc.stdout) > 500_000:
        raise GateDenied("replay_process_failed", "独立回放进程失败，未产生通过结论。")
    try:
        result = json.loads(proc.stdout)
        if (not isinstance(result["cases"], list) or type(result["total"]) is not int
                or type(result["passed"]) is not int or result["total"] != len(expected_ids)):
            raise ValueError("bad count")
        if [case["id"] for case in result["cases"]] != expected_ids:
            raise ValueError("missing, duplicate or substituted case")
        for case in result["cases"]:
            if (set(case) != {"id", "passed", "mismatch_fields"} or type(case["passed"]) is not bool
                    or not isinstance(case["mismatch_fields"], list)
                    or not all(isinstance(key, str) for key in case["mismatch_fields"])
                    or case["passed"] != (not case["mismatch_fields"])):
                raise ValueError("invalid case result")
        if result["passed"] != sum(c["passed"] is True for c in result["cases"]):
            raise ValueError("bad verdict")
    except (ValueError, KeyError, TypeError):
        raise GateDenied("invalid_verifier_output", "验证器输出不完整，拒绝判定通过。") from None
    result.update({"schema_version": 1, "verdict": "pass" if result["passed"] == result["total"] else "fail",
                   "baseline_sha256": baseline_sha, "observer_sha256": sha(PROJECT / "ai_ceo/observers.py"),
                   "recorded_at": iso(), "elapsed_seconds": round(time.monotonic() - start, 3),
                   "synthetic_only": True, "business_verified": False,
                   "natural_value_verified": False, "verifier": "independent_process_fixed_expectations"})
    directory = state_dir / "verification"
    directory.mkdir(mode=0o700, exist_ok=True)
    name = "replay-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4) + ".json"
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    result["receipt_ref"] = "verification/" + name
    return result
