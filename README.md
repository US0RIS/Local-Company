# Local Company

A persistent, fully local AI company built around one shared Ollama model (`qwen3:8b` by default). The app provides stable AI employees in an editable management hierarchy, DMs and company/department/project channels, persisted goals/projects/tasks/meetings/memory, real manager delegation, bounded autonomous work, local filesystem/terminal/Git/Playwright tools, permission approvals, and a full audit/model-call trail.

## Run on your Mac

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

Open **http://127.0.0.1:5173**.

Live organization/activity visualization: **http://127.0.0.1:5173/activity-map.html**. It shows hierarchy ranks, manager/subordinate edges, task-assignment overlays, active/queued work, elapsed time, observable structured model actions/normal outputs, delegated work, and the next expected task for each employee. It intentionally does not expose hidden chain-of-thought.

On a fresh clone, `start.sh` first reconstructs the validated application source from the repository's checksum-verified bootstrap bundle. It then creates `.venv`, installs backend/frontend dependencies, seeds the persistent default company, checks the existing Ollama installation, installs Playwright Chromium if necessary, and starts FastAPI and Vite.

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

The bundled source was validated before publication with:

```text
Python compileall             PASS
Deterministic backend tests   16 passed
TypeScript source transpile   PASS
Bootstrap checksum/extract    PASS
```

The remaining checks necessarily run on the target Mac: real `qwen3:8b` inference, npm production build with downloaded packages, Playwright browser launch, and the real-model end-to-end acceptance workflows.

After the first launch, the full source tree is present locally, including `backend/`, `frontend/`, `docs/`, Alembic migrations, tests, `PROJECT_STATUS.md`, and `.env.example`.