"""Gemini CLI usage provider.

Google only exposes a quota API (Code Assist ``retrieveUserQuota``) for
Google-account login. With API-key auth there is no quota endpoint at all, so
we estimate: count today's prompts in the Gemini CLI's local session logs
(~/.gemini/tmp/*/logs.json) against the documented daily request limit for
the configured auth type. The result is clearly labeled an estimate — the
real number of API calls is somewhat higher because one prompt can trigger
several model requests.

Documented daily limits (gemini-cli docs, resources/quota-and-pricing.md):
    API key free tier          250 requests/day
    Google login (individual)  1000 requests/day
Override with the GEMINI_DAILY_LIMIT environment variable if on a paid tier.
"""

import json
import os
import time
from datetime import datetime, timedelta

GEMINI_DIR = os.path.expanduser("~/.gemini")
DISPATCH_LOG = os.path.expanduser("~/.cache/usage-tracker/dispatches.jsonl")

DAILY_LIMITS = {
    "gemini-api-key": 250,
    "oauth-personal": 1000,
}


def _auth_type():
    try:
        with open(os.path.join(GEMINI_DIR, "settings.json")) as f:
            settings = json.load(f)
        return ((settings.get("security") or {}).get("auth") or {}).get("selectedType")
    except (OSError, json.JSONDecodeError):
        return None


def _headless_dispatches_today():
    """Headless `ai` dispatches to gemini today.

    Headless (-p) gemini sessions do not write logs.json, so the session-log
    count misses exactly the runs the dispatcher makes. Interactive
    dispatches DO land in logs.json, so only headless ones are added here.
    """
    today = datetime.now().date()
    count = 0
    try:
        with open(DISPATCH_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (record.get("agent") == "gemini"
                        and record.get("mode") == "headless"
                        and datetime.fromtimestamp(record.get("ts", 0)).date() == today):
                    count += 1
    except OSError:
        pass
    return count


def _requests_today():
    """Count of user prompts logged today across all Gemini CLI projects."""
    today = datetime.now().date()
    count = _headless_dispatches_today()
    tmp_dir = os.path.join(GEMINI_DIR, "tmp")
    try:
        project_dirs = os.listdir(tmp_dir)
    except OSError:
        return 0
    for project in project_dirs:
        log_path = os.path.join(tmp_dir, project, "logs.json")
        try:
            with open(log_path) as f:
                entries = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "user":
                continue
            stamp = entry.get("timestamp")
            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.astimezone().date() == today:
                count += 1
    return count


def _next_reset_epoch():
    """Gemini free-tier quotas reset daily; approximate with next local midnight."""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def fetch(force=False):
    del force  # nothing cached; the server layer handles TTLs
    auth = _auth_type()
    if auth is None:
        return {"ok": False,
                "error": "gemini CLI not configured (no ~/.gemini/settings.json)"}

    limit = DAILY_LIMITS.get(auth, 250)
    env_limit = os.environ.get("GEMINI_DAILY_LIMIT")
    if env_limit and env_limit.isdigit():
        limit = int(env_limit)

    used = _requests_today()
    percent = round(min(100.0, used / limit * 100.0), 1)
    plan = "api key" if auth == "gemini-api-key" else "google login"
    return {
        "ok": True,
        "plan": plan,
        "as_of": int(time.time()),
        "source": "local log estimate",
        "windows": [{
            "key": "daily",
            "label": "daily requests (est.)",
            "used_percent": percent,
            "resets_at": _next_reset_epoch(),
        }],
        "requests_today": used,
        "daily_limit": limit,
    }
