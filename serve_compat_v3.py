#!/usr/bin/env python3
"""Local Company compatibility server v3.

An outer FastAPI router intercepts UI message sends before the original backend,
and exposes exact runtime diagnostics for the original message handler.
"""
from __future__ import annotations

import enum
import inspect
import os
from typing import Any, get_args, get_origin

import httpx
import ollama_compat  # noqa: F401
import serve_compat
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse, Response

core_app = serve_compat.app
app = FastAPI(title="Local Company compatibility router", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_TEXT_KEYS = ("content", "text", "message", "message_text", "prompt", "body", "input", "value")
_AGENT_KEYS = (
    "agent_id", "agentId", "employee_id", "employeeId", "recipient_id", "recipientId",
    "target_agent_id", "targetAgentId", "to_agent_id", "toAgentId", "recipient", "target", "to",
)
_HUMAN_KEYS = ("sender", "sender_type", "author", "author_type", "role", "source")


def _real_message_route() -> APIRoute | None:
    for route in core_app.routes:
        if isinstance(route, APIRoute) and route.path == "/api/agents/{agent_id}/messages" and "POST" in (route.methods or set()):
            return route
    return None


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = _extract_text(value)
            if nested:
                return nested
    return None


def _string_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("id", "agent_id", "agentId", "employee_id", "employeeId", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _extract_agent_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in _AGENT_KEYS:
        candidate = _string_id(payload.get(key))
        if candidate:
            return candidate
    for key in ("message", "request", "destination", "context", "meta"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidate = _extract_agent_id(nested)
            if candidate:
                return candidate
    return None


def _required(field: Any) -> bool:
    checker = getattr(field, "is_required", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    value = getattr(field, "required", None)
    return bool(value) if value is not None else False


def _annotation(field: Any) -> Any:
    value = getattr(field, "annotation", None)
    if value is not None:
        return value
    return getattr(field, "outer_type_", None) or getattr(field, "type_", None)


def _fields(model: Any) -> dict[str, Any]:
    if model is None:
        return {}
    value = getattr(model, "model_fields", None)
    if isinstance(value, dict) and value:
        return value
    value = getattr(model, "__fields__", None)
    return value if isinstance(value, dict) else {}


def _alias(field: Any, name: str) -> str:
    for attr in ("validation_alias", "alias"):
        value = getattr(field, attr, None)
        if isinstance(value, str) and value:
            return value
    return name


def _is_type(annotation: Any, target: type) -> bool:
    if annotation is target:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(_is_type(arg, target) for arg in get_args(annotation))


def _human_value(annotation: Any) -> Any | None:
    if _is_type(annotation, str):
        return "human"
    origin = get_origin(annotation)
    if origin is not None:
        for wanted in ("human", "user", "owner"):
            for value in get_args(annotation):
                if str(value).lower() == wanted:
                    return value
        for value in get_args(annotation):
            found = _human_value(value)
            if found is not None:
                return found
        return None
    try:
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            for wanted in ("human", "user", "owner"):
                for member in annotation:
                    if member.name.lower() == wanted or str(member.value).lower() == wanted:
                        return member.value
    except Exception:
        pass
    return None


def _descriptor() -> dict[str, Any]:
    route = _real_message_route()
    if route is None:
        return {"route": None, "kind": "missing", "fields": {}, "body_param": None, "annotation": None}

    body_params = list(getattr(route.dependant, "body_params", []) or [])
    candidates: list[Any] = []
    if route.body_field is not None:
        candidates.extend([
            getattr(route.body_field, "type_", None),
            getattr(getattr(route.body_field, "field_info", None), "annotation", None),
        ])
    for param in body_params:
        candidates.extend([
            getattr(param, "type_", None),
            getattr(param, "annotation", None),
            getattr(getattr(param, "field_info", None), "annotation", None),
        ])

    for candidate in candidates:
        model_fields = _fields(candidate)
        if model_fields:
            return {"route": route, "kind": "model", "fields": model_fields, "body_param": None, "annotation": candidate}

    if len(body_params) == 1:
        param = body_params[0]
        annotation = getattr(param, "type_", None) or getattr(param, "annotation", None) or getattr(getattr(param, "field_info", None), "annotation", None)
        return {"route": route, "kind": "scalar", "fields": {}, "body_param": getattr(param, "name", None), "annotation": annotation}

    return {"route": route, "kind": "unknown", "fields": {}, "body_param": None, "annotation": None}


def _normalize(payload: Any, agent_id: str) -> tuple[Any, dict[str, Any]]:
    desc = _descriptor()
    text = _extract_text(payload)

    if desc["kind"] == "model":
        source = payload if isinstance(payload, dict) else {}
        clean: dict[str, Any] = {}
        unresolved: list[str] = []
        agent_tokens = {key.lower() for key in _AGENT_KEYS}

        for name, field in desc["fields"].items():
            alias = _alias(field, name)
            ann = _annotation(field)
            lower = name.lower()
            if alias in source:
                clean[alias] = source[alias]
            elif name in source:
                clean[alias] = source[name]
            elif lower in _TEXT_KEYS and text is not None:
                clean[alias] = text
            elif lower in agent_tokens:
                clean[alias] = agent_id
            elif lower in _HUMAN_KEYS:
                value = _human_value(ann)
                if value is not None:
                    clean[alias] = value
            elif _required(field):
                if text is not None and _is_type(ann, str):
                    clean[alias] = text
                elif _is_type(ann, bool):
                    clean[alias] = False
                else:
                    unresolved.append(name)

        if text is not None and len(unresolved) == 1:
            name = unresolved[0]
            clean[_alias(desc["fields"][name], name)] = text
            unresolved = []

        return clean, {
            "kind": "model",
            "fields": list(desc["fields"].keys()),
            "required": [name for name, field in desc["fields"].items() if _required(field)],
            "sent_keys": list(clean.keys()),
            "unresolved_required": unresolved,
        }

    if desc["kind"] == "scalar":
        ann = desc["annotation"]
        param = desc["body_param"]
        if _is_type(ann, str):
            return text or "", {"kind": "scalar", "body_param": param, "annotation": "str"}
        if isinstance(payload, dict) and param and param in payload:
            return payload[param], {"kind": "scalar", "body_param": param, "annotation": str(ann)}
        return payload, {"kind": "scalar", "body_param": param, "annotation": str(ann)}

    return payload, {"kind": desc["kind"]}


async def _forward(agent_id: str, payload: Any) -> Response:
    normalized, debug = _normalize(payload, agent_id)
    transport = httpx.ASGITransport(app=core_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://local-company-core") as client:
        core_response = await client.post(f"/api/agents/{agent_id}/messages", json=normalized)

    if core_response.status_code == 422:
        try:
            validation = core_response.json()
        except Exception:
            validation = core_response.text
        return JSONResponse(status_code=422, content={"detail": validation, "adapter": debug})

    blocked = {"content-length", "content-encoding", "transfer-encoding", "connection", "content-type"}
    headers = {k: v for k, v in core_response.headers.items() if k.lower() not in blocked}
    return Response(
        content=core_response.content,
        status_code=core_response.status_code,
        headers=headers,
        media_type=core_response.headers.get("content-type", "application/json").split(";", 1)[0],
    )


@app.post("/api/agents/{agent_id}/messages", include_in_schema=False)
async def send_agent_message(agent_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = (await request.body()).decode("utf-8", "replace")
    return await _forward(agent_id, payload)


@app.post("/api/messages", include_in_schema=False)
async def send_generic_message(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    agent_id = _extract_agent_id(payload)
    if not agent_id:
        return JSONResponse(status_code=422, content={"detail": "Message destination missing"})
    return await _forward(agent_id, payload)


def _param_dump(items: list[Any]) -> list[dict[str, Any]]:
    out = []
    for p in items or []:
        out.append({
            "name": getattr(p, "name", None),
            "alias": getattr(p, "alias", None),
            "required": bool(getattr(p, "required", False)),
            "type": str(getattr(p, "type_", None)),
            "annotation": str(getattr(p, "annotation", None) or getattr(getattr(p, "field_info", None), "annotation", None)),
            "default": repr(getattr(p, "default", None)),
        })
    return out


@app.get("/api/compat/message-schema", include_in_schema=False)
def message_schema():
    desc = _descriptor()
    return {
        "ok": desc["route"] is not None,
        "kind": desc["kind"],
        "body_param": desc["body_param"],
        "annotation": str(desc["annotation"]),
        "fields": list(desc["fields"].keys()),
        "required": [name for name, field in desc["fields"].items() if _required(field)],
        "ollama_native_fallback": True,
    }


@app.get("/api/compat/message-debug", include_in_schema=False)
def message_debug():
    route = _real_message_route()
    if route is None:
        return {"ok": False, "error": "route not found"}
    endpoint = route.endpoint
    try:
        source = inspect.getsource(endpoint)
    except Exception as exc:
        source = f"<source unavailable: {exc}>"
    dep = route.dependant
    dependencies = []
    for child in getattr(dep, "dependencies", []) or []:
        call = getattr(child, "call", None)
        try:
            sig = str(inspect.signature(call)) if call else None
        except Exception:
            sig = None
        dependencies.append({
            "name": getattr(child, "name", None),
            "call": getattr(call, "__qualname__", repr(call)),
            "module": getattr(call, "__module__", None),
            "signature": sig,
        })
    return {
        "ok": True,
        "path": route.path,
        "methods": sorted(route.methods or []),
        "endpoint": getattr(endpoint, "__qualname__", repr(endpoint)),
        "module": getattr(endpoint, "__module__", None),
        "signature": str(inspect.signature(endpoint)),
        "source": source,
        "path_params": _param_dump(getattr(dep, "path_params", []) or []),
        "query_params": _param_dump(getattr(dep, "query_params", []) or []),
        "body_params": _param_dump(getattr(dep, "body_params", []) or []),
        "header_params": _param_dump(getattr(dep, "header_params", []) or []),
        "cookie_params": _param_dump(getattr(dep, "cookie_params", []) or []),
        "dependencies": dependencies,
    }


app.mount("/", core_app)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LOCAL_COMPANY_BACKEND_PORT", "8000")), reload=False, workers=1, log_level="info")


if __name__ == "__main__":
    main()
