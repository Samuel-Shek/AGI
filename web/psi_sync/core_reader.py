"""Bounded, allowlisted Core subprocess reads and tolerant contract parsing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import tempfile

LOG = logging.getLogger(__name__)
PYTHON = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
CORE = "~/Library/Application Support/PSI/core"
COLLECTIONS = ("decisions", "systems", "resources", "closures")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,199}\Z")
ITEM_FIELDS = set("id kind title summary requires_decision member_count members background reason impact inaction recommendation attempts completion_rule revision group_key card children state recurrence_count source_code actions current_evidence_count historical_evidence_count investigation investigation_original current_verification recovery source phase task_id owner requires_check evidence filename file_name size size_bytes created_at modified_at runtime closure_runtime_label closure_recovery_label codex_thread_id codex_open_via_handoff codex_handoff_available".split())
TOP_FIELDS = set("interface contract status raw_access generated_at revision decision_count system_count resource_count activity_question_count information_insufficient_count memory_total memory_pending autonomy_enabled decisions systems resources closures display_scope_revision reason error message item".split())
ITEM_FIELDS.add("codex_reply_available")
CARD_FIELDS = set("group_key member_revision event_type section priority state canonical_subject subject_status semantic_key display_title display_question display_evidence verification_route app time_label observed_start observed_end source_count reason_codes answer_options raw_access".split())
OPTION_FIELDS = {"id", "title", "label", "impact", "recommended", "is_recommended"}


class ReadError(ValueError):
    """Safe error codes only: never include Core output, paths or stderr."""


def valid_id(value):
    return isinstance(value, str) and bool(ID_RE.fullmatch(value)) and ".." not in value


def decode(raw):
    try:
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReadError("invalid_json") from exc


def _unknown(value, allowed):
    # Field names can themselves contain private text. Log counts, never names/values.
    count = len(set(value) - allowed)
    if count:
        LOG.info("ignored_unknown_fields count=%d", count)


def normalize_options(rows):
    if not isinstance(rows, list):
        raise ReadError("invalid_options")
    result = []
    for row in rows:
        if isinstance(row, str):
            result.append(row)
            continue
        if not isinstance(row, dict):
            raise ReadError("invalid_option")
        _unknown(row, OPTION_FIELDS)
        option = {key: value for key, value in row.items() if key in OPTION_FIELDS}
        for key in ("id", "title", "label", "impact"):
            if key in option and not isinstance(option[key], str):
                raise ReadError("invalid_option_field")
        for key in ("recommended", "is_recommended"):
            if key in option and type(option[key]) is not bool:
                raise ReadError("invalid_option_field")
        result.append(option)
    return result


def normalize_item(raw, depth=0):
    if not isinstance(raw, dict) or not valid_id(raw.get("id")) or depth > 8:
        raise ReadError("invalid_item")
    _unknown(raw, ITEM_FIELDS)
    item = {key: value for key, value in raw.items() if key in ITEM_FIELDS}
    for key in ("kind", "title", "summary", "revision", "group_key", "background", "reason", "impact", "recommendation", "state"):
        if key in item and not isinstance(item[key], str):
            raise ReadError("invalid_item_field")
        item.setdefault(key, "")
    if "requires_decision" in item and type(item["requires_decision"]) is not bool:
        raise ReadError("invalid_item_field")
    for key in ('codex_open_via_handoff', 'codex_handoff_available', 'codex_reply_available'):
        if key in item and type(item[key]) is not bool:
            raise ReadError('invalid_handoff_field')
    thread = item.get('codex_thread_id')
    if thread is not None and (not isinstance(thread, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', thread)):
        raise ReadError('invalid_handoff_thread')
    item.setdefault("requires_decision", False)
    for key in ("member_count", "recurrence_count"):
        if key in item and (type(item[key]) is not int or item[key] < 0):
            raise ReadError("invalid_item_field")
    item.setdefault("member_count", 0)
    for key in ("card",):
        if key in item and not isinstance(item[key], dict):
            raise ReadError("invalid_item_field")
        item.setdefault(key, {})
    card = item["card"]
    _unknown(card, CARD_FIELDS)
    item["card"] = {key: value for key, value in card.items() if key in CARD_FIELDS}
    for key in ("semantic_key", "event_type", "verification_route", "display_title", "display_question"):
        if key in card and not isinstance(card[key], str):
            raise ReadError("invalid_card_field")
    item["actions"] = normalize_options(item.get("actions", []))
    if "answer_options" in card:
        item["card"]["answer_options"] = normalize_options(card["answer_options"])
    for key in ("children", "members"):
        if key in item and not isinstance(item[key], list):
            raise ReadError("invalid_item_field")
        item.setdefault(key, [])
    item["children"] = [normalize_item(child, depth + 1) for child in item["children"]]
    return item


def envelope(raw):
    if not isinstance(raw, dict):
        raise ReadError("invalid_contract")
    if raw.get("reason") in ("display_scope_unavailable", "source_not_in_display_scope"):
        raise ReadError("scope_withdrawn")
    if raw.get("interface") != "attention-center" or type(raw.get("contract")) is not int or raw["contract"] != 1:
        raise ReadError("invalid_contract")
    _unknown(raw, TOP_FIELDS)


def normalize_snapshot(raw):
    envelope(raw)
    if raw.get("status") not in ("ok", "partial"):
        raise ReadError("unavailable")
    result = {key: raw.get(key, "") for key in ("generated_at", "revision", "display_scope_revision")}
    if any(not isinstance(value, str) for value in result.values()):
        raise ReadError("invalid_contract")
    result["status"] = raw["status"]
    result["counts"] = {key: raw.get(key) if type(raw.get(key)) is int and raw[key] >= 0 else None
                        for key in ("decision_count", "system_count", "resource_count")}
    result["counts_present"] = [key for key in result["counts"] if key in raw]
    # Missing lists are filled, but an incomplete envelope must never assert zero.
    for key in COLLECTIONS:
        if key not in raw:
            result["status"] = "partial"
        rows = raw.get(key, [])
        if not isinstance(rows, list):
            raise ReadError("invalid_collection")
        result[key] = [normalize_item(row) for row in rows]
    # Core's complete flag does not cover the independent feedback projection.
    if any(row["id"] == "feedback-read-unavailable" for row in result["systems"]):
        result["status"] = "partial"
    result["interface"] = "attention-center"
    result["contract"] = 1
    return result


def normalize_detail(raw, identifier):
    envelope(raw)
    if raw.get("status") != "ok":
        raise ReadError("detail_unavailable")
    item = normalize_item(raw.get("item"))
    if item["id"] != identifier:
        raise ReadError("detail_id_mismatch")
    return item


@dataclass(frozen=True)
class CoreConfig:
    python: str = PYTHON
    launcher: str = CORE + "/pam_isolated_launcher.py"
    db: str = CORE + "/data/activity.sqlite"
    timeout: float = 60
    output_limit: int = 4 * 1024 * 1024

    def __post_init__(self):
        if not 0 < self.timeout <= 120 or not 1024 <= self.output_limit <= 16 * 1024 * 1024:
            raise ValueError("invalid_read_limits")

    def argv(self, action, identifier=None):
        if action not in {"snapshot", "detail"}:
            raise ValueError("command_not_allowed")
        if action == "detail" and not valid_id(identifier):
            raise ValueError("invalid_id")
        if action == "snapshot" and identifier is not None:
            raise ValueError("unexpected_id")
        args = [os.path.expanduser(self.python), "-I", "-S", os.path.expanduser(self.launcher),
                "--db", os.path.expanduser(self.db), "attention-center", action]
        return args + (["--id", identifier] if action == "detail" else [])


def bounded_run(argv, timeout, limit, input_data=None, cwd=None):
    """Drain both pipes with a combined hard cap and one deadline, including EOF wait."""
    source = None
    if input_data is not None:
        if not isinstance(input_data, bytes) or len(input_data) > 32768:
            raise ReadError('invalid_input')
        source = tempfile.TemporaryFile()
        source.write(input_data)
        source.seek(0)
    try:
        process = subprocess.Popen(argv, stdin=source if source else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, start_new_session=True, cwd=cwd)
    except OSError as exc:
        if source: source.close()
        raise ReadError("start_failed") from exc
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadError("timeout")
                events = selector.select(remaining)
                for key, _ in events:
                    block = os.read(key.fd, min(65536, limit - total + 1))
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > limit:
                        raise ReadError("output_limit")
                    if key.fileobj is process.stdout:
                        output.extend(block)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadError("timeout")
            if process.wait(timeout=remaining):
                raise ReadError("exit_failed")
        return bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise ReadError("timeout") from exc
    except OSError as exc:
        raise ReadError("read_io_failed") from exc
    finally:
        # Only this invocation's own process group, never a service or arbitrary PID.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()
        if source: source.close()


class CoreReader:
    def __init__(self, config=None, fixture=None):
        self.config = config or CoreConfig()
        self.fixture = Path(fixture).expanduser() if fixture else None

    @property
    def cache_identity(self):
        # Never mix synthetic data, another fixture, or a different Core source.
        if self.fixture:
            source = ["fixture", str(self.fixture.absolute())]
        else:
            source = self.config.argv("snapshot")
            # Relative launcher/db arguments are resolved by the subprocess cwd.
            # Include PATH for bare executable names; do not probe installed Core.
            if "/" not in source[0]:
                source[0] = [source[0], os.environ.get("PATH", "")]
            else:
                source[0] = os.path.abspath(source[0])
            for index in (3, 5):
                source[index] = os.path.abspath(source[index])
        return hashlib.sha256(json.dumps(source).encode()).hexdigest()

    def _raw(self, action, identifier=None):
        if self.fixture is None:
            argv = self.config.argv(action, identifier)
            return decode(bounded_run(argv, self.config.timeout, self.config.output_limit))
        try:
            with self.fixture.open("rb") as stream:
                data = stream.read(self.config.output_limit + 1)
        except OSError as exc:
            raise ReadError("fixture_unavailable") from exc
        if len(data) > self.config.output_limit:
            raise ReadError("output_limit")
        bundle = decode(data)
        if not isinstance(bundle, dict):
            raise ReadError("invalid_fixture")
        if action == "snapshot":
            snapshot = bundle.get("snapshot", bundle)
            # Fixture-only control, outside the Core envelope. Production reads
            # always retain their real source timestamp, including stale dates.
            if bundle.get("fixture_clock") == "now" and isinstance(snapshot, dict):
                snapshot = {**snapshot, "generated_at": datetime.now(timezone.utc).isoformat()}
            return snapshot
        details = bundle.get("details", {})
        if not isinstance(details, dict) or identifier not in details:
            raise ReadError("detail_unavailable")
        return details[identifier]

    def snapshot(self):
        return normalize_snapshot(self._raw("snapshot"))

    def detail(self, identifier, registered_ids):
        if not valid_id(identifier) or identifier not in registered_ids:
            raise ReadError("unregistered_id")
        return normalize_detail(self._raw("detail", identifier), identifier)
