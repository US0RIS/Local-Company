#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

EXPECTED_BUNDLE_SHA256="be4b46495a0a44380770ae694769ed584073088b3fbf0399616c93f4800e01e9"

find_python() {
  local candidate
  local candidates=()

  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("$PYTHON_BIN")
  fi

  # Prefer an explicitly versioned modern Python. This matters on macOS, where
  # /usr/bin/python3 may still be Apple's older system Python even after a
  # newer Homebrew Python has been installed.
  candidates+=(python3.14 python3.13 python3.12 python3)
  candidates+=(/opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12)
  candidates+=(/usr/local/bin/python3.14 /usr/local/bin/python3.13 /usr/local/bin/python3.12)

  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
      then
        command -v "$candidate"
        return 0
      fi
    fi
  done

  return 1
}

PYTHON_EXECUTABLE="$(find_python || true)"
if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  echo "Python 3.12+ is required, but no compatible interpreter was found." >&2
  echo "On Homebrew macOS, install it with: brew install python@3.12" >&2
  echo "Then run ./start.sh again." >&2
  exit 1
fi

echo "✓ Using $($PYTHON_EXECUTABLE --version 2>&1) at $PYTHON_EXECUTABLE"

bootstrap_source() {
  if [[ -f backend/app/main.py && -f frontend/package.json ]]; then
    return
  fi

  echo "Preparing Local Company source from the repository bootstrap bundle..."
  "$PYTHON_EXECUTABLE" - "$ROOT" "$EXPECTED_BUNDLE_SHA256" <<'PY'
import base64
import hashlib
import io
import pathlib
import sys
import tarfile

root = pathlib.Path(sys.argv[1]).resolve()
expected = sys.argv[2]
parts = sorted((root / "bootstrap").glob("bundle.part.*"))
if not parts:
    raise SystemExit("Bootstrap bundle is missing. Re-clone US0RIS/Local-Company and try again.")

payload = b"".join(part.read_bytes().strip() for part in parts)
try:
    archive = base64.b64decode(payload, validate=True)
except Exception as exc:
    raise SystemExit(f"Bootstrap bundle is corrupt: {exc}") from exc

actual = hashlib.sha256(archive).hexdigest()
if actual != expected:
    raise SystemExit(
        f"Bootstrap checksum mismatch. Expected {expected}, got {actual}. "
        "Re-clone the repository rather than running unverified source."
    )

with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
    for member in tf.getmembers():
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise SystemExit(f"Unsafe path in bootstrap archive: {member.name}")
    tf.extractall(root)

required = [root / "backend/app/main.py", root / "frontend/package.json", root / "docs/ARCHITECTURE.md"]
missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
if missing:
    raise SystemExit("Bootstrap completed incompletely; missing: " + ", ".join(missing))

print("Source ready.")
PY
}

bootstrap_source

if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js/npm is required for the React UI. Install Node.js 20+ and run ./start.sh again." >&2
  exit 1
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export LOCAL_COMPANY_ROOT="$ROOT"
export OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:8b}"

if [[ ! -d .venv ]]; then
  "$PYTHON_EXECUTABLE" -m venv .venv
fi

# Refuse a stale incompatible virtual environment rather than deleting it.
if ! .venv/bin/python - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
then
  echo "The existing .venv uses Python older than 3.12." >&2
  echo "Remove it manually with 'rm -rf .venv' and run ./start.sh again." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e './backend[dev]'

if [[ ! -d frontend/node_modules ]]; then
  (cd frontend && npm install --no-audit --no-fund)
fi

if command -v ollama >/dev/null 2>&1; then
  if ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx "$OLLAMA_MODEL"; then
    echo "✓ Found Ollama model $OLLAMA_MODEL"
  else
    echo "! Ollama is installed, but $OLLAMA_MODEL was not found."
    echo "  Local Company will still start; Test Model will show setup status."
    echo "  Verify the installed models with: ollama list"
    echo "  No model will be downloaded automatically."
  fi
else
  echo "! The ollama command is not available."
  echo "  Local Company will still start so setup status is visible in the UI."
  echo "  Install/start Ollama and verify qwen3:8b with: ollama list"
fi

python -m app.cli seed

# Install Playwright Chromium only if the already-installed browser cannot launch.
python - <<'PY' >/dev/null 2>&1 || python -m playwright install chromium
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    browser.close()
PY

cleanup() {
  kill "${BACK_PID:-}" "${FRONT_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# run_backend.py applies WAL + busy_timeout to every SQLite connection and runs
# exactly one backend process. This prevents UI reads/background agent writes
# from failing with `sqlite3.OperationalError: database is locked`.
python "$ROOT/run_backend.py" &
BACK_PID=$!
(cd frontend && npm run dev -- --host 127.0.0.1) &
FRONT_PID=$!

echo
echo "Local Company is starting:"
echo "  UI:      http://127.0.0.1:5173"
echo "  Backend: http://127.0.0.1:8000"
echo "Press Ctrl-C to stop both servers."
echo

wait
