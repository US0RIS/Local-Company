#!/usr/bin/env python3
"""Windows entrypoint that preserves the existing launcher but swaps only the live server.

Database seeding and dependency setup still use run_backend.py. When the launcher
starts the long-running backend via subprocess.Popen, this shim redirects that
one process to serve_compat.py so the streamlined UI gets its read aliases.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import start_windows

_REAL_POPEN = subprocess.Popen
_ROOT = Path(__file__).resolve().parent
_ORIGINAL_SERVER = str((_ROOT / "run_backend.py").resolve()).lower()
_COMPAT_SERVER = str((_ROOT / "serve_compat.py").resolve())


def _compat_popen(args, *pargs, **kwargs):
    rewritten = args
    if isinstance(args, (list, tuple)):
        parts = list(args)
        for index, value in enumerate(parts):
            try:
                normalized = str(Path(str(value)).resolve()).lower()
            except Exception:
                normalized = str(value).lower()
            if normalized == _ORIGINAL_SERVER:
                parts[index] = _COMPAT_SERVER
                rewritten = parts
                break
    return _REAL_POPEN(rewritten, *pargs, **kwargs)


# start_windows imports the subprocess module object, so replacing Popen on that
# module is sufficient. subprocess.run (used for seed/setup) remains unchanged.
start_windows.subprocess.Popen = _compat_popen


if __name__ == "__main__":
    raise SystemExit(start_windows.main())
