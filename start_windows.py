#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173


def stage(number: int, total: int, message: str) -> None:
    print(f"\n[{number}/{total}] {message}", flush=True)


def fail(message: str, code: int = 1) -> int:
    print(f"\nERROR: {message}", file=sys.stderr, flush=True)
    return code


def refresh_windows_path() -> None:
    """Refresh PATH from the registry so same-session winget installs are visible."""
    if os.name != "nt":
        return
    values: list[str] = []
    try:
        import winreg

        registry_locations = [
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ]
        for hive, key_name in registry_locations:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
                    if value:
                        values.append(os.path.expandvars(str(value)))
            except OSError:
                pass
    except Exception:
        pass

    current = os.environ.get("PATH", "")
    if current:
        values.append(current)
    if values:
        os.environ["PATH"] = ";".join(values)


def find_executable(name: str, fallbacks: list[Path] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for path in fallbacks or []:
        if path.is_file():
            return str(path)
    return None


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    kwargs = {"cwd": str(cwd) if cwd else None, "check": False}
    if quiet:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True})
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        if quiet and proc.stdout:
            print(proc.stdout, file=sys.stderr)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return proc


def cmdline_for_batch(batch: str, args: list[str]) -> list[str]:
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    command = subprocess.list2cmdline([batch, *args])
    return [comspec, "/d", "/s", "/c", command]


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name:
            os.environ[name] = value


