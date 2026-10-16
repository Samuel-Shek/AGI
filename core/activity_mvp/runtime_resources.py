"""Local power and idle probes shared by non-visual maintenance."""
from __future__ import annotations
import re
import subprocess


def _low_power_mode_enabled() -> bool:
    """Report macOS Low Power Mode, fail-closed to False when unreadable.

    ``pmset -g batt`` does not carry this flag, so it needs its own read of
    ``pmset -g`` (about 8ms).  Unknown reads answer False so an unreadable
    probe never silently stops collection; callers that must be conservative
    check ``external_power`` instead.
    """
    try:
        completed = subprocess.run(
            ["pmset", "-g"],
            check=False,
            timeout=2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"(?m)^\s*lowpowermode\s+(\d+)\s*$", completed.stdout)
    return bool(match) and match.group(1) != "0"

def power_state() -> dict:
    low_power_mode = _low_power_mode_enabled()
    try:
        completed = subprocess.run(
            ["pmset", "-g", "batt"],
            check=False,
            timeout=2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "external_power": False,
            "source": "unknown",
            "summary": "电源状态不可用。",
            "low_power_mode": low_power_mode,
        }
    text = completed.stdout.strip()
    lowered = text.lower()
    if "ac power" in lowered:
        state = {"external_power": True, "source": "ac_power", "summary": "当前接电。"}
    elif "battery power" in lowered:
        state = {
            "external_power": False,
            "source": "battery_power",
            "summary": "当前使用电池。",
        }
    else:
        state = {
            "external_power": True,
            "source": "desktop_or_unknown",
            "summary": "未检测到电池供电。",
        }
    state["low_power_mode"] = low_power_mode
    return state

def system_idle_seconds() -> dict:
    try:
        completed = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            check=False,
            timeout=2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "idle_seconds": 0}
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', completed.stdout)
    if not match:
        return {"available": False, "idle_seconds": 0}
    return {"available": True, "idle_seconds": int(int(match.group(1)) / 1_000_000_000)}
