"""Claude (Anthropic subscription) usage provider.

Two data sources, newest wins:

1. Snapshot file written by the Claude Code statusline hook
   (~/.claude/usage-snapshot.json). Claude Code pipes rate-limit data to the
   statusline script after every assistant message, so this is fresh whenever
   Claude Code is actively used, and costs zero API calls.

2. The undocumented https://api.anthropic.com/api/oauth/usage endpoint, using
   the OAuth access token Claude Code stores in the macOS Keychain. This
   endpoint rate-limits per access token (~5 requests), so we cache results on
   disk, poll at most every 15 minutes, and back off for an hour after a 429.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

SNAPSHOT_PATH = os.path.expanduser("~/.claude/usage-snapshot.json")
CACHE_DIR = os.path.expanduser("~/.cache/usage-tracker")
OAUTH_CACHE_PATH = os.path.join(CACHE_DIR, "claude_oauth.json")

OAUTH_URL = "https://api.anthropic.com/api/oauth/usage"
MIN_POLL_INTERVAL_S = 15 * 60
BACKOFF_AFTER_429_S = 60 * 60

WINDOW_LABELS = {
    "five_hour": "5h window",
    "seven_day": "7d window",
    "seven_day_opus": "7d Opus",
    "seven_day_sonnet": "7d Sonnet",
    "seven_day_oauth_apps": "7d OAuth apps",
}


def _read_access_token():
    """Claude Code stores OAuth creds in the login keychain on macOS,
    or in ~/.claude/.credentials.json on other platforms."""
    raw = None
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        pass
    if not raw:
        cred_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.exists(cred_path):
            with open(cred_path) as f:
                raw = f.read()
    if not raw:
        return None, None
    try:
        creds = json.loads(raw)
        oauth = creds.get("claudeAiOauth", {})
        return oauth.get("accessToken"), oauth.get("subscriptionType")
    except (json.JSONDecodeError, AttributeError):
        return None, None


def _claude_code_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        return out.split()[0] if out else "2.1.90"
    except Exception:
        return "2.1.90"


def _epoch(value):
    """resets_at may arrive as epoch seconds or an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _windows_from_oauth(payload):
    windows = []
    for key, val in payload.items():
        if not isinstance(val, dict):
            continue
        pct = val.get("utilization", val.get("used_percentage"))
        if pct is None:
            continue
        windows.append({
            "key": key,
            "label": WINDOW_LABELS.get(key, key.replace("_", " ")),
            "used_percent": round(float(pct), 1),
            "resets_at": _epoch(val.get("resets_at")),
        })
    order = list(WINDOW_LABELS)
    windows.sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    return windows


def _read_snapshot():
    """Snapshot written by claude_statusline.py: {"ts": epoch, "rate_limits": {...}}"""
    try:
        with open(SNAPSHOT_PATH) as f:
            snap = json.load(f)
        limits = snap.get("rate_limits") or {}
        windows = []
        for key in ("five_hour", "seven_day"):
            val = limits.get(key)
            if isinstance(val, dict) and val.get("used_percentage") is not None:
                windows.append({
                    "key": key,
                    "label": WINDOW_LABELS[key],
                    "used_percent": round(float(val["used_percentage"]), 1),
                    "resets_at": _epoch(val.get("resets_at")),
                })
        if windows:
            return {"as_of": snap.get("ts", 0), "windows": windows, "source": "statusline snapshot"}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _load_oauth_cache():
    try:
        with open(OAUTH_CACHE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_oauth_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(OAUTH_CACHE_PATH, "w") as f:
        json.dump(cache, f)


def _try_oauth_endpoint(force=False):
    """Returns {"as_of", "windows", "source"} or None. Heavily throttled."""
    cache = _load_oauth_cache()
    now = time.time()

    fresh_enough = now - cache.get("last_attempt", 0) < MIN_POLL_INTERVAL_S
    backing_off = now - cache.get("last_429", 0) < BACKOFF_AFTER_429_S
    if not force and (fresh_enough or backing_off):
        return _cached_oauth_result(cache)

    token, _sub = _read_access_token()
    if not token:
        return _cached_oauth_result(cache)

    cache["last_attempt"] = now
    req = urllib.request.Request(OAUTH_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-code/{_claude_code_version()}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
        if "error" in payload:
            raise urllib.error.HTTPError(OAUTH_URL, 429, "rate limited", {}, None)
        cache["payload"] = payload
        cache["success_ts"] = now
        cache.pop("last_429", None)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            cache["last_429"] = now
    except Exception:
        pass
    _save_oauth_cache(cache)
    return _cached_oauth_result(cache)


def _cached_oauth_result(cache):
    payload = cache.get("payload")
    if not payload:
        return None
    windows = _windows_from_oauth(payload)
    if not windows:
        return None
    return {"as_of": cache.get("success_ts", 0), "windows": windows, "source": "oauth endpoint"}


def fetch(force=False):
    snapshot = _read_snapshot()
    oauth = _try_oauth_endpoint(force=force)

    candidates = [c for c in (snapshot, oauth) if c]
    if not candidates:
        _, subscription = _read_access_token()
        hint = ("No usage data yet. Use Claude Code once so the statusline hook "
                "records a snapshot, or wait for the OAuth endpoint backoff to clear.")
        return {"ok": False, "error": hint, "plan": subscription}

    best = max(candidates, key=lambda c: c["as_of"])
    _, subscription = _read_access_token()
    return {
        "ok": True,
        "plan": subscription,
        "as_of": best["as_of"],
        "source": best["source"],
        "windows": best["windows"],
    }
