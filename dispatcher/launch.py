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
import uuid
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


def _write_log(record):
    """Append one JSON object per line. Never block the caller on failure."""
    try:
        os.makedirs(os.path.dirname(DISPATCH_LOG), exist_ok=True)
        with open(DISPATCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def log_dispatch_start(run_id, agent, reason, task, mode, forced, pid):
    """Note a dispatch launching. Paired with a later ``end`` event by ``run_id``."""
    _write_log({
        "event": "start",
        "run_id": run_id,
        "ts": int(time.time()),
        "agent": agent,
        "reason": reason,
        "task": " ".join(task.split())[:140],
        "mode": mode,
        "forced": forced,
        "cwd": os.getcwd(),
        "pid": pid,
    })


def log_dispatch_end(run_id, agent, exit_code, duration_s):
    """Note a dispatch finishing. Correlated with its start event by ``run_id``."""
    _write_log({
        "event": "end",
        "run_id": run_id,
        "ts": int(time.time()),
        "agent": agent,
        "exit_code": exit_code,
        "duration_s": round(float(duration_s), 3),
    })


def _pid_alive(pid):
    """Cheap liveness probe for a locally-owned process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Different user owns pid — treat as alive; we should not see this in
        # practice because dispatches are launched by the current user.
        return True
    except OSError:
        return False
    return True


def _load_log_lines():
    try:
        with open(DISPATCH_LOG, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def read_dispatch_log(limit=100, now=None):
    """Load the dispatch log and return ``(records, active)``.

    - ``records`` is the completed / legacy dispatch history newest-first,
      capped at ``limit`` entries. Each entry has the same shape as before
      (``ts``, ``agent``, ``reason``, ``task``, ``mode``, ``exit_code``,
      ``forced``, ``cwd``) with an added ``status`` field: ``finished`` for a
      normally-ended run, ``interrupted`` for a headless run whose dispatcher
      crashed, ``closed`` for an interactive run that has since exited, or
      ``legacy`` for single-line records written before this change.
    - ``active`` is the list of currently in-flight runs — a start event with
      no matching end event whose ``pid`` is still alive. Each entry adds
      ``run_id``, ``pid``, ``started_at``, ``elapsed_s``, and ``repo`` (the
      basename of the dispatch cwd) on top of the record shape above.
    """
    lines = _load_log_lines()
    starts, ends, legacy = {}, {}, []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_id = ev.get("run_id")
        kind = ev.get("event")
        if run_id and kind == "start":
            starts[run_id] = ev
        elif run_id and kind == "end":
            ends[run_id] = ev
        else:
            legacy.append(ev)

    now_ts = int(now if now is not None else time.time())
    records, active = [], []
    for run_id, start in starts.items():
        base = {
            "ts": start.get("ts"),
            "agent": start.get("agent"),
            "reason": start.get("reason"),
            "task": start.get("task"),
            "mode": start.get("mode"),
            "forced": start.get("forced", False),
            "cwd": start.get("cwd"),
            "run_id": run_id,
            "pid": start.get("pid"),
        }
        end = ends.get(run_id)
        if end is not None:
            base["exit_code"] = end.get("exit_code")
            base["duration_s"] = end.get("duration_s")
            base["status"] = "finished"
            base["ts_end"] = end.get("ts")
            records.append(base)
            continue
        if _pid_alive(base["pid"]):
            base["status"] = "running"
            base["started_at"] = base["ts"]
            base["elapsed_s"] = max(0, now_ts - (base["ts"] or now_ts))
            base["repo"] = os.path.basename(base["cwd"] or "")
            active.append(base)
        else:
            base["exit_code"] = None
            base["status"] = "closed" if base["mode"] == "interactive" else "interrupted"
            records.append(base)

    for rec in legacy:
        entry = dict(rec)
        entry.setdefault("status", "legacy")
        records.append(entry)

    records.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    active.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return records[:limit], active


def launch(agent, task, headless, use_journal, reason="", forced=False):
    """Hand the task to the agent's CLI.

    Interactive mode replaces this process, so it never returns.
    """
    prompt = f"{journal_preamble()}\n\nTASK:\n{task}" if use_journal else task
    if use_journal:
        append_run_record(agent, task)

    cmd = build_command(agent, prompt, headless)
    run_id = uuid.uuid4().hex
    if headless:
        # Close stdin: codex (and possibly others) block waiting for piped
        # input when stdin is not a tty, which would hang scripted callers.
        started_at = time.time()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        log_dispatch_start(run_id, agent, reason, task, "headless", forced, proc.pid)
        code = None
        try:
            code = proc.wait()
        finally:
            # try/finally so a KeyboardInterrupt still closes out the log
            # entry — otherwise a Ctrl+C would leave a phantom "running" row.
            log_dispatch_end(run_id, agent, code, time.time() - started_at)
        return code

    # execvp reuses this process (same pid), so the dashboard can still probe
    # liveness via os.kill(pid, 0); we cannot write an end event from a
    # replaced image, so interactive runs are treated as "closed" once the
    # pid dies.
    log_dispatch_start(run_id, agent, reason, task, "interactive", forced, os.getpid())
    sys.stdout.flush()
    os.execvp(cmd[0], cmd)
