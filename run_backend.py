#!/usr/bin/env python3
"""Local Company backend/seed launcher with SQLite concurrency safeguards."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def _configure_existing_database() -> None:
    """Enable WAL before SQLAlchemy opens its connection pool.

    WAL lets API readers coexist with the single logical writer much more safely.
    The retry is deliberately bounded: a genuinely live foreign process should
    not leave startup hanging forever.
    """
    db_path = ROOT / "runtime" / "company.db"
    if not db_path.exists():
        return

    last_error: Exception | None = None
    for attempt in range(6):
        try:
            conn = sqlite3.connect(db_path, timeout=60.0)
            try:
                conn.execute("PRAGMA busy_timeout=60000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA wal_autocheckpoint=1000")
                conn.commit()
                return
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.5 * (attempt + 1))

    if last_error:
        raise last_error


_configure_existing_database()

# Patch engine creation before any Local Company module imports app.db.  This
# therefore applies to normal server startup AND the seed command below.
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
