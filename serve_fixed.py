#!/usr/bin/env python3
"""Local Company live server with a thin, deterministic UI compatibility layer.

The bundled backend remains authoritative for persistence, scheduling, agent
execution, permissions, and model work. This module only:
  * exposes read aliases used by the streamlined UI;
  * resolves the REAL POST message route recursively through mounted FastAPI /
    Starlette sub-applications;
  * adapts the UI message into that route's actual query/body contract; and
  * enables the Ollama native /api/chat fallback before the backend is imported.

There is intentionally no request-body mutation middleware here.
"""
from __future__ import annotations

import enum
import os
import sqlite3
from pathlib import Path
from typing import Any, get_args, get_origin

import httpx
import ollama_compat  # installs before backend import
import run_backend  # DB/SQLAlchemy safeguards + UI installation
from app.main import app as core_app
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse, Response
from starlette.routing import Match

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


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _pick_table(existing: set[str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in existing:
            return name
    return None


def _rows(names: tuple[str, ...], limit: int = 500) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = _db()
    try:
        table = _pick_table(_tables(conn), names)
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


def _probe_scope(agent_id: str = "00000000-0000-0000-0000-000000000000") -> dict[str, Any]:
    path = f"/api/agents/{agent_id}/messages"
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
        "root_path": "",
        "app": core_app,
    }


def _resolve_route(router_like: Any, scope: dict[str, Any]) -> APIRoute | None:
    """Resolve through nested Mount/FastAPI/Router objects using Starlette itself."""
    routes = getattr(router_like, "routes", None)
    if routes is None:
        router = getattr(router_like, "router", None)
        routes = getattr(router, "routes", None) if router is not None else None
    if not routes:
        return None

    partial: APIRoute | None = None
    for route in routes:
        try:
            match, child_scope = route.matches(scope)
        except Exception:
            continue
        if match == Match.NONE:
            continue
        if isinstance(route, APIRoute):
            if match == Match.FULL:
                return route
            if partial is None:
                partial = route
            continue

        nested = getattr(route, "app", None)
        if nested is not None and match == Match.FULL:
            next_scope = dict(scope)
            if isinstance(child_scope, dict):
                next_scope.update(child_scope)
            found = _resolve_route(nested, next_scope)
            if found is not None:
                return found

        nested_routes = getattr(route, "routes", None)
        if nested_routes is not None and match == Match.FULL:
            next_scope = dict(scope)
            if isinstance(child_scope, dict):
                next_scope.update(child_scope)
            found = _resolve_route(route, next_scope)
            if found is not None:
                return found
    return partial


def _real_message_route() -> APIRoute | None:
    return _resolve_route(core_app, _probe_scope())


def _required(field: Any) -> bool:
    fn = getattr(field, "is_required", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            pass
    value = getattr(field, "required", None)
    return bool(value) if value is not None else False


def _annotation(field: Any) -> Any:
    value = getattr(field, "annotation", None)
    if value is not None:
        return value
    value = getattr(field, "type_", None)
    if value is not None:
        return value
    return getattr(getattr(field, "field_info", None), "annotation", None)


def _alias(field: Any, name: str) -> str:
    for attr in ("validation_alias", "alias"):
        value = getattr(field, attr, None)
        if isinstance(value, str) and value:
            return value
    return name


def _model_fields(model: Any) -> dict[str, Any]:
    if model is None:
        return {}
    value = getattr(model, "model_fields", None)
    if isinstance(value, dict) and value:
        return value
    value = getattr(model, "__fields__", None)
    return value if isinstance(value, dict) else {}


def _contains_type(annotation: Any, wanted: type) -> bool:
    if annotation is wanted:
        return True
    origin = get_origin(annotation)
    return bool(origin is not None and any(_contains_type(x, wanted) for x in get_args(annotation)))


def _human_value(annotation: Any) -> Any | None:
    if _contains_type(annotation, str):
        return "human"
    try:
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            members = list(annotation)
            for wanted in ("human", "user", "owner"):
                for member in members:
                    if member.name.lower() == wanted or str(member.value).lower() == wanted:
                        return member.value
            return members[0].value if members else None
    except Exception:
        pass
    origin = get_origin(annotation)
    if origin is not None:
        for item in get_args(annotation):
            if str(item).lower() in {"human", "user", "owner"}:
                return item
            value = _human_value(item)
            if value is not None:
                return value
    return None


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if str(key).lower() in TEXT_KEYS and isinstance(value, str) and value.strip():
            return value
    for value in payload.values():
        if isinstance(value, dict):
            text = _extract_text(value)
            if text:
                return text
    return None


def _extract_agent(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if str(key).lower() in AGENT_KEYS:
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


def _field_value(name: str, alias: str, field: Any, source: dict[str, Any], text: str | None, agent_id: str) -> tuple[bool, Any]:
    if alias in source:
        return True, source[alias]
    if name in source:
        return True, source[name]
    token = name.lower()
    alias_token = alias.lower()
    ann = _annotation(field)
    if (token in TEXT_KEYS or alias_token in TEXT_KEYS) and text is not None:
        return True, text
    if token in AGENT_KEYS or alias_token in AGENT_KEYS:
        return True, agent_id
    if token in HUMAN_KEYS or alias_token in HUMAN_KEYS:
        value = _human_value(ann)
        if value is not None:
            return True, value
    if _required(field) and _contains_type(ann, bool):
        return True, False
    return False, None


def _build_contract(payload: Any, agent_id: str) -> tuple[dict[str, str], Any, dict[str, Any]]:
    route = _real_message_route()
    if route is None:
        return {}, payload, {"ok": False, "error": "real message route could not be resolved"}

    dep = route.dependant
    source = payload if isinstance(payload, dict) else {}
    text = _extract_text(payload)
    query: dict[str, str] = {}
    missing_query: list[str] = []

    # FastAPI turns ordinary scalar function parameters into QUERY parameters.
    # This was the source of the persistent 422 in the streamlined UI.
    query_params = list(getattr(dep, "query_params", []) or [])
    for field in query_params:
        name = str(getattr(field, "name", None) or getattr(field, "alias", None) or "")
        alias = str(getattr(field, "alias", None) or name)
        present, value = _field_value(name, alias, field, source, text, agent_id)
        if not present and _required(field) and text is not None and _contains_type(_annotation(field), str):
            # If there is one required string query parameter, it is almost
            # certainly the message text, regardless of its chosen name.
            value = text
            present = True
        if present:
            query[alias] = str(value).lower() if isinstance(value, bool) else str(value)
        elif _required(field):
            missing_query.append(name)

    body_params = list(getattr(dep, "body_params", []) or [])
    body: Any = None
    missing_body: list[str] = []

    if len(body_params) == 1:
        param = body_params[0]
        param_name = str(getattr(param, "name", None) or getattr(param, "alias", None) or "body")
        param_alias = str(getattr(param, "alias", None) or param_name)
        ann = _annotation(param)
        fields = _model_fields(ann)
        if fields:
            clean: dict[str, Any] = {}
            unresolved: list[str] = []
            for name, field in fields.items():
                alias = _alias(field, name)
                present, value = _field_value(name, alias, field, source, text, agent_id)
                if not present and _required(field) and text is not None and _contains_type(_annotation(field), str):
                    value = text
                    present = True
                if present:
                    clean[alias] = value
                elif _required(field):
                    unresolved.append(name)
            if text is not None and len(unresolved) == 1:
                only = unresolved.pop()
                clean[_alias(fields[only], only)] = text
            body = clean
            missing_body.extend(unresolved)
        elif _contains_type(ann, str):
            body = text or ""
        elif isinstance(source, dict) and param_alias in source:
            body = source[param_alias]
        elif isinstance(source, dict) and param_name in source:
            body = source[param_name]
        else:
            body = payload
            if _required(param) and body is None:
                missing_body.append(param_name)
    elif len(body_params) > 1:
        clean = {}
        for field in body_params:
            name = str(getattr(field, "name", None) or getattr(field, "alias", None) or "")
            alias = str(getattr(field, "alias", None) or name)
            present, value = _field_value(name, alias, field, source, text, agent_id)
            if not present and _required(field) and text is not None and _contains_type(_annotation(field), str):
                value = text
                present = True
            if present:
                clean[alias] = value
            elif _required(field):
                missing_body.append(name)
        body = clean

    return query, body, {
        "ok": not missing_query and not missing_body,
        "matched_path": route.path,
        "endpoint": getattr(route.endpoint, "__qualname__", repr(route.endpoint)),
        "query_params": [getattr(x, "name", None) for x in query_params],
        "body_params": [getattr(x, "name", None) for x in body_params],
        "missing_query": missing_query,
        "missing_body": missing_body,
    }


async def _send(agent_id: str, payload: Any) -> Response:
    query, body, debug = _build_contract(payload, agent_id)
    if not debug.get("ok"):
        return JSONResponse(status_code=422, content={"detail": "Could not satisfy the real backend message contract", "contract": debug})

    transport = httpx.ASGITransport(app=core_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://local-company-core") as client:
        kwargs: dict[str, Any] = {"params": query}
        if body is not None:
            kwargs["json"] = body
        response = await client.post(f"/api/agents/{agent_id}/messages", **kwargs)

    if response.status_code == 422:
        try:
            detail: Any = response.json()
        except Exception:
            detail = response.text
        return JSONResponse(status_code=422, content={"detail": detail, "contract": debug, "sent_query": query, "sent_body": body})

    blocked = {"content-length", "content-encoding", "transfer-encoding", "connection", "content-type"}
    headers = {k: v for k, v in response.headers.items() if k.lower() not in blocked}
    return Response(
        response.content,
        status_code=response.status_code,
        headers=headers,
        media_type=response.headers.get("content-type", "application/json").split(";", 1)[0],
    )


@app.post("/api/agents/{agent_id}/messages", include_in_schema=False)
async def send_agent_message(agent_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = (await request.body()).decode("utf-8", "replace")
    return await _send(agent_id, payload)


@app.post("/api/messages", include_in_schema=False)
async def send_generic_message(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    agent_id = _extract_agent(payload)
    if not agent_id:
        return JSONResponse(status_code=422, content={"detail": "Message destination missing"})
    return await _send(agent_id, payload)


@app.get("/api/compat/status", include_in_schema=False)
def compat_status():
    route = _real_message_route()
    query, body, contract = _build_contract({"content": "test"}, "00000000-0000-0000-0000-000000000000")
    return {
        "ok": route is not None,
        "message_route": getattr(route, "path", None),
        "message_endpoint": getattr(getattr(route, "endpoint", None), "__qualname__", None),
        "contract": contract,
        "test_query": query,
        "test_body": body,
        "ollama_native_fallback": True,
    }


# Everything not explicitly adapted above goes to the authoritative backend.
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
