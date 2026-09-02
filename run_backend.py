#!/usr/bin/env python3
"""Local Company backend/seed launcher with SQLite concurrency safeguards.

This launcher also installs product-level communication and thinking policies.
Qwen3 supports hybrid thinking/non-thinking operation. Local Company uses that
capability deliberately: the CEO always thinks, managers choose a thinking mode
for delegated work, and deterministic worker tasks can run in fast mode.

Raw hidden chain-of-thought is never exposed. The UI shows a concise public
reasoning summary: what matters, the decision/tradeoff, and the next action.
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

VOICE_POLICY_MARKER = "[Local Company communication policy v4]"

# This is injected into the existing Grok-inspired shell at startup. It does not
# expose model scratch-work; it makes the existing observable model/action cards
# read as human-friendly thought summaries and explains the two Qwen3 modes.
UI_THOUGHTS_OVERLAY = r"""
<style id="lc-thoughts-style">
  .lc-thinking-legend{position:fixed;right:18px;bottom:18px;z-index:9999;background:rgba(15,18,24,.94);border:1px solid rgba(148,163,184,.22);border-radius:12px;padding:9px 12px;color:#aab4c5;font:12px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.22);pointer-events:none;backdrop-filter:blur(10px)}
  .lc-thinking-legend b{color:#eef2ff;font-weight:650}.lc-thinking-legend .deep{color:#c4b5fd}.lc-thinking-legend .fast{color:#67e8f9}
</style>
<script id="lc-thoughts-script">
(() => {
  const replacements = [
    [/^Model turn$/i, 'Thoughts'],
    [/^Parsed structured action$/i, 'Decision'],
    [/^Parsed action$/i, 'Decision'],
    [/Thinking mode:\s*DEEP\s*\(\/think\)/gi, 'Thinking mode: 🧠 DEEP'],
    [/Thinking mode:\s*FAST\s*\(\/no_think\)/gi, 'Thinking mode: ⚡ FAST'],
    [/Thinking mode:\s*REQUIRED/gi, 'Thinking mode: 🧠 DEEP'],
    [/Thinking mode:\s*SKIP/gi, 'Thinking mode: ⚡ FAST']
  ];

  function rewrite(root=document.body) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      let text = node.nodeValue;
      for (const [pattern, value] of replacements) text = text.replace(pattern, value);
      if (text !== node.nodeValue) node.nodeValue = text;
    }
  }

  function addLegend() {
    if (document.querySelector('.lc-thinking-legend')) return;
    const el = document.createElement('div');
    el.className = 'lc-thinking-legend';
    el.innerHTML = '<b>Thinking</b> · <span class="deep">🧠 Deep</span> for reasoning · <span class="fast">⚡ Fast</span> for execution';
    document.body.appendChild(el);
  }

  const start = () => {
    rewrite(); addLegend();
    const observer = new MutationObserver(muts => {
      for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1 || n.nodeType === 3) rewrite(n.nodeType === 1 ? n : n.parentNode);
      addLegend();
    });
    observer.observe(document.body, {subtree:true, childList:true});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
</script>
""".strip()


def _install_ui() -> None:
    source = ROOT / "ui" / "grok-shell.html.gz"
    target = ROOT / "frontend" / "index.html"
    if not source.exists() or not target.parent.exists():
        return
    data = gzip.decompress(source.read_bytes())
    if not data.lstrip().startswith(b"<!doctype html>"):
        raise RuntimeError("Local Company UI bundle is invalid")
    overlay = UI_THOUGHTS_OVERLAY.encode("utf-8")
    marker = b'id="lc-thoughts-script"'
    if marker not in data:
        if b"</body>" in data:
            data = data.replace(b"</body>", overlay + b"\n</body>", 1)
        else:
            data += b"\n" + overlay
    if not target.exists() or target.read_bytes() != data:
        target.write_bytes(data)
        print("✓ Installed conversational UI with visible thought summaries")


def _configure_database() -> None:
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
        return "Sound decisive and commercially minded. Synthesize quickly, delegate clearly, and speak like a CEO to colleagues rather than writing a management memo."
    if "cto" in t or "technology" in t:
        return "Sound like a pragmatic technical leader: concrete, technically literate, decisive, and candid about engineering tradeoffs."
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
    return "Sound like a capable coworker in your actual role. Let your job and current situation shape your voice instead of using generic assistant phrasing."


def _thinking_policy(*, is_ceo: bool, is_manager: bool) -> str:
    common = """
THINKING-MODE CONTRACT
Qwen3 has two execution modes and the mode token is operational, not decorative:
- Thinking mode: 🧠 DEEP. Put the exact token `/think` as the FINAL line of the delegated task instructions. Use it for architecture, debugging with uncertainty, research synthesis, novel planning, QA judgment, reviews, consequential tradeoffs, or anything where a wrong quick answer is likely to cost more than the extra inference time.
- Thinking mode: ⚡ FAST. Put the exact token `/no_think` as the FINAL line of the delegated task instructions. Use it for deterministic execution: run an already-chosen command, read a known file, apply a precisely specified edit, collect a metric, format data, execute an established test, or other work where extended reasoning is unlikely to improve the result.
- Every `delegate_task` must explicitly choose exactly one of those two modes. In the natural-language instructions, include a short line `Thinking mode: DEEP` or `Thinking mode: FAST` plus a one-sentence reason, then end the instructions with the corresponding exact Qwen token.
- Do not choose DEEP reflexively. The point of FAST is to save wall-clock time and output tokens on routine work. Do not choose FAST merely to be quick when judgment materially affects correctness.
- A worker must obey the task's final `/think` or `/no_think` token. Do not silently upgrade FAST to DEEP just because you prefer to deliberate; ask the manager if the task turns out to be materially more ambiguous than expected.
""".strip()

    if is_ceo:
        return common + """

CEO SPECIAL RULES
- You, the CEO, ALWAYS run in DEEP thinking mode. Your own prompt ends in `/think`, and you never switch yourself to `/no_think`.
- Before delegating, explicitly decide whether each direct-report task deserves DEEP or FAST. This is part of the assignment, just like priority and completion criteria.
- When a project will cascade through a manager, tell the manager to preserve this economy: use DEEP only for child tasks that genuinely require judgment and FAST for routine execution.
- Your public Thoughts summary should briefly state why you chose the work/delegate and, when relevant, why you chose DEEP versus FAST. Do not reveal scratch-work.

/think"""
    if is_manager:
        return common + """

MANAGER RULES
- Management decisions, review, reprioritization, and delegation normally deserve DEEP thinking unless your own parent task explicitly ends in `/no_think` and the requested action is deterministic.
- When you create child tasks, choose DEEP or FAST independently for each child and make the choice visible in the task instructions.
- Keep the public Thoughts summary short: the key consideration, your decision, and the next move.

/think"""
    return common + """

INDIVIDUAL-CONTRIBUTOR DEFAULT
- Your default is FAST because most assigned execution should not pay for unnecessary deliberation.
- If your current task ends in `/think`, that task overrides the default and you should use DEEP reasoning.
- If it ends in `/no_think`, act directly and do not generate planning chatter. You may still report blockers, actual results, and a concise rationale for any unavoidable judgment.

/no_think"""


def _communication_policy(title: str, *, is_ceo: bool, is_manager: bool) -> str:
    return f"""
{VOICE_POLICY_MARKER}
REAL-PERSON COMMUNICATION
- Talk like a real coworker, not a chatbot performing a role. Use first person, contractions, direct language, and colleagues' names when natural.
- Do not use canned phrases such as "Task assigned", "Completion criteria", "As an AI", "I will now", or repetitive acknowledgements. System/task cards already show formal metadata; your messages should sound human.
- Human-facing updates should usually be 1-4 short paragraphs. Say what you found, what changed, what you need, or what you're doing next. Use bullets only when they genuinely improve scanning.
- Internal messages should look like actual workplace messages: a clear ask, result, blocker, decision, or handoff. Never spend a model turn merely saying thanks or acknowledging receipt.
- If work spans multiple turns, speak at meaningful transitions: plan/priority changed, blocker/decision reached, handoff made, material result obtained, or work finished. Do not narrate every tiny step.
- Make uncertainty natural: "I think the lock is coming from the worker session; I'm checking that next" is better than a formal confidence paragraph.

VISIBLE THOUGHTS
- The UI exposes a concise public `Thoughts` summary for meaningful model turns. This is NOT raw chain-of-thought.
- Put useful decision rationale into EXISTING schema-supported natural-language fields such as reason, rationale, summary, instructions, result, or a human-facing message. Never invent an unsupported JSON field solely for thoughts.
- A good public Thoughts summary is 1-3 sentences: what matters right now, the key tradeoff/inference, the decision, and the next move.
- Never output private scratch work, hidden chain-of-thought, token-by-token reasoning, or a long internal monologue. The user gets the useful professional rationale, not the private derivation.

ROLE VOICE
{_role_voice(title)}

{_thinking_policy(is_ceo=is_ceo, is_manager=is_manager)}
""".strip()


def _install_agent_communication_policy() -> None:
    """Append natural voice + Qwen thinking policy to persisted employees.

    Existing user-authored instructions are preserved. The schema is introspected
    because older bootstrapped databases may use slightly different column names.
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

        qtable = _quote_identifier(table)
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({qtable})")]
        instruction_col = next((c for c in (
            "behavioral_instructions", "behavior_instructions", "system_instructions",
            "instructions", "behavior", "system_prompt"
        ) if c in cols), None)
        id_col = next((c for c in ("id", "uuid", "agent_id") if c in cols), None)
        title_col = next((c for c in ("job_title", "title", "role") if c in cols), None)
        manager_col = next((c for c in ("manager_id", "manager", "reports_to_id") if c in cols), None)
        if not instruction_col or not id_col:
            return

        qid = _quote_identifier(id_col)
        qinst = _quote_identifier(instruction_col)
        qtitle = _quote_identifier(title_col) if title_col else None
        qmanager = _quote_identifier(manager_col) if manager_col else None
        select_cols = [qid, qinst]
        if qtitle: select_cols.append(qtitle)
        if qmanager: select_cols.append(qmanager)
        rows = list(conn.execute(f"SELECT {', '.join(select_cols)} FROM {qtable}"))

        # Resolve column positions in the dynamic SELECT.
        title_idx = 2 if qtitle else None
        manager_idx = 3 if qtitle and qmanager else (2 if qmanager else None)
        manager_ids = {
            row[manager_idx] for row in rows
            if manager_idx is not None and row[manager_idx] not in (None, "")
        }

        changed = 0
        for row in rows:
            agent_id = row[0]
            existing = row[1] or ""
            title = str(row[title_idx] if title_idx is not None and row[title_idx] else "Employee")
            manager_id = row[manager_idx] if manager_idx is not None else None
            title_lower = title.lower()
            is_ceo = "ceo" in title_lower or "chief executive" in title_lower or (manager_col is not None and manager_id in (None, ""))
            is_manager = is_ceo or agent_id in manager_ids

            if VOICE_POLICY_MARKER in existing:
                continue
            updated = (existing.rstrip() + "\n\n" + _communication_policy(title, is_ceo=is_ceo, is_manager=is_manager)).strip()
            conn.execute(f"UPDATE {qtable} SET {qinst}=? WHERE {qid}=?", (updated, agent_id))
            changed += 1

        if changed:
            print(f"✓ Applied real-person voice + Qwen thinking policy to {changed} employees")
    finally:
        conn.close()


def _install_task_thinking_index() -> None:
    """Persist an inspectable thinking-mode index derived from task instructions.

    The main V1 task schema remains untouched. This side table gives future UI/API
    code a stable first-class place to inspect which tasks were assigned DEEP vs
    FAST, while the actual Qwen switch remains the `/think` or `/no_think` token
    embedded in the delegated task instructions.
    """
    db_path = ROOT / "runtime" / "company.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_thinking_policies (
                task_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('DEEP','FAST','AUTO')),
                source TEXT NOT NULL DEFAULT 'task_instructions',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        task_table = next((name for name in ("tasks", "task") if name in tables), None)
        if not task_table:
            return
        qtask = _quote_identifier(task_table)
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({qtask})")]
        id_col = next((c for c in ("id", "uuid", "task_id") if c in cols), None)
        text_col = next((c for c in ("description", "instructions", "body", "details") if c in cols), None)
        if not id_col or not text_col:
            return
        qid = _quote_identifier(id_col)
        qtext = _quote_identifier(text_col)
        for task_id, text in conn.execute(f"SELECT {qid}, COALESCE({qtext}, '') FROM {qtask}"):
            low = str(text).lower()
            mode = "FAST" if "/no_think" in low else ("DEEP" if "/think" in low else "AUTO")
            conn.execute(
                "INSERT INTO task_thinking_policies(task_id, mode, updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(task_id) DO UPDATE SET mode=excluded.mode, updated_at=CURRENT_TIMESTAMP",
                (str(task_id), mode),
            )

        # Keep newly created/edited tasks synchronized without modifying the ORM.
        insert_trigger = "lc_task_thinking_policy_insert"
        update_trigger = "lc_task_thinking_policy_update"
        conn.execute(f'DROP TRIGGER IF EXISTS "{insert_trigger}"')
        conn.execute(f'DROP TRIGGER IF EXISTS "{update_trigger}"')
        mode_expr = f"CASE WHEN instr(lower(COALESCE(NEW.{qtext},'')), '/no_think') > 0 THEN 'FAST' WHEN instr(lower(COALESCE(NEW.{qtext},'')), '/think') > 0 THEN 'DEEP' ELSE 'AUTO' END"
        conn.execute(f"""
            CREATE TRIGGER "{insert_trigger}" AFTER INSERT ON {qtask}
            BEGIN
              INSERT INTO task_thinking_policies(task_id,mode,updated_at)
              VALUES(CAST(NEW.{qid} AS TEXT), {mode_expr}, CURRENT_TIMESTAMP)
              ON CONFLICT(task_id) DO UPDATE SET mode=excluded.mode,updated_at=CURRENT_TIMESTAMP;
            END
        """)
        conn.execute(f"""
            CREATE TRIGGER "{update_trigger}" AFTER UPDATE OF {qtext} ON {qtask}
            BEGIN
              INSERT INTO task_thinking_policies(task_id,mode,updated_at)
              VALUES(CAST(NEW.{qid} AS TEXT), {mode_expr}, CURRENT_TIMESTAMP)
              ON CONFLICT(task_id) DO UPDATE SET mode=excluded.mode,updated_at=CURRENT_TIMESTAMP;
            END
        """)
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
    _install_task_thinking_index()


def _serve() -> None:
    _install_agent_communication_policy()
    _install_task_thinking_index()
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
