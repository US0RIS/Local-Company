#!/usr/bin/env python3
"""Strict compatibility server for the streamlined Local Company UI.

The original FastAPI backend remains authoritative.  This outer layer adapts
only UI message requests.  Unlike the older adapter, it builds a clean JSON body
containing only fields accepted by the original Pydantic/FastAPI body schema, so
models configured with extra='forbid' do not reject Grok-shell metadata.
"""
from __future__ import annotations

import enum
import json
import os
from typing import Any, get_args, get_origin

import serve_compat
import serve_compat_v2
from starlette.requests import Request
from starlette.responses import JSONResponse

app = serve_compat_v2.app


def _field_alias(field: Any, fallback: str) -> str:
    alias = getattr(field, "alias", None)
    if isinstance(alias, str) and alias:
        return alias
    serialization_alias = getattr(field, "serialization_alias", None)
    if isinstance(serialization_alias, str) and serialization_alias:
        return serialization_alias
    return fallback


def _schema_fields() -> dict[str, dict[str, Any]]:
    """Return the actual accepted body fields, including scalar body params."""
    route = serve_compat._agent_message_route()
    if route is None:
        return {}

    result: dict[str, dict[str, Any]] = {}
    model_fields = serve_compat._body_model_fields(route)
    if model_fields:
        for name, field in model_fields.items():
            result[name] = {
                "field": field,
                "alias": _field_alias(field, name),
                "required": serve_compat._field_required(field),
                "annotation": serve_compat._field_annotation(field),
            }
        return result

    # FastAPI also supports endpoints declared with scalar/body parameters rather
    # than a user-defined Pydantic model.  In that case there may be no model
    # fields to introspect, but dependant.body_params is authoritative.
    for param in getattr(route.dependant, "body_params", []) or []:
        name = getattr(param, "name", None) or getattr(param, "alias", None)
        if not name:
            continue
        alias = getattr(param, "alias", None) or name
        required = bool(getattr(param, "required", False))
        annotation = getattr(param, "type_", None)
        if annotation is None:
            annotation = getattr(getattr(param, "field_info", None), "annotation", None)
        result[str(name)] = {
            "field": param,
            "alias": str(alias),
            "required": required,
            "annotation": annotation,
        }
    return result


def _is_string_annotation(annotation: Any) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is not None:
        return any(_is_string_annotation(arg) for arg in get_args(annotation))
    return False


def _human_value(annotation: Any) -> Any:
    value = serve_compat._enum_or_literal_human_value(annotation)
    if value is not None:
        return value
    if _is_string_annotation(annotation):
        return "human"
    try:
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            members = list(annotation)
            if members:
                return members[0].value
    except Exception:
        pass
    return None


def _candidate_value(payload: dict[str, Any], name: str, alias: str) -> tuple[bool, Any]:
    for key in (alias, name):
        if key in payload:
            return True, payload[key]
    return False, None


def _clean_payload(payload: dict[str, Any], agent_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = _schema_fields()
    text = serve_compat._extract_text(payload)
    clean: dict[str, Any] = {}

    if not fields:
        # Do not guess silently.  Returning diagnostics is better than another
        # opaque FastAPI 422.
        return {}, {
            "ok": False,
            "error": "Could not introspect the original message body schema",
            "received_keys": list(payload.keys()),
        }

    for name, meta in fields.items():
        alias = str(meta["alias"])
        present, value = _candidate_value(payload, name, alias)
        if present:
            clean[alias] = value
            continue

        lower_name = name.lower()
        lower_alias = alias.lower()
        key_tokens = {lower_name, lower_alias}

        if text is not None and any(token in serve_compat._TEXT_KEYS for token in key_tokens):
            clean[alias] = text
            continue
        if any(token in serve_compat._AGENT_KEYS for token in key_tokens):
            clean[alias] = agent_id
            continue
        if any(token in serve_compat._HUMAN_KEYS for token in key_tokens):
            human = _human_value(meta["annotation"])
            if human is not None:
                clean[alias] = human
                continue

    required = [name for name, meta in fields.items() if meta["required"]]
    missing = [
        name
        for name in required
        if str(fields[name]["alias"]) not in clean
    ]

    # Common FastAPI shape: exactly one required body field whose name is not one
    # of our known text tokens.  If the UI supplied text, that field is the DM.
    if text is not None and len(missing) == 1:
        name = missing[0]
        clean[str(fields[name]["alias"])] = text
        missing = []

    return clean, {
        "ok": not missing,
        "schema_fields": {
            name: {
                "alias": meta["alias"],
                "required": meta["required"],
                "annotation": str(meta["annotation"]),
            }
            for name, meta in fields.items()
        },
        "required_fields": required,
        "missing_required": missing,
        "received_keys": list(payload.keys()),
        "sent_keys": list(clean.keys()),
    }


def _replace_body(request: Request, body: bytes) -> None:
    request._body = body
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive

    # Keep Content-Length consistent for downstream consumers that inspect it.
    headers = []
    for key, value in request.scope.get("headers", []):
        if key.lower() != b"content-length":
            headers.append((key, value))
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    request.scope["headers"] = headers


@app.middleware("http")
async def strict_message_adapter(request: Request, call_next):
    if request.method.upper() != "POST":
        return await call_next(request)
    if "application/json" not in request.headers.get("content-type", "").lower():
        return await call_next(request)

    original_path = request.url.path
    generic = original_path == "/api/messages"
    match = serve_compat._AGENT_MESSAGE_RE.match(original_path)
    if not generic and not match:
        return await call_next(request)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Message body must be valid JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=422, content={"detail": "Message body must be a JSON object"})

    if generic:
        agent_id = serve_compat_v2._extract_agent_id(payload)
        if not agent_id:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "Message destination missing",
                    "received_keys": list(payload.keys()),
                    "accepted_destination_keys": list(serve_compat_v2._AGENT_ID_KEYS),
                },
            )
        rewritten = f"/api/agents/{agent_id}/messages"
        request.scope["path"] = rewritten
        request.scope["raw_path"] = rewritten.encode("ascii", "ignore")
    else:
        agent_id = match.group(1)

    clean, debug = _clean_payload(payload, agent_id)
    if not debug.get("ok"):
        return JSONResponse(status_code=422, content={"detail": "Message adapter could not satisfy backend schema", **debug})

    new_body = json.dumps(clean, ensure_ascii=False).encode("utf-8")
    _replace_body(request, new_body)
    request.state.local_company_strict_message_adapter = debug
    return await call_next(request)


@app.get("/api/compat/message-schema-v3", include_in_schema=False)
def message_schema_v3():
    fields = _schema_fields()
    return {
        "ok": bool(fields),
        "fields": {
            name: {
                "alias": meta["alias"],
                "required": meta["required"],
                "annotation": str(meta["annotation"]),
            }
            for name, meta in fields.items()
        },
    }


def main() -> None:
    import uvicorn
    import ollama_compat

    ollama_compat.install()
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
