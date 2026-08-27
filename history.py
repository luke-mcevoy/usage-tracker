"""Time-series history for the usage tracker.

The server appends compact snapshots of every service's usage to a local
JSONL file, and `daily_series` folds those snapshots — plus the dispatch
log — into per-day analytics:

  - cursor: real dollars spent per day (sum of positive deltas between
    consecutive snapshots; a billing-cycle reset shows as a negative delta
    and is ignored rather than counted as negative spend)
  - claude / codex: peak rate-limit window utilization seen that day
  - gemini: peak estimated request count that day
  - all agents: dispatches per day from the `ai` dispatch log

Snapshots only accumulate while the server is running, so the series starts
the day this feature shipped and densifies with uptime.
"""

import json
import os
import threading
import time
from datetime import datetime

HISTORY_PATH = os.path.expanduser("~/.cache/usage-tracker/history.jsonl")
MIN_SNAPSHOT_INTERVAL_S = 60

_append_lock = threading.Lock()
_last_append_ts = 0.0


def snapshot_from_services(services, now=None):
    """Reduce a full /api/usage payload to one compact history row."""
    snap = {"ts": int(now if now is not None else time.time())}
    for name, svc in (services or {}).items():
        if not isinstance(svc, dict) or not svc.get("ok"):
            continue
        entry = {}
        windows = {}
        for w in svc.get("windows") or []:
            key, pct = w.get("key"), w.get("used_percent")
            if key and isinstance(pct, (int, float)):
                windows[key] = pct
        if windows:
            entry["win"] = windows
        if name == "cursor":
            for field in ("used_usd", "limit_usd"):
                if isinstance(svc.get(field), (int, float)):
                    entry[field] = svc[field]
        if name == "gemini" and isinstance(svc.get("requests_today"), int):
            entry["requests"] = svc["requests_today"]
        if entry:
            snap[name] = entry
    return snap


def append_snapshot(snap):
    """Append one row, throttled so bursts of dashboard refreshes don't spam."""
    global _last_append_ts
    with _append_lock:
        if snap["ts"] - _last_append_ts < MIN_SNAPSHOT_INTERVAL_S:
            return False
        _last_append_ts = snap["ts"]
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, separators=(",", ":")) + "\n")
        return True
    except OSError:
        return False


def read_history(max_age_days=90):
    cutoff = time.time() - max_age_days * 86400
    rows = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("ts", 0) >= cutoff:
                    rows.append(row)
    except OSError:
        pass
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def _date_of(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def daily_series(snapshots, dispatch_records, days=14, now=None):
    """Fold snapshots + dispatch records into an ordered list of day rows."""
    now = now if now is not None else time.time()
    cutoff = now - days * 86400

    by_date = {}

    def day(ts):
        return by_date.setdefault(_date_of(ts), {
            "date": _date_of(ts),
            "cursor_spend_usd": 0.0,
            "dispatches": {},
            "peaks": {},
        })

    # Cursor spend: positive deltas between consecutive samples, credited to
    # the later sample's date. Negative deltas are billing-cycle resets.
    prev_usd = None
    for snap in snapshots:
        ts = snap.get("ts", 0)
        usd = (snap.get("cursor") or {}).get("used_usd")
        if isinstance(usd, (int, float)):
            if prev_usd is not None and usd > prev_usd and ts >= cutoff:
                day(ts)["cursor_spend_usd"] += usd - prev_usd
            prev_usd = usd
        if ts < cutoff:
            continue
        row = day(ts)
        for svc in ("claude", "codex"):
            for key, pct in ((snap.get(svc) or {}).get("win") or {}).items():
                peak_key = f"{svc}_{key}"
                row["peaks"][peak_key] = max(row["peaks"].get(peak_key, 0.0), pct)
        requests = (snap.get("gemini") or {}).get("requests")
        if isinstance(requests, int):
            row["peaks"]["gemini_requests"] = max(
                row["peaks"].get("gemini_requests", 0), requests)

    for record in dispatch_records or []:
        ts = record.get("ts", 0)
        if ts < cutoff:
            continue
        agent = record.get("agent") or "?"
        dispatches = day(ts)["dispatches"]
        dispatches[agent] = dispatches.get(agent, 0) + 1

    for row in by_date.values():
        row["cursor_spend_usd"] = round(row["cursor_spend_usd"], 2)

    return [by_date[d] for d in sorted(by_date)]
