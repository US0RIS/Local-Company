# Local Company

A persistent, fully local AI company built around one shared Ollama model (`qwen3:8b` by default). The app provides stable AI employees in an editable management hierarchy, DMs and company/department/project channels, persisted goals/projects/tasks/meetings/memory, real manager delegation, bounded autonomous work, local filesystem/terminal/Git/Playwright tools, permission approvals, and a full audit/model-call trail.

Employees are instructed to communicate like actual coworkers rather than task-system bots: concise first-person workplace language, role-specific voice, natural uncertainty, and no useless acknowledgement chatter. Observable model/action cards are presented as concise **Thoughts** summaries: the important consideration, decision/tradeoff, and next move. Raw hidden chain-of-thought is not displayed.

## Qwen thinking modes

Local Company uses Qwen3's hybrid thinking support to avoid paying the latency cost of deep reasoning on every routine action.

- **CEO:** always runs in **DEEP** mode (`/think`).
- **Managers:** every delegated child task must explicitly choose **DEEP** or **FAST** and state why.
- **DEEP (`/think`):** architecture, ambiguous debugging, research synthesis, QA judgment, reviews, planning, consequential tradeoffs.
- **FAST (`/no_think`):** deterministic execution such as running a chosen command, reading a known file, applying a precise edit, collecting a metric, or executing an established test.
- Individual contributors default to FAST unless the delegated task explicitly requires DEEP reasoning.

The runtime maintains a persistent `task_thinking_policies` index (`DEEP`, `FAST`, `AUTO`) derived from the actual task instructions so debugging/UI tooling can show whether a worker was expected to think.

## Run on Windows

Prerequisites:

- Windows 10/11
- Git
- Python 3.12+
- Node.js 20+ / npm
- Ollama for Windows, running locally
- `qwen3:8b` already installed (check with `ollama list`)

From Windows Terminal, PowerShell, or Command Prompt:

```powershell
git clone https://github.com/US0RIS/Local-Company.git
cd Local-Company
.\start.cmd
```

If you already cloned it:

```powershell
cd Local-Company
git pull origin main
.\start.cmd
```

`start.cmd` invokes the native `start.ps1` launcher with a local execution-policy bypass, so you do not need to change your machine-wide PowerShell policy. It automatically finds Python 3.12+, reconstructs the source bundle on a fresh clone, creates `.venv`, installs Python/npm dependencies, checks Ollama and `qwen3:8b`, installs Playwright Chromium if necessary, initializes the persistent database, stops stale Local Company Python/Node workers, and starts FastAPI plus Vite.

Open **http://127.0.0.1:5173**.

Live organization/activity visualization: **http://127.0.0.1:5173/activity-map.html**.

## Run on macOS

Prerequisites:

- Python 3.12+
- Node.js 20+ / npm
- Ollama installed and running
- `qwen3:8b` already installed (check with `ollama list`)

Then:

```bash
git clone https://github.com/US0RIS/Local-Company.git
cd Local-Company
./start.sh
```

If you already cloned it:

```bash
git pull origin main
./start.sh
```

Open **http://127.0.0.1:5173**.

Live organization/activity visualization: **http://127.0.0.1:5173/activity-map.html**. It shows hierarchy ranks, manager/subordinate edges, task-assignment overlays, active/queued work, elapsed time, observable structured model actions/normal outputs, delegated work, and the next expected task for each employee. It intentionally does not expose hidden chain-of-thought.

On a fresh clone, the platform launcher first reconstructs the validated application source from the repository's checksum-verified bootstrap bundle. It then creates `.venv`, installs backend/frontend dependencies, seeds the persistent default company, checks the existing Ollama installation, installs Playwright Chromium if necessary, and starts FastAPI and Vite.

**It never downloads an AI model or silently falls back to a cloud model.** If Ollama or `qwen3:8b` is unavailable, the UI still starts and reports setup status; use the **Test Model** control after fixing Ollama.

Default local model configuration:

```text
provider: Ollama
host:     http://127.0.0.1:11434
model:    qwen3:8b
physical inference concurrency: 1
```

Runtime state is stored under `runtime/` and is not deleted on restart.

## Validation

The original bundled source was validated before publication with:

```text
Python compileall             PASS
Deterministic backend tests   16 passed
TypeScript source transpile   PASS
Bootstrap checksum/extract    PASS
```

The remaining checks necessarily run on the target machine: real `qwen3:8b` inference, npm production build with downloaded packages, Playwright browser launch, and the real-model end-to-end acceptance workflows.

After the first launch, the full source tree is present locally, including `backend/`, `frontend/`, `docs/`, Alembic migrations, tests, `PROJECT_STATUS.md`, and `.env.example`.
