#!/usr/bin/env python3
"""Compatibility fallback for Ollama installs without /v1/chat/completions.

The original runtime uses Ollama's OpenAI-compatible endpoint. Some local Ollama
installs expose /api/chat but return 404 for /v1/chat/completions. This patch
recognizes both absolute and base_url-relative httpx requests, retries through
/api/chat, and translates the native response back to the OpenAI-shaped JSON the
runtime already expects.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

_INSTALLED = False
_ORIG_ASYNC_REQUEST = httpx.AsyncClient.request
_ORIG_SYNC_REQUEST = httpx.Client.request


def _resolved_url(client: Any, url: object) -> httpx.URL | None:
    try:
        candidate = httpx.URL(str(url))
        if candidate.is_absolute_url:
            return candidate
        base = getattr(client, "base_url", None)
        if base:
            return httpx.URL(base).join(candidate)
        return candidate
    except Exception:
        return None


def _is_openai_ollama_url(url: httpx.URL | None) -> bool:
    if url is None:
        return False
    try:
        return (
            url.host in {"127.0.0.1", "localhost"}
            and (url.port or (443 if url.scheme == "https" else 80)) == 11434
            and url.path.rstrip("/") == "/v1/chat/completions"
        )
    except Exception:
        return False


def _native_url(url: httpx.URL) -> httpx.URL:
    return url.copy_with(path="/api/chat", query=None, fragment=None)


def _extract_payload(kwargs: dict) -> dict | None:
    payload = kwargs.get("json")
    if isinstance(payload, dict):
        return dict(payload)
    content = kwargs.get("content")
    if isinstance(content, (bytes, bytearray)):
        try:
            parsed = json.loads(bytes(content).decode("utf-8"))
            return dict(parsed) if isinstance(parsed, dict) else None
        except Exception:
            return None
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return dict(parsed) if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _native_payload(openai_payload: dict) -> dict:
    native: dict = {
        "model": openai_payload.get("model"),
        "messages": openai_payload.get("messages") or [],
        "stream": False,
    }

    response_format = openai_payload.get("response_format")
    if isinstance(response_format, dict):
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            native["format"] = "json"
        elif fmt_type == "json_schema" and isinstance(response_format.get("json_schema"), dict):
            schema = response_format["json_schema"].get("schema")
            if schema:
                native["format"] = schema

    options: dict = {}
    for source, target in {
        "temperature": "temperature",
        "top_p": "top_p",
        "seed": "seed",
        "max_tokens": "num_predict",
    }.items():
        if source in openai_payload and openai_payload[source] is not None:
            options[target] = openai_payload[source]
    if options:
        native["options"] = options

    return native


def _openai_response(native_response: httpx.Response, model_hint: object = None) -> httpx.Response:
    data = native_response.json()
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        message = {"role": "assistant", "content": ""}
    content = message.get("content", "")
    if not isinstance(content, str):
        content = str(content)

    translated_message = {"role": message.get("role") or "assistant", "content": content}
    if isinstance(message.get("thinking"), str) and message.get("thinking"):
        translated_message["thinking"] = message["thinking"]

    prompt_eval_count = data.get("prompt_eval_count", 0) if isinstance(data, dict) else 0
    eval_count = data.get("eval_count", 0) if isinstance(data, dict) else 0
    model = (data.get("model") if isinstance(data, dict) else None) or model_hint or "ollama"
    translated = {
        "id": "ollama-native-fallback",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": translated_message,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": int(prompt_eval_count or 0),
            "completion_tokens": int(eval_count or 0),
            "total_tokens": int((prompt_eval_count or 0) + (eval_count or 0)),
        },
    }
    return httpx.Response(
        200,
        json=translated,
        request=native_response.request,
        headers={"x-local-company-ollama-fallback": "native-api-chat"},
    )


async def _async_request(self, method, url, *args, **kwargs):
    resolved = _resolved_url(self, url)
    response = await _ORIG_ASYNC_REQUEST(self, method, url, *args, **kwargs)
    if response.status_code != 404 or not _is_openai_ollama_url(resolved):
        return response

    payload = _extract_payload(kwargs)
    if not payload or payload.get("stream") is True:
        return response

    retry_kwargs = dict(kwargs)
    retry_kwargs.pop("content", None)
    retry_kwargs["json"] = _native_payload(payload)
    native = await _ORIG_ASYNC_REQUEST(self, method, _native_url(resolved), *args, **retry_kwargs)
    if native.status_code >= 400:
        return native
    try:
        return _openai_response(native, payload.get("model"))
    except Exception:
        return native


def _sync_request(self, method, url, *args, **kwargs):
    resolved = _resolved_url(self, url)
    response = _ORIG_SYNC_REQUEST(self, method, url, *args, **kwargs)
    if response.status_code != 404 or not _is_openai_ollama_url(resolved):
        return response

    payload = _extract_payload(kwargs)
    if not payload or payload.get("stream") is True:
        return response

    retry_kwargs = dict(kwargs)
    retry_kwargs.pop("content", None)
    retry_kwargs["json"] = _native_payload(payload)
    native = _ORIG_SYNC_REQUEST(self, method, _native_url(resolved), *args, **retry_kwargs)
    if native.status_code >= 400:
        return native
    try:
        return _openai_response(native, payload.get("model"))
    except Exception:
        return native


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    httpx.AsyncClient.request = _async_request
    httpx.Client.request = _sync_request
    _INSTALLED = True
    print("Local Company: Ollama native /api/chat fallback enabled")


install()
