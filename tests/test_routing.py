"""Tests for the dispatcher's routing rules and config merging."""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from dispatcher import config as config_mod
from dispatcher.routing import AgentAssessment, Decision, choose_agent

ALL_INSTALLED = {"claude": True, "codex": True, "cursor": True}


def base_config():
    return copy.deepcopy(config_mod.DEFAULT_CONFIG)


def claude_service(five_hour=23.0, seven_day=4.0):
    return {
        "ok": True, "plan": "pro", "as_of": 1787784306, "source": "statusline snapshot",
        "windows": [
            {"key": "five_hour", "label": "5h window",
             "used_percent": five_hour, "resets_at": 1781227800},
            {"key": "seven_day", "label": "7d window",
             "used_percent": seven_day, "resets_at": 1781344800},
        ],
    }


def codex_service(primary=0.0, secondary=0.0):
    return {
        "ok": True, "as_of": 1787784317, "source": "codex app-server", "plan": "plus",
        "windows": [
            {"key": "primary", "label": "5h window",
             "used_percent": primary, "resets_at": 1787802317},
            {"key": "secondary", "label": "7d window",
             "used_percent": secondary, "resets_at": 1788389117},
        ],
        "credits_balance": "0", "has_credits": False,
    }


def cursor_service(used_usd=4.87, limit_usd=400.0):
    remaining = limit_usd - used_usd
    return {
        "ok": True, "plan": "ultra", "as_of": 1787784317,
        "used_usd": used_usd, "limit_usd": limit_usd, "remaining_usd": remaining,
        "percent_used": round(used_usd / limit_usd * 100, 2),
        "api_percent_used": 0.97, "auto_percent_used": 0.0,
        "cycle_start": 1785601879, "cycle_end": 1788280279,
        "on_demand": None, "source": "cursor dashboard RPC",
    }


def all_services(**overrides):
    services = {
        "claude": claude_service(),
        "codex": codex_service(),
        "cursor": cursor_service(),
    }
    services.update(overrides)
    return services


def by_agent(decision):
    return {a.agent: a for a in decision.assessments}


