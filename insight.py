"""Routing memory, efficiency, and a readable snapshot of the current decision.

Nothing here shells out. Task kinds are a small keyword heuristic over the
dispatch log — enough to notice that Codex finishes implementations and
Claude finishes explanations, not a classifier.
"""

from dispatcher.routing import Decision, choose_agent

KINDS = ("implementation", "explanation", "debug", "probe", "other")

_IMPLEMENT = ("implement", "build ", "add ", "port ", "refactor", "create ",
              "write ", "complete spec", "workstream")
_EXPLAIN = ("explain", "why ", "how does", "teach", "what is", "how do ")
_DEBUG = ("fix ", "bug", "flaky", "debug", "failing test", "test_")
_PROBE = ("reply with exactly", "nothing else", "pong", "ping")


def classify_task(text):
    t = (text or "").lower()
    if any(s in t for s in _PROBE):
        return "probe"
    if any(s in t for s in _IMPLEMENT):
        return "implementation"
    if any(s in t for s in _EXPLAIN):
        return "explanation"
    if any(s in t for s in _DEBUG):
        return "debug"
    return "other"


def _success(record):
    """True / False / None (unknown — interactive or missing exit)."""
    if record.get("mode") == "interactive":
        return None
    if record.get("status") == "interrupted":
        return False
    if record.get("mode") == "gateway":
        code = record.get("exit_code")
        if code is None:
            return None
        return code == 0
    code = record.get("exit_code")
    if code is None:
        return None
    return code == 0


def efficiency(records, history_days=None):
    records = records or []
    forced = sum(1 for r in records if r.get("forced"))
    headless = [r for r in records if r.get("mode") in ("headless", "gateway")]
    known = [r for r in headless if _success(r) is not None]
    ok = sum(1 for r in known if _success(r))
    failed = sum(1 for r in known if _success(r) is False)
    cursor_n = sum(1 for r in records if r.get("agent") == "cursor")
    spend_today = 0.0
    if history_days:
        spend_today = (history_days[-1] or {}).get("cursor_spend_usd") or 0.0
    return {
        "total": len(records),
        "forced": forced,
        "forced_pct": round(100.0 * forced / len(records), 1) if records else 0.0,
        "headless": len(headless),
        "succeeded": ok,
        "failed": failed,
        "success_pct": round(100.0 * ok / len(known), 1) if known else None,
        "cursor_dispatches": cursor_n,
        "cursor_spend_today": round(float(spend_today), 2),
        "known_outcomes": len(known),
    }


def memory(records):
    """Success counts keyed by task kind, then agent."""
    table = {}
    for record in records or []:
        kind = classify_task(record.get("task"))
        agent = record.get("agent") or "?"
        outcome = _success(record)
        if outcome is None:
            continue
        slot = table.setdefault(kind, {}).setdefault(agent, {"ok": 0, "n": 0})
        slot["n"] += 1
        if outcome:
            slot["ok"] += 1
    rows = []
    for kind, agents in table.items():
        for agent, slot in agents.items():
            rows.append({
                "kind": kind,
                "agent": agent,
                "ok": slot["ok"],
                "n": slot["n"],
                "rate": round(100.0 * slot["ok"] / slot["n"], 1) if slot["n"] else 0.0,
            })
    rows.sort(key=lambda r: (-r["n"], r["kind"], r["agent"]))
    return rows


def apply_memory(decision, task, records, config=None, min_samples=3):
    """If another eligible non-reserve agent clearly wins this task kind, use it.

    Conservative: only overrides when the memory agent has ≥70% success on
    ≥min_samples runs of this kind, and the quota pick is under 50% on
    ≥min_samples of the same kind. Quota still gates eligibility.
    """
    if not decision or not decision.agent or not task:
        return decision
    reserve = set((config or {}).get("reserve") or [])
    kind = classify_task(task)
    by_agent = {}
    for row in memory(records):
        if row["kind"] == kind:
            by_agent[row["agent"]] = row
    current = by_agent.get(decision.agent)
    best = None
    for assessment in decision.assessments:
        if not assessment.eligible or assessment.agent in reserve:
            continue
        row = by_agent.get(assessment.agent)
        if not row or row["n"] < min_samples:
            continue
        if best is None or row["rate"] > best["rate"]:
            best = row
    if best is None or best["agent"] == decision.agent:
        return decision
    if best["rate"] < 70:
        return decision
    current_rate = current["rate"] if current and current["n"] >= min_samples else None
    if current_rate is not None and current_rate >= 50:
        return decision
    return Decision(
        best["agent"],
        f"memory: {kind} tasks succeed on {best['agent']} "
        f"({best['ok']}/{best['n']})"
        + (f" vs {decision.agent} ({current['ok']}/{current['n']})" if current else ""),
        decision.assessments,
    )


def snapshot(services, records, history_days, config, installed):
    """Dashboard payload: current routing + efficiency + memory."""
    decision = choose_agent(services, config, installed)
    assessments = []
    for a in decision.assessments:
        assessments.append({
            "agent": a.agent,
            "eligible": a.eligible,
            "reason": a.reason,
            "headroom": a.headroom,
            "reserve": a.agent in set((config or {}).get("reserve") or []),
        })
    return {
        "routing": {
            "agent": decision.agent,
            "reason": decision.reason,
            "assessments": assessments,
        },
        "efficiency": efficiency(records, history_days),
        "memory": memory(records),
    }
