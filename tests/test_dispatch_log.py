"""Tests for the dispatch log's paired start/end events and active-run detection."""

import json
import os
import tempfile
import unittest
from unittest import mock

from dispatcher import launch


def write_log(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


NOW = 1_800_000_000


def start_event(run_id, ts, agent="codex", pid=1, mode="headless", task="do a thing",
                reason="codex has most headroom", cwd="/repos/proj", forced=False):
    return {"event": "start", "run_id": run_id, "ts": ts, "agent": agent,
            "reason": reason, "task": task, "mode": mode, "forced": forced,
            "cwd": cwd, "pid": pid}


def end_event(run_id, ts, agent="codex", exit_code=0, duration_s=1.0):
    return {"event": "end", "run_id": run_id, "ts": ts, "agent": agent,
            "exit_code": exit_code, "duration_s": duration_s}


class ReadDispatchLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "dispatches.jsonl")
        patcher = mock.patch.object(launch, "DISPATCH_LOG", self.log_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_empty_log_returns_empty_lists(self):
        records, active = launch.read_dispatch_log()
        self.assertEqual(records, [])
        self.assertEqual(active, [])

    def test_pairs_start_and_end_into_finished_record(self):
        write_log(self.log_path, [
            start_event("r1", NOW - 100, agent="codex", pid=1, task="fix bug"),
            end_event("r1", NOW - 40, agent="codex", exit_code=0, duration_s=60.0),
        ])
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(active, [])
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "finished")
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["duration_s"], 60.0)
        self.assertEqual(r["agent"], "codex")
        self.assertEqual(r["task"], "fix bug")
        self.assertEqual(r["run_id"], "r1")

    def test_start_with_no_end_and_alive_pid_is_active(self):
        my_pid = os.getpid()  # guaranteed alive
        write_log(self.log_path, [
            start_event("r1", NOW - 90, pid=my_pid, mode="headless",
                        task="long task", reason="least headroom", cwd="/repos/alpha"),
        ])
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(records, [])
        self.assertEqual(len(active), 1)
        a = active[0]
        self.assertEqual(a["status"], "running")
        self.assertEqual(a["elapsed_s"], 90)
        self.assertEqual(a["started_at"], NOW - 90)
        self.assertEqual(a["repo"], "alpha")
        self.assertEqual(a["pid"], my_pid)

    def test_start_with_no_end_and_dead_pid_headless_is_interrupted(self):
        # PID 1 (init) is technically alive, so use a clearly-invalid PID.
        write_log(self.log_path, [
            start_event("r1", NOW - 10, pid=0x7FFFFFFF, mode="headless"),
        ])
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(active, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "interrupted")
        self.assertIsNone(records[0]["exit_code"])

    def test_start_with_no_end_and_dead_pid_interactive_is_closed(self):
        write_log(self.log_path, [
            start_event("r1", NOW - 10, pid=0x7FFFFFFF, mode="interactive"),
        ])
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(active, [])
        self.assertEqual(records[0]["status"], "closed")
        self.assertEqual(records[0]["mode"], "interactive")

    def test_legacy_single_line_records_still_render(self):
        legacy = {"ts": NOW - 5, "agent": "claude", "reason": "least headroom",
                  "task": "old task", "mode": "headless", "exit_code": 0,
                  "forced": False, "cwd": "/repos/legacy"}
        write_log(self.log_path, [legacy])
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(active, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "legacy")
        self.assertEqual(records[0]["exit_code"], 0)
        self.assertEqual(records[0]["task"], "old task")

    def test_legacy_and_paired_events_coexist(self):
        legacy = {"ts": NOW - 200, "agent": "gemini", "mode": "interactive",
                  "task": "old", "reason": "r", "forced": False,
                  "cwd": "/repos/old", "exit_code": None}
        write_log(self.log_path, [
            legacy,
            start_event("r1", NOW - 100, pid=os.getpid(), task="live"),
            start_event("r2", NOW - 80, pid=0x7FFFFFFF, task="orphaned"),
            start_event("r3", NOW - 60),
            end_event("r3", NOW - 20, exit_code=1, duration_s=40.0),
        ])
        records, active = launch.read_dispatch_log(now=NOW)
        # active: r1 only (alive pid)
        self.assertEqual([a["run_id"] for a in active], ["r1"])
        # records newest-first: r3 (finished), r2 (interrupted), legacy
        self.assertEqual([r.get("run_id") or r["task"] for r in records],
                         ["r3", "r2", "old"])
        finished = records[0]
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["exit_code"], 1)

    def test_running_dispatch_absent_from_records(self):
        write_log(self.log_path, [
            start_event("r1", NOW - 10, pid=os.getpid(), task="live"),
        ])
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(records, [])
        self.assertEqual(len(active), 1)

    def test_malformed_lines_are_ignored(self):
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps(start_event("r1", NOW - 5,
                                           pid=0x7FFFFFFF, mode="headless")) + "\n")
        records, active = launch.read_dispatch_log(now=NOW)
        self.assertEqual(active, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "interrupted")

    def test_records_are_newest_first(self):
        write_log(self.log_path, [
            start_event("r1", NOW - 300), end_event("r1", NOW - 290),
            start_event("r2", NOW - 200), end_event("r2", NOW - 190),
            start_event("r3", NOW - 100), end_event("r3", NOW - 90),
        ])
        records, _ = launch.read_dispatch_log(now=NOW)
        self.assertEqual([r["run_id"] for r in records], ["r3", "r2", "r1"])

    def test_limit_applies_to_records_only(self):
        events = []
        for i in range(5):
            events.append(start_event(f"r{i}", NOW - 1000 + i))
            events.append(end_event(f"r{i}", NOW - 990 + i))
        # one active on top of the finished set
        events.append(start_event("live", NOW - 10, pid=os.getpid()))
        write_log(self.log_path, events)
        records, active = launch.read_dispatch_log(limit=2, now=NOW)
        self.assertEqual(len(records), 2)
        self.assertEqual(len(active), 1)


class LogDispatchStartEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "dispatches.jsonl")
        patcher = mock.patch.object(launch, "DISPATCH_LOG", self.log_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read(self):
        with open(self.log_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_start_writes_expected_shape(self):
        launch.log_dispatch_start(
            "r1", "codex", "least headroom", "  fix\nthe bug  ",
            "headless", forced=False, pid=1234)
        lines = self._read()
        self.assertEqual(len(lines), 1)
        ev = lines[0]
        self.assertEqual(ev["event"], "start")
        self.assertEqual(ev["run_id"], "r1")
        self.assertEqual(ev["agent"], "codex")
        self.assertEqual(ev["mode"], "headless")
        self.assertEqual(ev["pid"], 1234)
        self.assertEqual(ev["task"], "fix the bug")
        self.assertIn("ts", ev)
        self.assertIn("cwd", ev)

    def test_end_writes_expected_shape(self):
        launch.log_dispatch_end("r1", "codex", 0, 12.345)
        lines = self._read()
        self.assertEqual(len(lines), 1)
        ev = lines[0]
        self.assertEqual(ev["event"], "end")
        self.assertEqual(ev["run_id"], "r1")
        self.assertEqual(ev["exit_code"], 0)
        self.assertEqual(ev["duration_s"], 12.345)

    def test_write_failure_is_swallowed(self):
        # Point DISPATCH_LOG at a path we cannot create — must not raise.
        with mock.patch.object(launch, "DISPATCH_LOG", "/proc/does/not/exist/x"):
            launch.log_dispatch_start("r1", "codex", "r", "t", "headless", False, 1)
            launch.log_dispatch_end("r1", "codex", 0, 0.0)


if __name__ == "__main__":
    unittest.main()