class ChooseAgentTest(unittest.TestCase):
    def test_happy_path_picks_first_priority_agent(self):
        decision = choose_agent(all_services(), base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "claude")
        self.assertIn("claude", decision.reason)
        self.assertIn("5h window at 23% used", decision.reason)

    def test_returns_decision_and_assessment_types(self):
        decision = choose_agent(all_services(), base_config(), ALL_INSTALLED)
        self.assertIsInstance(decision, Decision)
        for assessment in decision.assessments:
            self.assertIsInstance(assessment, AgentAssessment)

    def test_assessments_cover_every_priority_agent_in_order(self):
        decision = choose_agent(all_services(), base_config(), {"claude": True})
        self.assertEqual([a.agent for a in decision.assessments],
                         ["claude", "codex", "cursor"])

    def test_uninstalled_agents_are_assessed_as_not_installed(self):
        decision = choose_agent(all_services(), base_config(), {"claude": True})
        codex = by_agent(decision)["codex"]
        self.assertFalse(codex.eligible)
        self.assertEqual(codex.reason, "CLI not installed")
        self.assertIsNone(codex.headroom)

    def test_headroom_uses_worst_window(self):
        services = all_services(claude=claude_service(five_hour=23.0, seven_day=61.0))
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertAlmostEqual(by_agent(decision)["claude"].headroom, 39.0)
        self.assertIn("7d window at 61% used", by_agent(decision)["claude"].reason)

    def test_cursor_headroom_uses_percent_used(self):
        decision = choose_agent(all_services(), base_config(), ALL_INSTALLED)
        cursor = by_agent(decision)["cursor"]
        self.assertAlmostEqual(cursor.headroom, 100.0 - 1.22, places=2)
        self.assertIn("remaining", cursor.reason)

    def test_threshold_exceeded_skips_to_next_agent(self):
        services = all_services(claude=claude_service(five_hour=91.0))
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        claude = by_agent(decision)["claude"]
        self.assertFalse(claude.eligible)
        self.assertEqual(claude.reason, "over threshold: 5h window at 91% > 85%")
        self.assertAlmostEqual(claude.headroom, 9.0)

    def test_any_window_over_threshold_disqualifies(self):
        services = all_services(claude=claude_service(five_hour=2.0, seven_day=99.0))
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        self.assertEqual(by_agent(decision)["claude"].reason,
                         "over threshold: 7d window at 99% > 85%")

    def test_exactly_at_threshold_is_still_eligible(self):
        services = all_services(claude=claude_service(five_hour=85.0))
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "claude")

    def test_custom_threshold_from_config(self):
        config = base_config()
        config["thresholds"]["claude"]["max_window_percent"] = 20
        decision = choose_agent(all_services(), config, ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        self.assertEqual(by_agent(decision)["claude"].reason,
                         "over threshold: 5h window at 23% > 20%")

    def test_cursor_dollar_threshold_blocks_low_balance(self):
        services = all_services(
            claude=claude_service(five_hour=99.0),
            codex=codex_service(primary=99.0),
            cursor=cursor_service(used_usd=390.0, limit_usd=400.0),
        )
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        cursor = by_agent(decision)["cursor"]
        self.assertFalse(cursor.eligible)
        self.assertEqual(cursor.reason, "over threshold: $10.00 remaining < $25.00")

    def test_cursor_dollar_threshold_passes_above_minimum(self):
        services = all_services(
            claude=claude_service(five_hour=99.0),
            codex=codex_service(primary=99.0),
            cursor=cursor_service(used_usd=370.0, limit_usd=400.0),
        )
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "cursor")
        self.assertTrue(by_agent(decision)["cursor"].eligible)

    def test_custom_cursor_threshold_from_config(self):
        config = base_config()
        config["thresholds"]["cursor"]["min_remaining_usd"] = 396.0
        services = all_services(
            claude=claude_service(five_hour=99.0), codex=codex_service(primary=99.0))
        decision = choose_agent(services, config, ALL_INSTALLED)
        self.assertEqual(by_agent(decision)["cursor"].reason,
                         "over threshold: $395.13 remaining < $396.00")

    def test_all_over_threshold_falls_back_to_max_headroom(self):
        services = all_services(
            claude=claude_service(five_hour=95.0),
            codex=codex_service(primary=88.0),
            cursor=cursor_service(used_usd=395.0, limit_usd=400.0),
        )
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        self.assertEqual(decision.reason, "all agents over limits; codex has most headroom")
        self.assertTrue(all(not a.eligible for a in decision.assessments))

    def test_fallback_only_considers_installed_agents(self):
        # codex has the most headroom but is not installed, so cursor wins.
        services = all_services(
            claude=claude_service(five_hour=99.0),
            codex=codex_service(primary=88.0),
            cursor=cursor_service(used_usd=390.0, limit_usd=400.0),
        )
        installed = {"claude": True, "cursor": True}
        decision = choose_agent(services, base_config(), installed)
        self.assertEqual(decision.agent, "cursor")
        self.assertEqual(decision.reason, "all agents over limits; cursor has most headroom")

    def test_fallback_ties_break_by_priority(self):
        services = all_services(
            claude=claude_service(five_hour=90.0),
            codex=codex_service(primary=90.0),
            cursor=cursor_service(used_usd=399.0, limit_usd=400.0),
        )
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "claude")

    def test_unknown_data_ranked_after_known(self):
        services = all_services(claude={"ok": False, "error": "statusline snapshot missing"})
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        claude = by_agent(decision)["claude"]
        self.assertTrue(claude.eligible)
        self.assertIsNone(claude.headroom)
        self.assertEqual(claude.reason, "usage unknown: statusline snapshot missing")

    def test_unknown_data_chosen_when_others_are_over_limits(self):
        services = all_services(
            claude={"ok": False, "error": "no snapshot"},
            codex=codex_service(primary=99.0),
            cursor=cursor_service(used_usd=399.0, limit_usd=400.0),
        )
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "claude")
        self.assertIn("usage unknown", decision.reason)

    def test_missing_service_entry_is_unknown_but_eligible(self):
        services = {"codex": codex_service(), "cursor": cursor_service()}
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        claude = by_agent(decision)["claude"]
        self.assertTrue(claude.eligible)
        self.assertIsNone(claude.headroom)
        self.assertEqual(claude.reason, "usage unknown: no tracker data")

    def test_empty_services_picks_first_installed_with_unknown_usage(self):
        decision = choose_agent({}, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "claude")
        self.assertTrue(all(a.headroom is None for a in decision.assessments))

    def test_service_ok_but_windows_missing(self):
        services = all_services(claude={"ok": True, "plan": "pro", "windows": []})
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        self.assertEqual(by_agent(decision)["claude"].reason, "usage unknown: no window data")

    def test_cursor_without_spend_data_is_unknown(self):
        services = all_services(cursor={"ok": True, "plan": "ultra"})
        decision = choose_agent(services, base_config(), ALL_INSTALLED)
        cursor = by_agent(decision)["cursor"]
        self.assertTrue(cursor.eligible)
        self.assertIsNone(cursor.headroom)
        self.assertEqual(cursor.reason, "usage unknown: no spend data")

    def test_nothing_installed(self):
        decision = choose_agent(all_services(), base_config(), {})
        self.assertIsNone(decision.agent)
        self.assertEqual(decision.reason, "no supported agent CLIs installed")
        self.assertEqual(len(decision.assessments), 3)
        self.assertTrue(all(not a.eligible for a in decision.assessments))

    def test_all_installed_flags_false(self):
        installed = {"claude": False, "codex": False, "cursor": False}
        decision = choose_agent(all_services(), base_config(), installed)
        self.assertIsNone(decision.agent)
        self.assertEqual(decision.reason, "no supported agent CLIs installed")

    def test_override_is_honored(self):
        services = all_services(cursor=cursor_service(used_usd=399.0, limit_usd=400.0))
        decision = choose_agent(services, base_config(), ALL_INSTALLED, override="cursor")
        self.assertEqual(decision.agent, "cursor")
        self.assertEqual(decision.reason, "manual override via --agent")
        self.assertEqual(len(decision.assessments), 3)

    def test_override_wins_over_priority_and_thresholds(self):
        decision = choose_agent(all_services(), base_config(), ALL_INSTALLED, override="codex")
        self.assertEqual(decision.agent, "codex")

    def test_override_not_installed(self):
        installed = {"claude": True, "codex": True}
        decision = choose_agent(all_services(), base_config(), installed, override="cursor")
        self.assertIsNone(decision.agent)
        self.assertIn("cursor", decision.reason)
        self.assertIn("not installed", decision.reason)
        self.assertEqual(len(decision.assessments), 3)

    def test_custom_priority_order(self):
        config = base_config()
        config["priority"] = ["cursor", "codex", "claude"]
        decision = choose_agent(all_services(), config, ALL_INSTALLED)
        self.assertEqual(decision.agent, "cursor")
        self.assertEqual([a.agent for a in decision.assessments],
                         ["cursor", "codex", "claude"])

    def test_priority_subset_excludes_other_agents(self):
        config = base_config()
        config["priority"] = ["codex"]
        decision = choose_agent(all_services(), config, ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")
        self.assertEqual([a.agent for a in decision.assessments], ["codex"])

    def test_non_numeric_used_percent_is_ignored(self):
        service = claude_service()
        service["windows"][0]["used_percent"] = None
        service["windows"][1]["used_percent"] = 30.0
        decision = choose_agent(all_services(claude=service), base_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "claude")
        self.assertAlmostEqual(by_agent(decision)["claude"].headroom, 70.0)


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "dispatcher.json")
        patcher = mock.patch.object(config_mod, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f)

    def test_defaults_when_file_missing(self):
        self.assertEqual(config_mod.load_config(), config_mod.DEFAULT_CONFIG)

    def test_user_values_win(self):
        self.write({"priority": ["cursor", "claude"], "tracker_url": "http://localhost:9000/x"})
        config = config_mod.load_config()
        self.assertEqual(config["priority"], ["cursor", "claude"])
        self.assertEqual(config["tracker_url"], "http://localhost:9000/x")

    def test_nested_dicts_merge_recursively(self):
        self.write({"thresholds": {"claude": {"max_window_percent": 50}}})
        config = config_mod.load_config()
        self.assertEqual(config["thresholds"]["claude"]["max_window_percent"], 50)
        self.assertEqual(config["thresholds"]["codex"]["max_window_percent"], 85)
        self.assertEqual(config["thresholds"]["cursor"]["min_remaining_usd"], 25.0)

    def test_unknown_keys_are_preserved(self):
        self.write({"thresholds": {"gemini": {"max_window_percent": 70}}, "verbose": True})
        config = config_mod.load_config()
        self.assertEqual(config["thresholds"]["gemini"], {"max_window_percent": 70})
        self.assertTrue(config["verbose"])
        self.assertIn("claude", config["thresholds"])

    def test_corrupt_file_is_ignored(self):
        self.write("{not json at all")
        self.assertEqual(config_mod.load_config(), config_mod.DEFAULT_CONFIG)

    def test_non_dict_json_is_ignored(self):
        self.write(["claude", "codex"])
        self.assertEqual(config_mod.load_config(), config_mod.DEFAULT_CONFIG)

    def test_does_not_mutate_defaults(self):
        self.write({"thresholds": {"claude": {"max_window_percent": 1}}, "priority": ["cursor"]})
        before = copy.deepcopy(config_mod.DEFAULT_CONFIG)
        config = config_mod.load_config()
        config["thresholds"]["codex"]["max_window_percent"] = 999
        config["priority"].append("gemini")
        self.assertEqual(config_mod.DEFAULT_CONFIG, before)

    def test_result_is_independent_across_calls(self):
        first = config_mod.load_config()
        first["thresholds"]["claude"]["max_window_percent"] = 3
        second = config_mod.load_config()
        self.assertEqual(second["thresholds"]["claude"]["max_window_percent"], 85)

    def test_loaded_config_drives_routing(self):
        self.write({"priority": ["codex", "claude", "cursor"]})
        decision = choose_agent(all_services(), config_mod.load_config(), ALL_INSTALLED)
        self.assertEqual(decision.agent, "codex")


if __name__ == "__main__":
    unittest.main()
