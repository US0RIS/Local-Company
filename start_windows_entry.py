#!/usr/bin/env python3
"""Windows entrypoint that preserves setup/seed and swaps only the live server."""
from __future__ import annotations

import subprocess
from pathlib import Path

import start_windows

_REAL_POPEN = subprocess.Popen
_ROOT = Path(__file__).resolve().parent
_ORIGINAL_SERVER = str((_ROOT / "run_backend.py").resolve()).lower()
_FINAL_SERVER = str((_ROOT / "serve_final.py").resolve())


def _compat_popen(args, *pargs, **kwargs):
    rewritten = args
    if isinstance(args, (list, tuple)):
        parts = list(args)
        is_seed = any(str(value).lower() == "--seed" for value in parts)
        if not is_seed:
            for index, value in enumerate(parts):
                try:
                    normalized = str(Path(str(value)).resolve()).lower()
                except Exception:
                    normalized = str(value).lower()
                if normalized == _ORIGINAL_SERVER:
                    parts[index] = _FINAL_SERVER
                    rewritten = parts
                    break
    return _REAL_POPEN(rewritten, *pargs, **kwargs)


start_windows.subprocess.Popen = _compat_popen


if __name__ == "__main__":
    raise SystemExit(start_windows.main())
