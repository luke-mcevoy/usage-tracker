"""Cursor usage provider.

Reads the access token from Cursor's local state database (read-only) and
queries the dashboard API. Primary: api2.cursor.sh DashboardService RPC with
a Bearer token. Fallback: cursor.com/api/usage-summary with a session cookie
constructed from the same token.
"""

import base64
import json
import os
import sqlite3
import time
import urllib.request

STATE_DB = os.path.expanduser(
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")

RPC_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
SUMMARY_URL = "https://cursor.com/api/usage-summary"


def _read_state():
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        rows = dict(conn.execute(
            "SELECT key, value FROM ItemTable WHERE key IN "
            "('cursorAuth/accessToken', 'cursorAuth/stripeMembershipType')"
        ).fetchall())
    finally:
        conn.close()
    return rows.get("cursorAuth/accessToken"), rows.get("cursorAuth/stripeMembershipType")


def _jwt_sub(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub", "")
    except Exception:
        return ""


def _ms_to_epoch(value):
    try:
        return int(int(value) / 1000)
    except (TypeError, ValueError):
        return None


def _from_rpc(token):
    req = urllib.request.Request(
        RPC_URL, data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    plan = data.get("planUsage") or {}
    if plan.get("limit") is None:
        return None
    return {
        "used_usd": plan.get("totalSpend", 0) / 100,
        "limit_usd": plan["limit"] / 100,
        "remaining_usd": plan.get("remaining", 0) / 100,
        "percent_used": round(float(plan.get("totalPercentUsed", 0)), 2),
        "api_percent_used": round(float(plan.get("apiPercentUsed", 0)), 2),
        "auto_percent_used": round(float(plan.get("autoPercentUsed", 0)), 2),
        "cycle_start": _ms_to_epoch(data.get("billingCycleStart")),
        "cycle_end": _ms_to_epoch(data.get("billingCycleEnd")),
        "on_demand": None,
        "source": "cursor dashboard RPC",
    }


def _from_summary(token):
    user_id = _jwt_sub(token).split("|")[-1]
    req = urllib.request.Request(SUMMARY_URL, headers={
        "Cookie": f"WorkosCursorSessionToken={user_id}%3A%3A{token}",
        "Origin": "https://cursor.com",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    plan = (data.get("individualUsage") or {}).get("plan") or {}
    if plan.get("limit") is None:
        return None
    on_demand = (data.get("individualUsage") or {}).get("onDemand") or {}

    def iso_epoch(s):
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return None

    return {
        "used_usd": plan.get("used", 0) / 100,
        "limit_usd": plan["limit"] / 100,
        "remaining_usd": plan.get("remaining", 0) / 100,
        "percent_used": round(float(plan.get("totalPercentUsed", 0)), 2),
        "api_percent_used": round(float(plan.get("apiPercentUsed", 0)), 2),
        "auto_percent_used": round(float(plan.get("autoPercentUsed", 0)), 2),
        "cycle_start": iso_epoch(data.get("billingCycleStart")),
        "cycle_end": iso_epoch(data.get("billingCycleEnd")),
        "on_demand": {
            "used_usd": (on_demand.get("used") or 0) / 100,
            "limit_usd": (on_demand.get("limit") or 0) / 100 if on_demand.get("limit") else None,
        } if on_demand.get("enabled") else None,
        "source": "cursor usage-summary",
    }


def fetch(force=False):
    del force  # server layer handles caching
    try:
        token, membership = _read_state()
    except (sqlite3.Error, OSError) as e:
        return {"ok": False, "error": f"could not read Cursor state db: {e}"}
    if not token:
        return {"ok": False, "error": "no Cursor access token found (are you logged in to Cursor?)"}

    result = None
    errors = []
    for attempt in (_from_rpc, _from_summary):
        try:
            result = attempt(token)
            if result:
                break
        except Exception as e:
            errors.append(f"{attempt.__name__}: {e}")
    if not result:
        return {"ok": False, "error": "; ".join(errors) or "no usage data returned"}

    return {"ok": True, "plan": membership, "as_of": int(time.time()), **result}
