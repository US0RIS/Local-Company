#!/usr/bin/env python3
"""Local Company backend/seed launcher with SQLite concurrency safeguards.

The application intentionally uses SQLite for V1, but the agent runtime can keep
many logical jobs alive while a single model call is in flight. A conventional
SQLAlchemy Session can therefore hold SQLite's single writer transaction open
while Python awaits inference or another agent. WAL and a busy timeout alone do
not fix that pattern.

This launcher configures WAL once before SQLAlchemy starts, uses short-lived
connections, and runs SQLite in DBAPI autocommit mode so individual statements
release the writer lock immediately. It also installs the current local UI shell
into the Vite frontend before startup so existing clones receive UI updates on
`git pull` without rebuilding the original bootstrap archive.
"""
from __future__ import annotations

import gzip
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def _install_ui() -> None:
    source = ROOT / "ui" / "grok-shell.html.gz"
    target = ROOT / "frontend" / "index.html"
    if not source.exists() or not target.parent.exists():
        return
    data = gzip.decompress(source.read_bytes())
    if not data.lstrip().startswith(b"<!doctype html>"):
        raise RuntimeError("Local Company UI bundle is invalid")
    if not target.exists() or target.read_bytes() != data:
        target.write_bytes(data)
        print("✓ Installed streamlined Local Company UI")


def _configure_database() -> None:
    """Create/configure the SQLite file before SQLAlchemy opens any connections."""
    db_path = ROOT / "runtime" / "company.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(8):
        try:
            conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=60000")
                # journal_mode is persistent. Do it once here, never in each
                # pooled/ORM connection where it can itself contend for a lock.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA wal_autocheckpoint=1000")
                conn.execute("PRAGMA foreign_keys=ON")
                return
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt == 7:
                raise
            time.sleep(min(4.0, 0.5 * (attempt + 1)))

    if last_error:
        raise last_error


_install_ui()
_configure_database()

# Patch engine creation before any Local Company module imports app.db. This
# applies to normal server startup and to the seed command below.
import sqlalchemy  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

_real_create_engine = sqlalchemy.create_engine


def _local_create_engine(url, *args, **kwargs):
    if str(url).startswith("sqlite"):
        connect_args = dict(kwargs.get("connect_args") or {})
        connect_args.setdefault("timeout", 60.0)
        connect_args.setdefault("check_same_thread", False)
        kwargs["connect_args"] = connect_args

        # Do not retain a connection/session transaction across unrelated agent
        # turns. SQLite has one writer; short-lived connections are appropriate
        # for this local single-user workload.
        kwargs.setdefault("poolclass", NullPool)

        # Critical: SQLAlchemy Sessions may remain alive while an async agent
        # awaits Ollama. DBAPI autocommit makes each SQL statement release the
        # SQLite writer lock instead of holding it until that later Session.commit().
        kwargs.setdefault("isolation_level", "AUTOCOMMIT")

    engine = _real_create_engine(url, *args, **kwargs)

    if str(url).startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=60000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


sqlalchemy.create_engine = _local_create_engine
try:
    import sqlalchemy.engine.create as _sa_create  # noqa: E402
    _sa_create.create_engine = _local_create_engine
except Exception:
    pass

try:
    import sqlmodel  # noqa: E402
    sqlmodel.create_engine = _local_create_engine
except Exception:
    pass


def _seed() -> None:
    from app.cli import seed
    seed()


def _serve() -> None:
    import uvicorn
    from app.main import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("LOCAL_COMPANY_BACKEND_PORT", "8000")),
        reload=False,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    if "--seed" in sys.argv[1:]:
        _seed()
    else:
        _serve()
