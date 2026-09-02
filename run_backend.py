#!/usr/bin/env python3
"""Local Company backend launcher.

This module is intentionally imported before app.main so SQLite engines created by
SQLAlchemy/SQLModel get safe local-concurrency defaults.  The application has one
physical inference worker but several logical/background jobs, so API reads and
background writes can overlap.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# Configure the existing database before SQLAlchemy opens pooled connections.
# WAL is persistent in the database file and permits readers while a writer is active.
db_path = ROOT / "runtime" / "company.db"
if db_path.exists():
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.commit()
    finally:
        conn.close()

# Patch engine construction before importing Local Company's app modules.  This
# covers both `from sqlalchemy import create_engine` and SQLModel's re-export.
import sqlalchemy  # noqa: E402
from sqlalchemy import event  # noqa: E402

_real_create_engine = sqlalchemy.create_engine


def _local_create_engine(url, *args, **kwargs):
    if str(url).startswith("sqlite"):
        connect_args = dict(kwargs.get("connect_args") or {})
        connect_args.setdefault("timeout", 60.0)
        connect_args.setdefault("check_same_thread", False)
        kwargs["connect_args"] = connect_args

    engine = _real_create_engine(url, *args, **kwargs)

    if str(url).startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=60000")
                cursor.execute("PRAGMA journal_mode=WAL")
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

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("LOCAL_COMPANY_BACKEND_PORT", "8000")),
        reload=False,
        workers=1,
        log_level="info",
    )
