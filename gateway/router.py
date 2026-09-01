"""Pick an HTTP backend for a chat-completions request, with fallback.

Reuses ``choose_agent`` so the gateway and the ``ai`` CLI share one quota
policy. Chat completions only go to HTTP backends — coding CLIs stay on
``ai``. A 429/5xx from a backend walks the rest of the chain.
"""

from __future__ import annotations

import json
import time
import uuid

from dispatcher.routing import choose_agent
from gateway.backends import BackendError, complete as backend_complete
from gateway.keys import read_key


def parse_model(model):
    """``auto`` / ``gemini`` / ``gemini/gemini-3.6-flash`` → (backend_id | None, override)."""
    raw = (model or "auto").strip() or "auto"
    if raw == "auto":
        return None, None
    if "/" in raw:
        backend_id, override = raw.split("/", 1)
        return backend_id, override or None
    return raw, None


def available_backends(config):
    """Backends from config that we can actually call (known kind + a key, if required)."""
    gateway = (config or {}).get("gateway") or {}
    out = []
    for backend in gateway.get("backends") or []:
        if not isinstance(backend, dict) or not backend.get("id"):
            continue
        if backend.get("kind") not in ("gemini", "openai"):
            continue
        if backend.get("kind") == "openai" and not backend.get("base_url"):
            continue
        # openai-compat with empty api_key_env is allowed (local servers)
        needs_key = backend.get("kind") == "gemini" or backend.get("api_key_env") or backend.get("api_key_keychain")
        if needs_key and not read_key(backend):
            continue
        out.append(backend)
    return out


def _quota_installed(backends):
    """Map quota-service name → True for choose_agent."""
    installed = {}
    for backend in backends:
        quota = backend.get("quota")
        if quota:
            installed[quota] = True
        installed[backend["id"]] = True
    return installed


def build_chain(model, backends, services, config):
    """Ordered list of backends to try for this request."""
    pin_id, _override = parse_model(model)
    by_id = {b["id"]: b for b in backends}
    if pin_id:
        if pin_id not in by_id:
            return [], f"unknown model '{pin_id}' — known: {', '.join(by_id) or '(none)'}"
        return [by_id[pin_id]], f"pinned to {pin_id}"

    if not backends:
        return [], "no HTTP backends configured (or their API keys are missing)"

    # Quota-aware pick among backends that declare a quota service.
    quota_backends = [b for b in backends if b.get("quota")]
    installed = _quota_installed(quota_backends)
    decision = choose_agent(services, config, installed)
    chain = []
    seen = set()
    if decision.agent:
        for backend in quota_backends:
            if backend.get("quota") == decision.agent and backend["id"] not in seen:
                chain.append(backend)
                seen.add(backend["id"])
        reason = decision.reason
    else:
        reason = decision.reason

    # Then remaining quota backends in config order (fallback on 429).
    for backend in quota_backends:
        if backend["id"] not in seen:
            chain.append(backend)
            seen.add(backend["id"])

    # Then pay-as-you-go / unmetered HTTP backends (no quota mapping).
    for backend in backends:
        if not backend.get("quota") and backend["id"] not in seen:
            chain.append(backend)
            seen.add(backend["id"])

    if not chain:
        return [], reason or "no HTTP backend available"
    return chain, reason


def openai_completion(result, requested_model, backend_id):
    created = int(time.time())
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": created,
        "model": result.get("model") or requested_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.get("content") or ""},
            "finish_reason": "stop",
        }],
        "usage": result.get("usage") or {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        },
        "system_fingerprint": f"usage-tracker:{backend_id}",
    }


def chat_completions(body, services, config, complete_fn=None):
    """Run one OpenAI-style chat completion. Returns (status, payload)."""
    complete_fn = complete_fn or backend_complete
    backends = available_backends(config)
    model = body.get("model") or "auto"
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return 400, {"error": {"message": "messages is required", "type": "invalid_request_error"}}

    chain, reason = build_chain(model, backends, services, config)
    if not chain:
        return 503, {"error": {"message": reason, "type": "router_error"}}

    _pin_id, model_override = parse_model(model)
    extras = {}
    if "temperature" in body:
        extras["temperature"] = body["temperature"]
    if "max_tokens" in body:
        extras["max_tokens"] = body["max_tokens"]

    errors = []
    for backend in chain:
        api_key = read_key(backend) or ""
        use_model = model_override or backend.get("model")
        try:
            result = complete_fn(backend, messages, use_model, api_key, extras=extras)
        except BackendError as exc:
            errors.append(f"{backend['id']}: {exc.status} {exc.message[:120]}")
            if exc.retryable and backend is not chain[-1]:
                continue
            status = exc.status if exc.status >= 400 else 502
            return status, {
                "error": {
                    "message": f"{backend['id']} failed: {exc.message[:300]}",
                    "type": "backend_error",
                    "tried": errors,
                    "router": reason,
                }
            }
        payload = openai_completion(result, use_model, backend["id"])
        payload["router"] = {
            "backend": backend["id"],
            "reason": reason,
            "fallback_from": errors or None,
        }
        return 200, payload

    return 503, {"error": {"message": "all backends failed", "tried": errors, "type": "router_error"}}


def sse_chunks(payload):
    """One-shot OpenAI SSE stream from a finished completion (no token trickle yet)."""
    content = payload["choices"][0]["message"]["content"]
    base = {
        "id": payload["id"],
        "object": "chat.completion.chunk",
        "created": payload["created"],
        "model": payload["model"],
    }
    first = dict(base, choices=[{
        "index": 0,
        "delta": {"role": "assistant", "content": content},
        "finish_reason": None,
    }])
    last = dict(base, choices=[{
        "index": 0, "delta": {}, "finish_reason": "stop",
    }])
    return (
        f"data: {json.dumps(first)}\n\n"
        f"data: {json.dumps(last)}\n\n"
        "data: [DONE]\n\n"
    )


def list_models(config):
    backends = available_backends(config)
    created = int(time.time())
    models = [{
        "id": "auto",
        "object": "model",
        "created": created,
        "owned_by": "usage-tracker",
    }]
    for backend in backends:
        models.append({
            "id": backend["id"],
            "object": "model",
            "created": created,
            "owned_by": backend.get("kind") or "usage-tracker",
        })
        if backend.get("model") and backend["model"] != backend["id"]:
            models.append({
                "id": f"{backend['id']}/{backend['model']}",
                "object": "model",
                "created": created,
                "owned_by": backend.get("kind") or "usage-tracker",
            })
    return {"object": "list", "data": models}
