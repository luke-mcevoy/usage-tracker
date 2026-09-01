"""HTTP backends for the OpenAI-compatible gateway.

Two kinds, both stdlib urllib:

- ``gemini`` — Google Generative Language API (native generateContent)
- ``openai`` — any OpenAI-compatible ``/chat/completions`` endpoint
  (OpenAI, Groq, Together, local vLLM, Ollama's OpenAI shim, …)

A new vendor is a config dict, not a new module.
"""

import json
import urllib.error
import urllib.request

GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class BackendError(Exception):
    """A backend call failed. ``retryable`` means the router should try the next one."""

    def __init__(self, status, message, retryable=False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retryable = retryable


def messages_to_gemini(messages):
    """OpenAI chat messages → Gemini ``contents`` + optional ``systemInstruction``."""
    system_parts = []
    contents = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        text = _content_text(msg.get("content"))
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"][0]["text"] += "\n" + text
        else:
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
    body = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return body


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
        return "".join(parts)
    return "" if content is None else str(content)


def gemini_to_openai_message(data):
    """Pull the first candidate's text out of a generateContent response."""
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates:
        return ""
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    return "".join(p.get("text") or "" for p in parts if isinstance(p, dict))


def _http_json(url, payload, headers, timeout=60):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        retryable = exc.code in (408, 409, 429, 500, 502, 503, 504)
        raise BackendError(exc.code, err_body or exc.reason, retryable=retryable) from exc
    except urllib.error.URLError as exc:
        raise BackendError(502, str(exc.reason or exc), retryable=True) from exc
    try:
        return json.loads(raw.decode("utf-8")), status
    except ValueError as exc:
        raise BackendError(502, "backend returned non-JSON", retryable=True) from exc


def complete_gemini(backend, messages, model, api_key, extras=None):
    extras = extras or {}
    body = messages_to_gemini(messages)
    gen = {}
    if extras.get("temperature") is not None:
        gen["temperature"] = extras["temperature"]
    if extras.get("max_tokens") is not None:
        gen["maxOutputTokens"] = extras["max_tokens"]
    if gen:
        body["generationConfig"] = gen
    url = GEMINI_GENERATE_URL.format(model=model)
    data, _status = _http_json(url, body, {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    })
    text = gemini_to_openai_message(data)
    usage = data.get("usageMetadata") or {}
    return {
        "content": text,
        "model": model,
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount") or 0,
            "completion_tokens": usage.get("candidatesTokenCount") or 0,
            "total_tokens": usage.get("totalTokenCount") or 0,
        },
    }


def complete_openai(backend, messages, model, api_key, extras=None):
    extras = extras or {}
    base = (backend.get("base_url") or "").rstrip("/")
    if not base:
        raise BackendError(500, "openai backend missing base_url", retryable=False)
    payload = {"model": model, "messages": messages}
    if extras.get("temperature") is not None:
        payload["temperature"] = extras["temperature"]
    if extras.get("max_tokens") is not None:
        payload["max_tokens"] = extras["max_tokens"]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data, _status = _http_json(base + "/chat/completions", payload, headers)
    choices = data.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    usage = data.get("usage") or {}
    return {
        "content": message.get("content") or "",
        "model": data.get("model") or model,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
            "total_tokens": usage.get("total_tokens") or 0,
        },
    }


COMPLETERS = {
    "gemini": complete_gemini,
    "openai": complete_openai,
}


def complete(backend, messages, model, api_key, extras=None):
    kind = backend.get("kind")
    completer = COMPLETERS.get(kind)
    if completer is None:
        raise BackendError(400, f"unknown backend kind: {kind}", retryable=False)
    return completer(backend, messages, model, api_key, extras=extras)
