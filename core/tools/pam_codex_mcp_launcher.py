#!/usr/bin/env python3
"""Start only the narrow PAM consumer in an isolated, local STDIO process."""
import os
from pathlib import Path
import runpy
import sys

if sys.flags.isolated != 1 or sys.flags.no_site != 1 or sys.flags.safe_path is not True:
    raise SystemExit('isolated_mode_required')
sys.dont_write_bytecode = True
os.umask(0o077)
sys.path.insert(0, str(Path(__file__).resolve(strict=True).parents[1]))
runpy.run_module('activity_mvp.codex_mcp', run_name='__main__', alter_sys=True)
