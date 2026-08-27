"""Launching a coding task on whichever vendor CLI was chosen.

Handles the mechanics only — which agent to use is decided in
``dispatcher.routing``. This module knows how to find the CLIs, how to read
current usage (tracker server first, local providers as a fallback), how to
build each CLI's argv, and how to keep the SESSION.md handoff journal.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

AGENT_COMMANDS = {"claude": "claude", "codex": "codex",
                  "gemini": "gemini", "cursor": "cursor-agent"}

JOURNAL_FILENAME = "SESSION.md"
JOURNAL_HEADING = "# Session journal"
TRACKER_TIMEOUT_S = 3

DISPATCH_LOG = os.path.expanduser("~/.cache/usage-tracker/dispatches.jsonl")


def installed_agents():
    """Which vendor CLIs are on PATH."""
    return {name: shutil.which(cmd) is not None for name, cmd in AGENT_COMMANDS.items()}


def _fetch_from_tracker(url, force=False):
    if force:
        url += ("&" if "?" in url else "?") + "refresh=1"
    with urllib.request.urlopen(url, timeout=TRACKER_TIMEOUT_S if not force else 30) as resp:
        payload = json.loads(resp.read())
    services = payload.get("services")
    if not isinstance(services, dict):
        raise ValueError("tracker response had no services object")
    return services


def _fetch_from_providers(force=False):
    from providers import claude, codex, cursor, gemini

    fetchers = {"claude": claude.fetch, "codex": codex.fetch,
                "gemini": gemini.fetch, "cursor": cursor.fetch}

    def run(fetcher):
        try:
            return fetcher(force=force)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {name: pool.submit(run, fn) for name, fn in fetchers.items()}
        return {name: fut.result() for name, fut in futures.items()}


def fetch_usage(config, force=False):
    """Current usage per service, shaped like the tracker's ``services`` dict.

    The running tracker server is preferred because it caches and so avoids
    hammering the vendor endpoints; if it is not up we call the providers
    ourselves. ``force`` bypasses caches — used after waiting out a rate-limit
    window, when cached data would still show the old, exhausted numbers.
    """
    try:
        return _fetch_from_tracker(config["tracker_url"], force=force)
    except Exception:
        return _fetch_from_providers(force=force)


def build_command(agent, prompt, headless):
    cmd = AGENT_COMMANDS[agent]
    if not headless:
        return [cmd, prompt]
    if agent == "codex":
        # --skip-git-repo-check: codex exec otherwise refuses to run outside a git repo
        # --sandbox workspace-write: exec otherwise defaults to a read-only
        # sandbox, so headless tasks cannot edit files or commit
        return [cmd, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt]
    if agent == "cursor":
        # -f trusts the working directory; headless runs otherwise stop at a trust prompt
        return [cmd, "-f", "-p", prompt]
    if agent == "gemini":
        # --skip-trust + yolo approvals: headless gemini cannot answer trust
        # or tool-approval prompts (same role as the flags above)
        return [cmd, "--skip-trust", "--approval-mode", "yolo", "-p", prompt]
    # --dangerously-skip-permissions: headless claude cannot answer permission
    # prompts and ignores project .claude/settings.json allow rules in
    # untrusted dirs, so every write would be refused (same role as cursor -f)
    return [cmd, "--dangerously-skip-permissions", "-p", prompt]


def journal_preamble():
    return (
        "[Session journal — before you start]\n"
        "A file named SESSION.md may exist in the current working directory. If it "
        "does, read it first: it holds handoff notes from previous AI sessions, "
        "possibly written by other models, and may explain earlier decisions, "
        "dead ends, and unfinished work.\n\n"
        "When this task is complete, append a short dated entry to SESSION.md "
        "(create it if it is missing) covering: what you did, the key decisions and "
        "why you made them, and anything still unresolved or worth watching. A few "
        "lines is enough — it is a handoff note for the next session, not a changelog."
    )


def append_run_record(agent, task):
    """Note the dispatch in ./SESSION.md so the next session sees the history."""
    path = os.path.join(os.getcwd(), JOURNAL_FILENAME)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    summary = " ".join(task.split())[:100]
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write(f"{JOURNAL_HEADING}\n\n")
        f.write(f"- {stamp} — dispatched to {agent}: {summary}\n")


def log_dispatch(agent, reason, task, mode, exit_code=None, forced=False):
    """Append one routing decision to the analytics log.

    The dashboard reads this file to show dispatch history and per-agent
    counts. One JSON object per line; failures to write never block a launch.
    """
    record = {
        "ts": int(time.time()),
        "agent": agent,
        "reason": reason,
        "task": " ".join(task.split())[:140],
        "mode": mode,
        "exit_code": exit_code,
        "forced": forced,
        "cwd": os.getcwd(),
    }
    try:
        os.makedirs(os.path.dirname(DISPATCH_LOG), exist_ok=True)
        with open(DISPATCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def read_dispatch_log(limit=100):
    """Most recent dispatch records, newest first."""
    try:
        with open(DISPATCH_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    records.reverse()
    return records


def launch(agent, task, headless, use_journal, reason="", forced=False):
    """Hand the task to the agent's CLI.

    Interactive mode replaces this process, so it never returns.
    """
    prompt = f"{journal_preamble()}\n\nTASK:\n{task}" if use_journal else task
    if use_journal:
        append_run_record(agent, task)

    cmd = build_command(agent, prompt, headless)
    if headless:
        # Close stdin: codex (and possibly others) block waiting for piped
        # input when stdin is not a tty, which would hang scripted callers.
        code = subprocess.call(cmd, stdin=subprocess.DEVNULL)
        log_dispatch(agent, reason, task, "headless", exit_code=code, forced=forced)
        return code

    # execvp never returns, so the interactive record has no exit code
    log_dispatch(agent, reason, task, "interactive", forced=forced)
    sys.stdout.flush()
    os.execvp(cmd[0], cmd)