def python_version(exe: Path) -> tuple[int, int] | None:
    proc = run(
        [str(exe), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=False,
        quiet=True,
    )
    if proc.returncode != 0:
        return None
    try:
        major, minor = proc.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except Exception:
        return None


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, process: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if port_open(port):
            return True
        time.sleep(0.25)
    return False


def terminate_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass


def main() -> int:
    if os.name != "nt":
        return fail("start_windows.py is for Windows. Use ./start.sh on macOS/Linux.")

    refresh_windows_path()
    os.chdir(ROOT)
    total = 8

    print(f"Local Company Windows launcher", flush=True)
    print(f"Using Python {sys.version.split()[0]} at {sys.executable}", flush=True)

    if sys.version_info < (3, 12):
        return fail("Python 3.12 or newer is required. Install Python 3.12 and run start.cmd again.")

    stage(1, total, "Preparing application source")
    try:
        import bootstrap_windows

        bootstrap_windows.main()
    except Exception as exc:
        return fail(f"Source bootstrap failed: {exc}")

    stage(2, total, "Checking Node.js and local environment")
    npm = find_executable(
        "npm.cmd",
        [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "npm.cmd"],
    )
    node = find_executable(
        "node.exe",
        [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"],
    )
    if not npm or not node:
        return fail("Node.js 20+ was not found. Run: winget install -e --id OpenJS.NodeJS.LTS")

    node_version_proc = run([node, "--version"], quiet=True)
    node_version = node_version_proc.stdout.strip()
    print(f"Node {node_version} at {node}", flush=True)
    try:
        major = int(node_version.lstrip("v").split(".", 1)[0])
    except Exception:
        major = 0
    if major < 20:
        return fail(f"Node.js 20+ is required; found {node_version}.")

    load_dotenv()
    os.environ["LOCAL_COMPANY_ROOT"] = str(ROOT)
    os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
    os.environ.setdefault("OLLAMA_MODEL", "qwen3:8b")

    stage(3, total, "Creating/checking Python virtual environment")
    if not VENV_PYTHON.is_file():
        print("Creating .venv with the selected Python...", flush=True)
        try:
            run([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        except subprocess.CalledProcessError as exc:
            return fail(f"Could not create .venv (exit code {exc.returncode}).")

    venv_version = python_version(VENV_PYTHON)
    if not venv_version or venv_version < (3, 12):
        return fail("The existing .venv uses Python older than 3.12. Delete .venv and run start.cmd again.")
    print(f"Virtual environment Python {venv_version[0]}.{venv_version[1]}", flush=True)

    stage(4, total, "Installing/updating backend and frontend dependencies")
    try:
        run([str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"])
        run([str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-e", "./backend[dev]"])
        if not (FRONTEND / "node_modules").is_dir():
            print("Installing frontend npm packages...", flush=True)
            run(cmdline_for_batch(npm, ["install", "--no-audit", "--no-fund"]), cwd=FRONTEND)
    except subprocess.CalledProcessError as exc:
        return fail(f"Dependency installation failed (exit code {exc.returncode}).")

    public = FRONTEND / "public"
    public.mkdir(parents=True, exist_ok=True)
    activity = ROOT / "activity-map.html"
    if activity.is_file():
        shutil.copy2(activity, public / "activity-map.html")

    stage(5, total, "Checking Ollama and qwen3:8b")
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ROOT))
    ollama = find_executable("ollama.exe", [local_app_data / "Programs" / "Ollama" / "ollama.exe"])
    if not ollama:
        print("WARNING: Ollama was not found. The UI can start, but agents cannot run until Ollama is installed.", flush=True)
    else:
        proc = run([ollama, "list"], check=False, quiet=True)
        if proc.returncode != 0:
            print("WARNING: Ollama is installed but its service is not responding. Open Ollama and retry Test Model in the UI.", flush=True)
        else:
            model = os.environ["OLLAMA_MODEL"]
            installed = any(line.split() and line.split()[0] == model for line in proc.stdout.splitlines()[1:])
            if installed:
                print(f"Found Ollama model {model}", flush=True)
            else:
                print(f"WARNING: {model} is not installed. Install it manually with: ollama pull {model}", flush=True)

    if port_open(BACKEND_PORT) or port_open(FRONTEND_PORT):
        busy = [str(p) for p in (BACKEND_PORT, FRONTEND_PORT) if port_open(p)]
        return fail(
            "Port(s) " + ", ".join(busy) + " are already in use. Stop the old Local Company process (Ctrl-C in its terminal) and run start.cmd again."
        )

    stage(6, total, "Initializing persistent company database")
    try:
        run([str(VENV_PYTHON), str(ROOT / "run_backend.py"), "--seed"], cwd=ROOT)
    except subprocess.CalledProcessError as exc:
        return fail(f"Database initialization failed (exit code {exc.returncode}).")

    stage(7, total, "Checking Playwright Chromium")
    check_script = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"
    )
    probe = run([str(VENV_PYTHON), "-c", check_script], check=False, quiet=True)
    if probe.returncode != 0:
        print("Playwright Chromium is missing; installing it now...", flush=True)
        try:
            run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"])
        except subprocess.CalledProcessError as exc:
            return fail(f"Playwright Chromium installation failed (exit code {exc.returncode}).")
    else:
        print("Playwright Chromium ready.", flush=True)

    stage(8, total, "Starting Local Company")
    backend: subprocess.Popen | None = None
    frontend: subprocess.Popen | None = None
    try:
        backend = subprocess.Popen(
            [str(VENV_PYTHON), str(ROOT / "run_backend.py")],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        frontend = subprocess.Popen(
            cmdline_for_batch(
                npm,
                ["run", "dev", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"],
            ),
            cwd=str(FRONTEND),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        print("Waiting for backend...", flush=True)
        if not wait_for_port(BACKEND_PORT, backend, 30):
            return fail(f"Backend did not become ready on port {BACKEND_PORT}. Check the error above.")
        print("Waiting for frontend...", flush=True)
        if not wait_for_port(FRONTEND_PORT, frontend, 30):
            return fail(f"Frontend did not become ready on port {FRONTEND_PORT}. Check the error above.")

        print("\nLocal Company is READY:", flush=True)
        print(f"  UI:           http://127.0.0.1:{FRONTEND_PORT}", flush=True)
        print(f"  Activity map: http://127.0.0.1:{FRONTEND_PORT}/activity-map.html", flush=True)
        print(f"  Backend:      http://127.0.0.1:{BACKEND_PORT}", flush=True)
        print("  Press Ctrl-C to stop both servers.\n", flush=True)

        try:
            webbrowser.open(f"http://127.0.0.1:{FRONTEND_PORT}")
        except Exception:
            pass

        while True:
            if backend.poll() is not None:
                return fail(f"Backend exited unexpectedly with code {backend.returncode}.")
            if frontend.poll() is not None:
                return fail(f"Frontend exited unexpectedly with code {frontend.returncode}.")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping Local Company...", flush=True)
        return 0
    finally:
        terminate_tree(frontend)
        terminate_tree(backend)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed with exit code {exc.returncode}: {exc.cmd}")
