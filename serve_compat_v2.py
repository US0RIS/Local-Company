#!/usr/bin/env python3
"""Final UI compatibility server.

This wraps serve_compat with an outer message-body middleware that:
- accepts both the real per-agent send route and the streamlined UI's generic
  POST /api/messages fallback;
- rewrites the generic route to the selected agent's real message endpoint; and
- normalizes the JSON body against the original FastAPI/Pydantic schema before
  validation.

The original backend remains authoritative for persistence, agent wakeups, model
work, permissions, and all write behavior.
"""
from __future__ import annotations

import json
import os
from typing import Any

import serve_compat
from starlette.requests import Request
from starlette.responses import JSONResponse

app = serve_compat.app

_GENERIC_MESSAGE_PATH = "/api/messages"
_AGENT_ID_KEYS = (
    "agent_id",
    "agentId",
    "employee_id",
    "employeeId",
    "recipient_id",
    "recipientId",
    "target_agent_id",
    "targetAgentId",
    "to_agent_id",
    "toAgentId",
    "recipient",
    "target",
    "to",
)


def _string_id(value: Any) -> str | None:
    """Extract an ID from the common shapes used by UI request payloads."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, dict):
        for key in ("id", "agent_id", "agentId", "employee_id", "employeeId", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _extract_agent_id(payload: dict[str, Any]) -> str | None:
    for key in _AGENT_ID_KEYS:
        if key in payload:
            candidate = _string_id(payload.get(key))
            if candidate:
                return candidate

    # Some clients nest destination metadata under a message/request object.
    for container_key in ("message", "request", "destination", "context", "meta"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            for key in _AGENT_ID_KEYS:
                if key in nested:
                    candidate = _string_id(nested.get(key))
                    if candidate:
                        return candidate
    return None


def _replace_cached_body(request: Request, body: bytes) -> None:
    # Starlette caches request.body(). Replacing both _body and _receive ensures
    # downstream middleware and FastAPI validation see the normalized bytes.
    request._body = body
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive


@app.middleware("http")
async def cached_message_body_adapter(request: Request, call_next):
    if request.method.upper() != "POST":
        return await call_next(request)
    if "application/json" not in request.headers.get("content-type", "").lower():
        return await call_next(request)

    original_path = request.url.path
    is_generic = original_path == _GENERIC_MESSAGE_PATH
    agent_match = serve_compat._AGENT_MESSAGE_RE.match(original_path)
    if not is_generic and not agent_match:
        return await call_next(request)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return await call_next(request)
    if not isinstance(payload, dict):
        return await call_next(request)

    if is_generic:
        agent_id = _extract_agent_id(payload)
        if not agent_id:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "Message destination missing: the UI did not include an agent/employee id.",
                    "received_keys": list(payload.keys()),
                    "expected_destination_keys": list(_AGENT_ID_KEYS),
                },
            )

        rewritten_path = f"/api/agents/{agent_id}/messages"
        request.scope["path"] = rewritten_path
        request.scope["raw_path"] = rewritten_path.encode("ascii", "ignore")
    else:
        agent_id = agent_match.group(1)

    normalized, debug = serve_compat._normalize_message_payload(payload, agent_id)
    new_body = json.dumps(normalized, ensure_ascii=False).encode("utf-8")
    _replace_cached_body(request, new_body)
    request.state.local_company_message_adapter = {
        **debug,
        "original_path": original_path,
        "routed_agent_id": agent_id,
        "rewritten_from_generic": is_generic,
    }

    return await call_next(request)


@app.get("/api/compat/send-status", include_in_schema=False)
def compat_send_status():
    route = serve_compat._agent_message_route()
    fields = serve_compat._body_model_fields(route)
    return {
        "ok": route is not None,
        "real_route": getattr(route, "path", None),
        "generic_alias": _GENERIC_MESSAGE_PATH,
        "body_fields": list(fields.keys()),
        "required_fields": [
            name for name, field in fields.items() if serve_compat._field_required(field)
        ],
        "destination_keys_accepted": list(_AGENT_ID_KEYS),
    }


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
