#!/usr/bin/env python3
"""Trusted PAM module entrypoint for Python isolated mode."""

from __future__ import annotations

import runpy
import json
import os
import sys
from pathlib import Path


if sys.flags.isolated != 1 or sys.flags.no_site != 1 or sys.flags.safe_path is not True:
    raise SystemExit("isolated_mode_required")

sys.dont_write_bytecode = True
os.umask(0o077)
core_root = Path(__file__).resolve(strict=True).parent
sys.path.insert(0, str(core_root))
if sys.argv[1:] == ["--render-launch-agent"]:
    from activity_mvp.launch_agent_contract import build_launch_agent_payload

    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise SystemExit("render_request_too_large")
    request = json.loads(raw.decode("utf-8"))
    payload = build_launch_agent_payload(request)
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    raise SystemExit(0)
runpy.run_module("activity_mvp", run_name="__main__", alter_sys=True)
