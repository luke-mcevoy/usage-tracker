"""Decide which vendor CLI should run a task.

Pure functions: the tracker's /api/usage payload plus config in, a Decision
out. Nothing here shells out or reads the network, so the routing rules stay
testable and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_PRIORITY = ["claude", "codex", "cursor"]
DEFAULT_MAX_WINDOW_PERCENT = 85.0
DEFAULT_MIN_REMAINING_USD = 25.0


@dataclass
class AgentAssessment:
    agent: str
    eligible: bool
    reason: str
    headroom: float | None = None


@dataclass
class Decision:
    agent: str | None
    reason: str
    assessments: list = field(default_factory=list)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _worst_window(service):
    """Return (label, used_percent) for the most-consumed window, or None."""
    worst = None
    for window in service.get("windows") or []:
        if not isinstance(window, dict):
            continue
        percent = _to_float(window.get("used_percent"))
        if percent is None:
            continue
        label = window.get("label") or window.get("key") or "window"
        if worst is None or percent > worst[1]:
            worst = (label, percent)
    return worst


def _threshold(config, agent, key, fallback):
    thresholds = config.get("thresholds") or {}
    agent_thresholds = thresholds.get(agent) or {}
    value = _to_float(agent_thresholds.get(key))
    return fallback if value is None else value


def _assess_windowed(agent, service, config):
    """Assess an agent whose quota is expressed as rolling usage windows."""
    worst = _worst_window(service)
    if worst is None:
        return AgentAssessment(agent, True, "usage unknown: no window data", None)
    label, percent = worst
    limit = _threshold(config, agent, "max_window_percent", DEFAULT_MAX_WINDOW_PERCENT)
    headroom = 100.0 - percent
    if percent > limit:
        return AgentAssessment(
            agent, False, f"over threshold: {label} at {percent:g}% > {limit:g}%", headroom)
    return AgentAssessment(agent, True, f"{label} at {percent:g}% used", headroom)


def _assess_cursor(agent, service, config):
    """Assess an agent whose quota is expressed as dollars against a limit."""
    percent = _to_float(service.get("percent_used"))
    remaining = _to_float(service.get("remaining_usd"))
    if percent is None:
        return AgentAssessment(agent, True, "usage unknown: no spend data", None)
    limit = _threshold(config, agent, "min_remaining_usd", DEFAULT_MIN_REMAINING_USD)
    headroom = 100.0 - percent
    if remaining is None:
        return AgentAssessment(agent, True, f"{percent:g}% of budget used", headroom)
    if remaining < limit:
        return AgentAssessment(
            agent, False,
            f"over threshold: ${remaining:,.2f} remaining < ${limit:,.2f}", headroom)
    return AgentAssessment(
        agent, True, f"${remaining:,.2f} remaining ({percent:g}% used)", headroom)


def _assess(agent, services, config, installed):
    if not installed.get(agent):
        return AgentAssessment(agent, False, "CLI not installed", None)
    service = services.get(agent)
    if not isinstance(service, dict) or not service.get("ok"):
        error = service.get("error") if isinstance(service, dict) else None
        reason = f"usage unknown: {error}" if error else "usage unknown: no tracker data"
        return AgentAssessment(agent, True, reason, None)
    if agent == "cursor":
        return _assess_cursor(agent, service, config)
    return _assess_windowed(agent, service, config)


def choose_agent(services, config, installed, override=None):
    """Pick the agent with the most quota headroom that is within its limits."""
    services = services or {}
    installed = installed or {}
    priority = list((config or {}).get("priority") or DEFAULT_PRIORITY)
    assessments = [_assess(agent, services, config or {}, installed) for agent in priority]

    if override:
        if installed.get(override):
            return Decision(override, "manual override via --agent", assessments)
        return Decision(
            None,
            f"manual override via --agent requested {override}, "
            f"but the {override} CLI is not installed",
            assessments)

    for assessment in assessments:
        if assessment.eligible and assessment.headroom is not None:
            return Decision(
                assessment.agent,
                f"{assessment.agent} is the highest-priority agent within limits "
                f"({assessment.reason})",
                assessments)

    for assessment in assessments:
        if assessment.eligible:
            return Decision(
                assessment.agent,
                f"{assessment.agent} is the highest-priority available agent "
                f"({assessment.reason})",
                assessments)

    known = [a for a in assessments if a.headroom is not None]
    if known:
        best = max(known, key=lambda a: a.headroom)
        return Decision(
            best.agent, f"all agents over limits; {best.agent} has most headroom", assessments)

    return Decision(None, "no supported agent CLIs installed", assessments)
