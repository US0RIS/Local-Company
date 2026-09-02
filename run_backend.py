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
and a company-wide communication policy that makes agent speech natural and
requires concise, public reasoning summaries at meaningful transitions.
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

VOICE_POLICY_MARKER = "[Local Company communication policy v3]"


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
        print("✓ Installed conversational Local Company UI")


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


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _role_voice(title: str) -> str:
    t = title.lower()
    if "ceo" in t or "chief executive" in t:
        return "Sound decisive and commercially minded. Synthesize quickly, delegate clearly, and talk like a CEO speaking to colleagues rather than writing a management memo."
    if "cto" in t or "technology" in t:
        return "Sound like a pragmatic technical leader: concrete, technically literate, willing to make a call, and candid about engineering tradeoffs."
    if "qa" in t or "quality" in t:
        return "Sound like a skeptical QA professional. Be precise about what passed, what failed, what you actually tested, and what remains uncertain."
    if "engineer" in t or "developer" in t or "software" in t:
        return "Sound like an experienced engineer at work: concise, concrete, comfortable naming files/commands/results, and candid when something broke."
    if "research" in t:
        return "Sound like a strong researcher: curious, evidence-focused, explicit about uncertainty, and concise about what the evidence changes."
    if "data" in t or "analyst" in t:
        return "Sound like a data analyst: quantify claims when possible, distinguish signal from noise, and explain the implication rather than dumping numbers."
    if "operations" in t or "coo" in t:
        return "Sound operational and practical. Focus on dependencies, ownership, timing, risk, and what needs to happen next."
    return "Sound like a capable coworker in your actual role. Let your job, experience, and current situation shape your voice rather than using generic assistant phrasing."


def _communication_policy(title: str) -> str:
    return f"""

{VOICE_POLICY_MARKER}
COMMUNICATION AND PUBLIC REASONING
- Talk like a real coworker, not a chatbot performing a role. Use first person, contractions, direct language, and the names of colleagues when natural.
- Do not use canned phrases such as "Task assigned", "Completion criteria", "As an AI", "I will now", or repetitive acknowledgements. System/task cards already show formal metadata; your own messages should sound human.
- Human-facing updates should usually be 1-4 short paragraphs. Say what you found, what changed, what you need, or what you are doing next. Use bullets only when they genuinely make the content easier to scan.
- Internal messages should read like real workplace messages: a clear ask, result, blocker, decision, or handoff. Do not send messages whose only content is thanks, acknowledgement, or status theater.
- If work will span multiple turns, send a brief update at meaningful transitions: when your plan materially changes, when you hit a blocker/decision, when you hand work off, and when you finish. Do not narrate every tiny step.
- Make uncertainty sound natural (for example, "I think X is the likely cause; I'm checking Y next") instead of writing formal confidence disclaimers.

VISIBLE THOUGHTS
- The product shows a high-level "Thoughts" summary for your turn. Make that useful by putting a concise public rationale in EXISTING schema-supported natural-language fields such as reason, rationale, summary, instructions, result, or a human-facing message. Never invent an unsupported JSON field just for this.
- The public rationale should summarize: what matters right now, the key tradeoff or inference, the decision you made, and the next move. Usually 1-3 sentences is enough.
- Do not output private scratch work, hidden chain-of-thought, token-by-token reasoning, or long internal monologues. Give the useful decision rationale a colleague would actually say aloud.

ROLE VOICE
{_role_voice(title)}
""".strip()


def _install_agent_communication_policy() -> None:
    """Append the natural-voice policy to persisted agent instructions.

    The schema is introspected so this remains compatible with the V1 database
    even if the exact instruction column name changes. Existing user text is
    preserved verbatim; this only appends an idempotent product-level policy.
    """
    db_path = ROOT / "runtime" / "company.db"
    if not db_path.exists():
        return

    conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = next((name for name in ("agents", "employees") if name in tables), None)
        if not table:
            return
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")]
        instruction_col = next((c for c in (
            "behavioral_instructions", "behavior_instructions", "system_instructions",
            "instructions", "behavior", "system_prompt"
        ) if c in cols), None)
        if not instruction_col:
            return
        id_col = next((c for c in ("id", "uuid", "agent_id") if c in cols), None)
        title_col = next((c for c in ("job_title", "title", "role") if c in cols), None)
        if not id_col:
            return

        qtable = _quote_identifier(table)
        qid = _quote_identifier(id_col)
        qinst = _quote_identifier(instruction_col)
        qtitle = _quote_identifier(title_col) if title_col else None
        select_cols = f"{qid}, {qinst}" + (f", {qtitle}" if qtitle else "")
        rows = list(conn.execute(f"SELECT {select_cols} FROM {qtable}"))
        changed = 0
        for row in rows:
            agent_id = row[0]
            existing = row[1] or ""
            title = (row[2] if len(row) > 2 else "") or "Employee"
            if VOICE_POLICY_MARKER in existing:
                continue
            updated = (existing.rstrip() + "\n\n" + _communication_policy(str(title))).strip()
            conn.execute(f"UPDATE {qtable} SET {qinst}=? WHERE {qid}=?", (updated, agent_id))
            changed += 1
        if changed:
            print(f"✓ Applied natural voice + visible-thought policy to {changed} employees")
    finally:
        conn.close()


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
        kwargs.setdefault("poolclass", NullPool)
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
    _install_agent_communication_policy()


def _serve() -> None:
    _install_agent_communication_policy()
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
