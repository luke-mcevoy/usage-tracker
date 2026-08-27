import unittest

from history import daily_series, snapshot_from_services, _date_of

DAY = 86400
NOW = 1_787_800_000


def snap(ts, cursor_usd=None, claude_5h=None, gemini_requests=None):
    row = {"ts": ts}
    if cursor_usd is not None:
        row["cursor"] = {"used_usd": cursor_usd, "limit_usd": 400.0}
    if claude_5h is not None:
        row["claude"] = {"win": {"five_hour": claude_5h}}
    if gemini_requests is not None:
        row["gemini"] = {"requests": gemini_requests}
    return row


class SnapshotFromServicesTest(unittest.TestCase):
    def test_extracts_windows_money_and_requests(self):
        services = {
            "claude": {"ok": True, "windows": [
                {"key": "five_hour", "used_percent": 42.0, "resets_at": 1}]},
            "cursor": {"ok": True, "used_usd": 133.11, "limit_usd": 400.0,
                       "remaining_usd": 266.89},
            "gemini": {"ok": True, "requests_today": 7,
                       "windows": [{"key": "daily", "used_percent": 2.8}]},
            "codex": {"ok": False, "error": "down"},
        }
        s = snapshot_from_services(services, now=NOW)
        self.assertEqual(s["ts"], NOW)
        self.assertEqual(s["claude"]["win"], {"five_hour": 42.0})
        self.assertEqual(s["cursor"]["used_usd"], 133.11)
        self.assertEqual(s["gemini"]["requests"], 7)
        self.assertNotIn("codex", s)

    def test_never_captures_tokens_or_unknown_fields(self):
        services = {"cursor": {"ok": True, "used_usd": 1.0,
                               "access_token": "secret", "source": "rpc"}}
        s = snapshot_from_services(services, now=NOW)
        self.assertEqual(set(s["cursor"]), {"used_usd"})


class DailySeriesTest(unittest.TestCase):
    def test_cursor_spend_sums_positive_deltas(self):
        snaps = [snap(NOW - 3000, cursor_usd=100.0),
                 snap(NOW - 2000, cursor_usd=110.0),
                 snap(NOW - 1000, cursor_usd=112.5)]
        days = daily_series(snaps, [], days=7, now=NOW)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["cursor_spend_usd"], 12.5)

    def test_billing_cycle_reset_not_counted_as_negative_spend(self):
        snaps = [snap(NOW - 4000, cursor_usd=390.0),
                 snap(NOW - 3000, cursor_usd=399.0),
                 snap(NOW - 2000, cursor_usd=5.0),   # new cycle
                 snap(NOW - 1000, cursor_usd=11.0)]
        days = daily_series(snaps, [], days=7, now=NOW)
        self.assertEqual(days[0]["cursor_spend_usd"], 15.0)  # 9 + 6

    def test_spend_split_across_days_credited_to_later_sample(self):
        yesterday = NOW - DAY
        snaps = [snap(yesterday, cursor_usd=100.0),
                 snap(NOW, cursor_usd=130.0)]
        days = daily_series(snaps, [], days=7, now=NOW)
        by_date = {d["date"]: d for d in days}
        self.assertEqual(by_date[_date_of(NOW)]["cursor_spend_usd"], 30.0)
        self.assertEqual(by_date[_date_of(yesterday)]["cursor_spend_usd"], 0.0)

    def test_peaks_take_daily_max(self):
        snaps = [snap(NOW - 2000, claude_5h=40.0, gemini_requests=3),
                 snap(NOW - 1000, claude_5h=97.0, gemini_requests=9)]
        days = daily_series(snaps, [], days=7, now=NOW)
        self.assertEqual(days[0]["peaks"]["claude_five_hour"], 97.0)
        self.assertEqual(days[0]["peaks"]["gemini_requests"], 9)

    def test_dispatches_grouped_by_date_and_agent(self):
        records = [{"ts": NOW - 1000, "agent": "codex"},
                   {"ts": NOW - 900, "agent": "codex"},
                   {"ts": NOW - 800, "agent": "gemini"},
                   {"ts": NOW - DAY, "agent": "claude"}]
        days = daily_series([], records, days=7, now=NOW)
        by_date = {d["date"]: d for d in days}
        self.assertEqual(by_date[_date_of(NOW - 900)]["dispatches"],
                         {"codex": 2, "gemini": 1})
        self.assertEqual(by_date[_date_of(NOW - DAY)]["dispatches"], {"claude": 1})

    def test_old_rows_outside_window_are_dropped(self):
        snaps = [snap(NOW - 30 * DAY, cursor_usd=1.0),
                 snap(NOW - 1000, cursor_usd=2.0)]
        records = [{"ts": NOW - 30 * DAY, "agent": "claude"}]
        days = daily_series(snaps, records, days=7, now=NOW)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["date"], _date_of(NOW - 1000))
        # the pre-window sample still seeds the delta baseline
        self.assertEqual(days[0]["cursor_spend_usd"], 1.0)

    def test_days_sorted_ascending(self):
        records = [{"ts": NOW, "agent": "codex"},
                   {"ts": NOW - 2 * DAY, "agent": "codex"}]
        days = daily_series([], records, days=7, now=NOW)
        self.assertEqual([d["date"] for d in days], sorted(d["date"] for d in days))


if __name__ == "__main__":
    unittest.main()
