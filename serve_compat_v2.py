#!/usr/bin/env python3
"""Final UI compatibility server.

This wraps serve_compat with an outer message-body middleware that replaces
Starlette's cached request body before the inner compatibility layer and the
original FastAPI route validate it.
"""
from __future__ import annotations

import json
import os

import serve_compat
from starlette.requests import Request

app = serve_compat.app


@app.middleware("http")
async def cached_message_body_adapter(request: Request, call_next):
    match = serve_compat._AGENT_MESSAGE_RE.match(request.url.path)
    if request.method.upper() != "POST" or not match:
        return await call_next(request)
    if "application/json" not in request.headers.get("content-type", "").lower():
        return await call_next(request)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return await call_next(request)
    if not isinstance(payload, dict):
        return await call_next(request)

    normalized, debug = serve_compat._normalize_message_payload(payload, match.group(1))
    if normalized != payload:
        new_body = json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        # _CachedRequest.wrapped_receive prefers _body once body() has been read,
        # so replacing only _receive is insufficient.
        request._body = new_body
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": new_body, "more_body": False}

        request._receive = receive
        request.state.local_company_message_adapter = debug

    return await call_next(request)


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
