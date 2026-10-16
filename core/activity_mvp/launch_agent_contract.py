"""Closed, deterministic LaunchAgent payload builder for PAM runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import unicodedata
from urllib.parse import urlsplit


LAUNCH_AGENT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ISOLATED_LAUNCHER_NAME = "pam_isolated_launcher.py"
ISOLATED_MODULE_BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('activity_mvp',run_name='__main__',alter_sys=True)"
)
OPTIONAL_CORE_ENV_KEYS = {
    "PAM_DOCUMENT_CHANGE_STARTUP_RECONCILE_ENABLED",
}


def _validated_environment(kind: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and isinstance(item, str)
        and item
        and len(item.encode("utf-8")) <= 4096
        and not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in item)
        for key, item in value.items()
    ):
        raise ValueError("launch_agent_environment_invalid")
    required = {"PATH", "PAM_PRIVACY_PROFILE"} if kind == "core" else {"PATH"}
    allowed = required | (OPTIONAL_CORE_ENV_KEYS if kind == "core" else set())
    if set(value) - allowed or not required.issubset(value) or value.get("PATH") != LAUNCH_AGENT_PATH:
        raise ValueError("launch_agent_environment_invalid")
    if any(key.startswith("PYTHON") for key in value):
        raise ValueError("launch_agent_environment_invalid")
    if kind == "core" and value.get("PAM_PRIVACY_PROFILE") not in {
        "strict",
        "personal_high_precision",
    }:
        raise ValueError("launch_agent_environment_invalid")
    return {str(key): str(item) for key, item in value.items()}


def build_launch_agent_payload(request: dict[str, Any]) -> dict[str, Any]:
    common = {
        "kind",
        "label",
        "program",
        "core_root",
        "database",
        "stdout_path",
        "stderr_path",
        "environment",
    }
    kind = request.get("kind")
    expected = common | (set() if kind == "core" else {"socket"})
    if kind not in {"core", "shadow"} or set(request) != expected:
        raise ValueError("launch_agent_request_fields_invalid")
    values = {key: request[key] for key in expected if key != "environment"}
    if not all(
        isinstance(value, str)
        and value
        and len(value.encode("utf-8")) <= 4096
        and not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
        for value in values.values()
    ):
        raise ValueError("launch_agent_request_value_invalid")
    for key in ("program", "core_root", "database", "stdout_path", "stderr_path"):
        path = Path(values[key])
        if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
            raise ValueError("launch_agent_request_path_invalid")
    core_root = values["core_root"]
    environment = _validated_environment(kind, request["environment"])
    if kind == "core":
        arguments = [
            values["program"],
            "-I",
            "-S",
            str(Path(core_root) / ISOLATED_LAUNCHER_NAME),
            "--db",
            values["database"],
            "run-daemon",
        ]
    else:
        if not Path(values["socket"]).is_absolute():
            raise ValueError("launch_agent_request_path_invalid")
        arguments = [
            values["program"],
            "-I",
            "-S",
            "-c",
            ISOLATED_MODULE_BOOTSTRAP,
            core_root,
            "--db",
            values["database"],
            "task-sources",
            "serve-receipts",
            "--socket",
            values["socket"],
        ]
    payload: dict[str, Any] = {
        "Label": values["label"],
        "ProgramArguments": arguments,
        "WorkingDirectory": core_root,
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": values["stdout_path"],
        "StandardErrorPath": values["stderr_path"],
    }
    if kind == "shadow":
        payload.update({"ProcessType": "Background", "ThrottleInterval": 2})
    return payload
