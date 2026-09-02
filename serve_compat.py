#!/usr/bin/env python3
"""Serve Local Company with read-only UI compatibility endpoints.

The bootstrapped backend remains authoritative for all writes, messaging, agent
execution, permissions, and model calls.  This module only adds GET aliases the
streamlined UI expects, backed directly by the same persistent SQLite database.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import run_backend  # applies DB safeguards/UI/policies before app import

from app.main import app

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "runtime" / "company.db"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _choose_table(existing: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in existing:
            return candidate
    # Conservative fuzzy fallback for schema revisions.
    lowered = {name.lower(): name for name in existing}
    for candidate in candidates:
        token = candidate.lower().rstrip("s")
        for lower, original in lowered.items():
            if token and token in lower and not lower.startswith("sqlite_") and "fts" not in lower:
                return original
    return None


def _rows(candidates: tuple[str, ...], *, limit: int = 500) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = _connection()
    try:
        existing = _tables(conn)
        table = _choose_table(existing, candidates)
        if not table:
            return []
        columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
        order_col = next(
            (c for c in ("updated_at", "created_at", "started_at", "timestamp", "id") if c in columns),
            None,
        )
        sql = f"SELECT * FROM {_quote(table)}"
        if order_col:
            sql += f" ORDER BY {_quote(order_col)} DESC"
        sql += " LIMIT ?"
        return [dict(row) for row in conn.execute(sql, (limit,))]
    finally:
        conn.close()


def _employees() -> list[dict]:
    return _rows(("employees", "agents"))


def _tasks() -> list[dict]:
    return _rows(("tasks", "agent_tasks", "work_items"))


def _channels() -> list[dict]:
    return _rows(("channels", "chat_channels"))


def _approvals() -> list[dict]:
    return _rows(("approvals", "approval_requests"))


def _audit() -> list[dict]:
    return _rows(("audit_events", "audit_log", "audit", "events"), limit=1000)


# The original backend already owns POST /api/agents. FastAPI can safely expose
# GET on the same path without altering the existing POST behavior.
@app.get("/api/agents", include_in_schema=False)
def compat_agents_get():
    return _employees()


@app.get("/api/employees", include_in_schema=False)
def compat_employees_get():
    return _employees()


@app.get("/api/tasks", include_in_schema=False)
def compat_tasks_get():
    return _tasks()


@app.get("/api/work/tasks", include_in_schema=False)
def compat_work_tasks_get():
    return _tasks()


@app.get("/api/channels", include_in_schema=False)
def compat_channels_get():
    return _channels()


@app.get("/api/approvals", include_in_schema=False)
def compat_approvals_get():
    return _approvals()


@app.get("/api/audit", include_in_schema=False)
def compat_audit_get():
    return _audit()


@app.get("/api/audit-log", include_in_schema=False)
def compat_audit_log_get():
    return _audit()


@app.get("/api/activity", include_in_schema=False)
def compat_activity_get():
    # Activity is intentionally the persisted audit stream. The UI can correlate
    # these rows with tasks/model calls without inventing ephemeral state.
    return _audit()


@app.get("/api/compat/status", include_in_schema=False)
def compat_status_get():
    conn = _connection()
    try:
        return {
            "ok": True,
            "database": str(DB_PATH),
            "tables": sorted(_tables(conn)),
            "aliases": [
                "/api/agents",
                "/api/employees",
                "/api/tasks",
                "/api/work/tasks",
                "/api/channels",
                "/api/approvals",
                "/api/audit",
                "/api/audit-log",
                "/api/activity",
            ],
        }
    finally:
        conn.close()


def main() -> None:
    import uvicorn

    # Keep the same single-process invariant as run_backend.py.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("LOCAL_COMPANY_BACKEND_PORT", "8000")),
        reload=False,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
