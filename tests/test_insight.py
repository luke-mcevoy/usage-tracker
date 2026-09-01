import unittest

from dispatcher.routing import AgentAssessment, Decision
from insight import apply_memory, classify_task, efficiency, memory


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify_task("Implement captions v2 for this project"),
                         "implementation")
        self.assertEqual(classify_task("Why is fiber coupling alignment-sensitive?"),
                         "explanation")
        self.assertEqual(classify_task("fix the flaky test in test_router.py"),
                         "debug")
        self.assertEqual(classify_task("Reply with exactly the word OK and nothing else"),
                         "probe")
        self.assertEqual(classify_task("hello"), "other")


def _rec(agent, task, ok=True, forced=False, mode="headless"):
    return {
        "agent": agent, "task": task, "forced": forced, "mode": mode,
        "exit_code": 0 if ok else 1, "status": "finished",
    }


class EfficiencyTest(unittest.TestCase):
    def test_forced_and_success(self):
        records = [
            _rec("claude", "implement x", ok=True, forced=True),
            _rec("codex", "implement y", ok=False),
            _rec("cursor", "chat", ok=True, mode="interactive"),
        ]
        stats = efficiency(records, [{"cursor_spend_usd": 12.5}])
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["forced"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(stats["success_pct"], 50.0)
        self.assertEqual(stats["cursor_spend_today"], 12.5)


class MemoryTest(unittest.TestCase):
    def test_groups_by_kind_and_agent(self):
        records = [
            _rec("codex", "Implement the captions"),
            _rec("codex", "Build a beam expander"),
            _rec("claude", "Implement the captions", ok=False),
            _rec("claude", "Why does this lens work?"),
        ]
        rows = {(r["kind"], r["agent"]): r for r in memory(records)}
        self.assertEqual(rows[("implementation", "codex")]["ok"], 2)
        self.assertEqual(rows[("implementation", "claude")]["ok"], 0)
        self.assertEqual(rows[("explanation", "claude")]["n"], 1)

    def test_overrides_weak_quota_pick(self):
        assessments = [
            AgentAssessment("claude", True, "ok", 80.0),
            AgentAssessment("codex", True, "ok", 70.0),
            AgentAssessment("cursor", True, "ok", 90.0),
        ]
        decision = Decision("claude", "priority", assessments)
        records = (
            [_rec("codex", "Implement feature %s" % i) for i in range(4)]
            + [_rec("claude", "Implement feature %s" % i, ok=False) for i in range(3)]
        )
        out = apply_memory(decision, "Implement the rest of captions v2", records,
                           config={"reserve": ["cursor"]})
        self.assertEqual(out.agent, "codex")
        self.assertIn("memory", out.reason)

    def test_does_not_override_when_current_is_fine(self):
        assessments = [
            AgentAssessment("claude", True, "ok", 80.0),
            AgentAssessment("codex", True, "ok", 70.0),
        ]
        decision = Decision("claude", "priority", assessments)
        records = [_rec("claude", "Implement x") for _ in range(4)]
        out = apply_memory(decision, "Implement y", records, config={"reserve": []})
        self.assertEqual(out.agent, "claude")
