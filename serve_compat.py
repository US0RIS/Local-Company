#!/usr/bin/env python3
"""Serve Local Company with UI compatibility endpoints.

The bootstrapped backend remains authoritative for writes, messaging, agent
execution, permissions, and model calls. This module adds read aliases expected
by the streamlined UI and a narrow request-body adapter for agent DMs so the UI
can speak the backend's real FastAPI/Pydantic schema without duplicating runtime
logic.
"""
from __future__ import annotations

import enum
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, get_args, get_origin

import run_backend  # applies DB safeguards/UI/policies before app import

from app.main import app
from fastapi.routing import APIRoute
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "runtime" / "company.db"
_AGENT_MESSAGE_RE = re.compile(r"^/api/agents/([^/]+)/messages/?$")
_TEXT_KEYS = (
    "content",
    "text",
    "message",
    "message_text",
    "prompt",
    "body",
    "input",
    "value",
)
_AGENT_KEYS = ("agent_id", "employee_id", "recipient_id", "target_agent_id")
_HUMAN_KEYS = ("sender", "sender_type", "author", "author_type", "role", "source")


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


def _agent_message_route() -> APIRoute | None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == "/api/agents/{agent_id}/messages" and "POST" in (route.methods or set()):
            return route
    return None


def _body_model_fields(route: APIRoute | None) -> dict[str, Any]:
    if route is None:
        return {}
    candidates: list[Any] = []
    if route.body_field is not None:
        candidates.append(getattr(route.body_field, "type_", None))
        candidates.append(getattr(getattr(route.body_field, "field_info", None), "annotation", None))
    for body_param in getattr(route.dependant, "body_params", []) or []:
        candidates.append(getattr(body_param, "type_", None))
        candidates.append(getattr(getattr(body_param, "field_info", None), "annotation", None))
    for candidate in candidates:
        if candidate is None:
            continue
        fields = getattr(candidate, "model_fields", None)
        if isinstance(fields, dict) and fields:
            return fields
        fields = getattr(candidate, "__fields__", None)
        if isinstance(fields, dict) and fields:
            return fields
    return {}


def _field_required(field: Any) -> bool:
    checker = getattr(field, "is_required", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    required = getattr(field, "required", None)
    return bool(required) if required is not None else False


def _field_annotation(field: Any) -> Any:
    annotation = getattr(field, "annotation", None)
    if annotation is not None:
        return annotation
    return getattr(field, "outer_type_", None) or getattr(field, "type_", None)


def _enum_or_literal_human_value(annotation: Any) -> Any | None:
    if annotation is None:
        return None
    origin = get_origin(annotation)
    if origin is not None:
        for arg in get_args(annotation):
            value = _enum_or_literal_human_value(arg)
            if value is not None:
                return value
        if str(origin).endswith("Literal"):
            values = list(get_args(annotation))
            for wanted in ("human", "user", "owner"):
                for value in values:
                    if str(value).lower() == wanted:
                        return value
        return None
    try:
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            members = list(annotation)
            for wanted in ("human", "user", "owner"):
                for member in members:
                    if str(member.value).lower() == wanted or member.name.lower() == wanted:
                        return member.value
    except Exception:
        pass
    return None


def _extract_text(payload: dict[str, Any]) -> str | None:
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for nested in _TEXT_KEYS:
                nested_value = value.get(nested)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value
    return None


def _normalize_message_payload(payload: dict[str, Any], agent_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map the streamlined UI body into the original POST route's model.

    We introspect the actual Pydantic model, so this survives field-name changes
    in the bootstrapped V1 backend. Only missing fields are added; valid original
    payloads pass through unchanged.
    """
    route = _agent_message_route()
    fields = _body_model_fields(route)
    if not fields:
        return payload, {"fields": [], "changed": False}

    out = dict(payload)
    text = _extract_text(payload)
    required_missing = [name for name, field in fields.items() if _field_required(field) and name not in out]

    for name, field in fields.items():
        if name in out:
            continue
        lower = name.lower()
        if lower in _TEXT_KEYS and text is not None:
            out[name] = text
            continue
        if lower in _AGENT_KEYS:
            out[name] = agent_id
            continue
        if lower in _HUMAN_KEYS:
            value = _enum_or_literal_human_value(_field_annotation(field))
            if value is not None:
                out[name] = value

    # If the backend has exactly one required body field and it is still absent,
    # it is overwhelmingly likely to be the human message string. This handles
    # schemas using a nonstandard name without hard-coding it.
    still_missing = [name for name in required_missing if name not in out]
    if text is not None and len(still_missing) == 1:
        out[still_missing[0]] = text

    return out, {
        "fields": list(fields.keys()),
        "required": [name for name, field in fields.items() if _field_required(field)],
        "changed": out != payload,
        "received_keys": list(payload.keys()),
        "normalized_keys": list(out.keys()),
    }


@app.middleware("http")
async def compat_message_body_adapter(request: Request, call_next):
    match = _AGENT_MESSAGE_RE.match(request.url.path)
    if request.method.upper() != "POST" or not match:
        return await call_next(request)

    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        return await call_next(request)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return await call_next(request)
    if not isinstance(payload, dict):
        return await call_next(request)

    normalized, debug = _normalize_message_payload(payload, match.group(1))
    if normalized == payload:
        return await call_next(request)

    new_body = json.dumps(normalized, ensure_ascii=False).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": new_body, "more_body": False}

    request._receive = receive  # Starlette request body replacement for downstream FastAPI validation.
    request.state.local_company_message_adapter = debug
    return await call_next(request)


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
    return _audit()


@app.get("/api/compat/message-schema", include_in_schema=False)
def compat_message_schema_get():
    route = _agent_message_route()
    fields = _body_model_fields(route)
    return {
        "ok": route is not None,
        "route": route.path if route else None,
        "fields": {
            name: {
                "required": _field_required(field),
                "annotation": str(_field_annotation(field)),
            }
            for name, field in fields.items()
        },
        "adapter_accepts": list(_TEXT_KEYS),
    }


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
                "/api/compat/message-schema",
            ],
            "message_adapter": True,
        }
    finally:
        conn.close()


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
