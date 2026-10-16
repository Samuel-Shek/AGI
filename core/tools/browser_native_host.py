#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    core_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(core_root))
    from activity_mvp.browser_signals import native_host_loop, native_host_core_root

    native_host_loop(native_host_core_root())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
