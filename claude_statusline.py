#!/usr/bin/env python3
"""Claude Code statusline hook.

Claude Code pipes session JSON (including rate_limits for Pro/Max
subscribers) to this script after every assistant message. We save the
rate-limit data to ~/.claude/usage-snapshot.json for the usage-tracker app,
and print a compact statusline.

Installed via "statusLine" in ~/.claude/settings.json.
"""

import json
import os
import sys
import time

SNAPSHOT_PATH = os.path.expanduser("~/.claude/usage-snapshot.json")


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("")
        return

    limits = data.get("rate_limits") or {}
    if limits:
        try:
            tmp = SNAPSHOT_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ts": int(time.time()), "rate_limits": limits}, f)
            os.replace(tmp, SNAPSHOT_PATH)
        except OSError:
            pass

    model = (data.get("model") or {}).get("display_name", "?")
    parts = [model]
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label}: {round(pct)}%")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx: {round(ctx)}%")
    print(" | ".join(parts))


if __name__ == "__main__":
    main()
