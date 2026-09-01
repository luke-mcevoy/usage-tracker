"""Resolve API keys for HTTP backends. Never log or return the secret itself."""

import json
import os
import subprocess


def read_key(backend):
    """Return the API key string for a backend config dict, or None.

    Order: explicit env var, then (for Gemini) GOOGLE_API_KEY, then a macOS
    Keychain service name if ``api_key_keychain`` is set. Keychain values may
    be a raw key or a JSON object with a ``token`` / ``apiKey`` field (Gemini
    CLI stores the latter).
    """
    env_name = backend.get("api_key_env")
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return _unwrap_secret(value.strip())
    if backend.get("kind") == "gemini":
        value = os.environ.get("GOOGLE_API_KEY")
        if value:
            return _unwrap_secret(value.strip())
    service = backend.get("api_key_keychain")
    if service:
        value = _keychain_password(service)
        if value:
            return _unwrap_secret(value)
    return None


def _unwrap_secret(raw):
    """Gemini CLI wraps the key as JSON; the token field may itself be an object."""
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            return raw
        found = _extract_token(data)
        if found:
            return found
    return raw


def _extract_token(data):
    if isinstance(data, str) and data.strip():
        return data.strip()
    if not isinstance(data, dict):
        return None
    for field in ("token", "apiKey", "api_key", "key", "accessToken", "access_token"):
        found = _extract_token(data.get(field))
        if found:
            return found
    return None


def _keychain_password(service):
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None
