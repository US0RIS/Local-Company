#!/usr/bin/env python3
"""Local Company live server.

This is the only UI compatibility layer used by the launchers. The bundled
backend remains authoritative for persistence, scheduling, agent execution,
permissions, and model work.

For human DMs, this server does not guess the backend contract up front. It
submits a minimal message to the real FastAPI app, reads FastAPI's authoritative
422 validation locations, adjusts only the rejected query/body fields, and
retries. Validation failures occur before endpoint execution, so there are no
duplicate message side effects. Once validation succeeds, the endpoint executes
exactly once.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import ollama_compat  # noqa: F401 - install Ollama native fallback before core import
import run_backend  # noqa: F401 - DB/SQLAlchemy safeguards + UI policy install
from app.main import app as core_app
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "runtime" / "company.db"

app = FastAPI(title="Local Company", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEXT_KEYS = {
    "content", "text", "message", "message_text", "prompt", "body", "input", "value"
}
AGENT_KEYS = {
    "agent_id", "agentid", "employee_id", "employeeid", "recipient_id", "recipientid",
    "target_agent_id", "targetagentid", "to_agent_id", "toagentid", "recipient", "target", "to"
}
HUMAN_KEYS = {"sender", "sender_type", "author", "author_type", "role", "source"}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rows(names: tuple[str, ...], limit: int = 500) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = _db()
    try:
        existing = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = next((name for name in names if name in existing), None)
        if not table:
            return []
        columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
        order = next((c for c in ("updated_at", "created_at", "started_at", "timestamp", "id") if c in columns), None)
        sql = f"SELECT * FROM {_quote(table)}"
        if order:
            sql += f" ORDER BY {_quote(order)} DESC"
        sql += " LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (limit,))]
    finally:
        conn.close()


@app.get("/api/agents", include_in_schema=False)
@app.get("/api/employees", include_in_schema=False)
def ui_employees():
    return _rows(("employees", "agents"))


@app.get("/api/tasks", include_in_schema=False)
@app.get("/api/work/tasks", include_in_schema=False)
def ui_tasks():
    return _rows(("tasks", "agent_tasks", "work_items"))


@app.get("/api/channels", include_in_schema=False)
def ui_channels():
    return _rows(("channels", "chat_channels"))


@app.get("/api/approvals", include_in_schema=False)
def ui_approvals():
    return _rows(("approvals", "approval_requests"))


@app.get("/api/audit", include_in_schema=False)
@app.get("/api/audit-log", include_in_schema=False)
@app.get("/api/activity", include_in_schema=False)
def ui_audit():
    return _rows(("audit_events", "audit_log", "audit", "events"), 1000)


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if str(key).lower() in TEXT_KEYS and isinstance(value, str) and value.strip():
            return value.strip()
    for value in payload.values():
        if isinstance(value, dict):
            found = _extract_text(value)
            if found:
                return found
    return None


def _extract_agent(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if str(key).lower() not in AGENT_KEYS:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("id", "agent_id", "agentId", "employee_id", "employeeId", "value"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    for value in payload.values():
        if isinstance(value, dict):
            found = _extract_agent(value)
            if found:
                return found
    return None


def _value_for(field: str | None, text: str, agent_id: str, error_type: str = "") -> Any:
    token = str(field or "").lower()
    if token in AGENT_KEYS or ("agent" in token and token.endswith("id")):
        return agent_id
    if token in HUMAN_KEYS:
        return "human"
    if "bool" in error_type:
        return False
    if "int" in error_type:
        return 0
    return text


def _set_nested(obj: dict[str, Any], path: list[Any], value: Any) -> bool:
    if not path or any(isinstance(part, int) for part in path):
        return False
    cur: dict[str, Any] = obj
    for part in path[:-1]:
        key = str(part)
        child = cur.get(key)
        if not isinstance(child, dict):
            child = {}
            cur[key] = child
        cur = child
    cur[str(path[-1])] = value
    return True


def _delete_nested(obj: dict[str, Any], path: list[Any]) -> bool:
    if not path or any(isinstance(part, int) for part in path):
        return False
    cur: dict[str, Any] = obj
    for part in path[:-1]:
        child = cur.get(str(part))
        if not isinstance(child, dict):
            return False
        cur = child
    return cur.pop(str(path[-1]), None) is not None


def _validation_details(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        data = response.json()
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    detail = data.get("detail")
    return detail if isinstance(detail, list) else []


def _adapt_from_422(
    details: list[dict[str, Any]],
    query: dict[str, str],
    body: Any,
    text: str,
    agent_id: str,
) -> tuple[dict[str, str], Any, bool]:
    changed = False

    for error in details:
        if not isinstance(error, dict):
            continue
        error_type = str(error.get("type") or "")
        loc = list(error.get("loc") or [])
        if not loc:
            continue
        where = str(loc[0])
        path = loc[1:]
        field = str(path[-1]) if path else None

        if where == "query":
            if not field:
                continue
            value = _value_for(field, text, agent_id, error_type)
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            if query.get(field) != rendered:
                query[field] = rendered
                changed = True
            continue

        if where != "body":
            continue

        # Scalar body: FastAPI tells us the whole JSON object should have been a
        # primitive string/bool/int. The human message is the only meaningful
        # string scalar in a DM request.
        if not path and ("string_type" in error_type or "str" in error_type):
            if body != text:
                body = text
                changed = True
            continue
        if not path and "bool" in error_type:
            if body is not False:
                body = False
                changed = True
            continue

        if not isinstance(body, dict):
            body = {}
            changed = True

        if "extra_forbidden" in error_type:
            if _delete_nested(body, path):
                changed = True
            continue

        if "missing" in error_type or error_type in {
            "string_type", "bool_type", "int_type", "uuid_type", "literal_error", "enum"
        }:
            value = _value_for(field, text, agent_id, error_type)
            if field and str(field).lower() in HUMAN_KEYS and error_type in {"literal_error", "enum"}:
                value = "human"
            if _set_nested(body, path, value):
                changed = True

    return query, body, changed


async def _core_post(agent_id: str, query: dict[str, str], body: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=core_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://local-company-core") as client:
        kwargs: dict[str, Any] = {"params": query}
        if body is not None:
            kwargs["json"] = body
        return await client.post(f"/api/agents/{agent_id}/messages", **kwargs)


async def _send(agent_id: str, payload: Any) -> Response:
    text = _extract_text(payload)
    if not text:
        return JSONResponse(status_code=422, content={"detail": "Message text is missing"})

    # Start clean rather than forwarding Grok-shell metadata that a strict
    # Pydantic model may reject. `content` is the canonical message key; if the
    # backend wants a query param, another field name, or a scalar body, FastAPI's
    # first 422 tells us exactly what to change.
    query: dict[str, str] = {}
    body: Any = {"content": text}
    attempts: list[dict[str, Any]] = []

    for _ in range(8):
        response = await _core_post(agent_id, query, body)
        if response.status_code != 422:
            blocked = {"content-length", "content-encoding", "transfer-encoding", "connection", "content-type"}
            headers = {k: v for k, v in response.headers.items() if k.lower() not in blocked}
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=headers,
                media_type=response.headers.get("content-type", "application/json").split(";", 1)[0],
            )

        details = _validation_details(response)
        attempts.append({
            "query": dict(query),
            "body": body,
            "validation": details,
        })
        query, body, changed = _adapt_from_422(details, query, body, text, agent_id)
        if not changed:
            break

    # Return a plain string detail so the UI displays the useful error rather
    # than `[object Object]`, while keeping full validation history available.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "The real backend rejected the message after validation-driven adaptation.",
            "attempts": attempts,
        },
    )


@app.post("/api/agents/{agent_id}/messages", include_in_schema=False)
async def send_agent_message(agent_id: str, request: Request):
    try:
        payload: Any = await request.json()
    except Exception:
        payload = (await request.body()).decode("utf-8", "replace")
    return await _send(agent_id, payload)


@app.post("/api/messages", include_in_schema=False)
async def send_generic_message(request: Request):
    try:
        payload: Any = await request.json()
    except Exception:
        payload = {}
    agent_id = _extract_agent(payload)
    if not agent_id:
        return JSONResponse(status_code=422, content={"detail": "Message destination is missing"})
    return await _send(agent_id, payload)


@app.get("/api/compat/status", include_in_schema=False)
def compat_status():
    return {
        "ok": True,
        "message_adapter": "fastapi-validation-driven-v1",
        "ollama_native_fallback": True,
    }


# All endpoints not explicitly adapted above remain the real backend endpoints.
app.mount("/", core_app)


def main() -> None:
    import uvicorn

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
