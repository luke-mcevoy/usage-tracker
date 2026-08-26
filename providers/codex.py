"""Codex (ChatGPT subscription) usage provider.

Spawns `codex app-server` and asks it for account rate limits over JSON-RPC
(stdin/stdout). No token handling needed; the codex CLI uses its own auth
from ~/.codex/auth.json.
"""

import json
import subprocess
import threading

REQUEST_TIMEOUT_S = 20


def _window_label(minutes):
    if not minutes:
        return "limit"
    if minutes % 10080 == 0:
        return f"{minutes // 10080 * 7}d window"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d window"
    if minutes % 60 == 0:
        return f"{minutes // 60}h window"
    return f"{minutes}m window"


def _parse_window(value, key):
    if not isinstance(value, dict) or value.get("usedPercent") is None:
        return None
    return {
        "key": key,
        "label": _window_label(value.get("windowDurationMins")),
        "used_percent": round(float(value["usedPercent"]), 1),
        "resets_at": value.get("resetsAt"),
    }


def _parse_snapshot(snap):
    if not isinstance(snap, dict):
        return None
    windows = []
    for key in ("primary", "secondary"):
        w = _parse_window(snap.get(key), key)
        if w:
            windows.append(w)
    if not windows:
        return None
    credits = snap.get("credits") or {}
    return {
        "windows": windows,
        "plan": snap.get("planType"),
        "credits_balance": credits.get("balance"),
        "has_credits": credits.get("hasCredits", False),
    }


def _parse_result(result):
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        parsed = _parse_snapshot(by_id.get("codex"))
        if parsed:
            return parsed
    return _parse_snapshot(result.get("rateLimits"))


def fetch(force=False):
    del force  # always queried live; the server layer caches
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "usage-tracker", "version": "1"},
                    "capabilities": {"experimentalApi": True}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None},
    ]
    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "codex CLI not found on PATH"}

    timer = threading.Timer(REQUEST_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2:
                if "error" in msg:
                    return {"ok": False, "error": msg["error"].get("message", "rateLimits/read failed")}
                parsed = _parse_result(msg.get("result") or {})
                if not parsed:
                    return {"ok": False, "error": "no rate limit windows in codex response"}
                import time
                return {"ok": True, "as_of": int(time.time()),
                        "source": "codex app-server", **parsed}
        return {"ok": False, "error": "codex app-server closed without answering (timeout or not logged in)"}
    except BrokenPipeError:
        return {"ok": False, "error": "could not talk to codex app-server"}
    finally:
        timer.cancel()
        proc.kill()
