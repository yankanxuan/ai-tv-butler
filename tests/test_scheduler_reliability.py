#!/usr/bin/env python3
"""Scheduler persistence/concurrency regressions; no ADB, model, or network."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from multiprocessing.connection import wait
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adbtv.scheduler import Scheduler, _cron_match, _match_field, _next_cron


def _add_worker(path, connection, prefix):
    scheduler = Scheduler(path)
    connection.send("ready")
    connection.recv()
    for index in range(5):
        scheduler.add(f"{prefix}-{index}", "once", delay=0)
    connection.send("done")
    connection.close()


def _run_worker(path, connection):
    scheduler = Scheduler(path)
    connection.send("ready")
    connection.recv()
    def execute(command):
        connection.send(("execute", command))
        if not connection.poll(10):
            raise RuntimeError("test release timeout")
        connection.recv()
        return {"ok": True}
    result = scheduler.run_due(execute)
    connection.send(("finished", len(result)))
    connection.close()


class SchedulerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitv_scheduler_")
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "tasks.json")
        self.now = datetime(2026, 6, 1, 12, 0, 0)
        self.clock = patch("adbtv.scheduler._now", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.scheduler = Scheduler(self.path)

    def cleanup_process(self, process):
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    def test_stale_instances_add_and_remove_without_lost_updates(self):
        second = Scheduler(self.path)
        first_task = self.scheduler.add("first", "once", delay=0)
        second_task = second.add("second", "once", delay=0)
        self.assertTrue(self.scheduler.remove(first_task["id"]))
        self.assertFalse(second.remove("missing"))
        self.assertEqual([task["id"] for task in second.tasks], [second_task["id"]])

    def test_stale_save_does_not_overwrite_new_or_completed_tasks(self):
        first = self.scheduler.add("first", "once", delay=0)
        stale = Scheduler(self.path)
        self.scheduler.add("second", "once", delay=60)
        self.scheduler.run_due(lambda command: {"ok": True})
        stale.save()
        latest = Scheduler(self.path).tasks
        self.assertEqual(len(latest), 2)
        done = next(task for task in latest if task["id"] == first["id"])
        self.assertFalse(done["enabled"])
        self.assertEqual(done["status"], "SUCCEEDED")

    def test_save_merges_distinct_field_edits_and_rejects_conflict(self):
        self.scheduler.add("first", "once", delay=0)
        other = Scheduler(self.path)
        self.scheduler.tasks[0]["command"] = "updated"
        self.scheduler.save()
        other.tasks[0]["enabled"] = False
        other.save()
        self.assertEqual(other.tasks[0]["command"], "updated")
        self.assertFalse(other.tasks[0]["enabled"])
        stale = Scheduler(self.path)
        other.tasks[0]["command"] = "newer"
        other.save()
        stale.tasks[0]["command"] = "old conflict"
        with self.assertRaisesRegex(ValueError, "冲突"):
            stale.save()
        self.assertEqual(Scheduler(self.path).tasks[0]["command"], "newer")

    def test_parallel_process_adds_preserve_all_tasks(self):
        context = multiprocessing.get_context("fork")
        pipes = [context.Pipe() for _ in range(3)]
        workers = [context.Process(target=_add_worker, args=(self.path, child, f"worker-{i}"))
                   for i, (_, child) in enumerate(pipes)]
        for worker in workers:
            worker.start()
            self.addCleanup(self.cleanup_process, worker)
        for parent, child in pipes:
            child.close()
            self.addCleanup(parent.close)
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "ready")
        for parent, _ in pipes:
            parent.send("start")
        for parent, _ in pipes:
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "done")
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)
        tasks = Scheduler(self.path).tasks
        self.assertEqual(len(tasks), 15)
        self.assertEqual(len({task["command"] for task in tasks}), 15)
        self.assertFalse(list(Path(self.temp.name).glob(".tasks-*.tmp")))

    def test_two_process_runners_execute_one_claim_once(self):
        # Workers use wall time; this task is deliberately far in the past.
        self.scheduler.add("one execution", "once", when="2000-01-01 00:00:00")
        context = multiprocessing.get_context("fork")
        pipes = [context.Pipe() for _ in range(2)]
        workers = [context.Process(target=_run_worker, args=(self.path, child)) for _, child in pipes]
        for worker in workers:
            worker.start()
            self.addCleanup(self.cleanup_process, worker)
        parents = [parent for parent, _ in pipes]
        for parent, child in pipes:
            child.close()
            self.addCleanup(parent.close)
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "ready")
        for parent in parents:
            parent.send("start")
        received = []
        executing = None
        available = list(parents)
        while len(received) < 2:
            ready = wait(available, timeout=10)
            self.assertTrue(ready)
            for connection in ready:
                message = connection.recv()
                received.append(message)
                available.remove(connection)
                if message[0] == "execute":
                    executing = connection
        self.assertCountEqual(received, [("execute", "one execution"), ("finished", 0)])
        # A storage mutation succeeds while the callback is blocked: no file lock
        # or redundant device lease is held around user code.
        self.scheduler.add("during callback", "once", delay=120)
        executing.send("release")
        self.assertTrue(executing.poll(10))
        received.append(executing.recv())
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(received.count(("execute", "one execution")), 1)
        self.assertIn(("finished", 1), received)
        latest = Scheduler(self.path).tasks
        self.assertEqual(len(latest), 2)
        self.assertFalse(next(task for task in latest if task["command"] == "one execution")["enabled"])

    def test_callback_mutations_are_merged_and_removed_task_not_resurrected(self):
        task = self.scheduler.add("first", "once", delay=0)
        def execute(command):
            writer = Scheduler(self.path)
            writer.add("added", "once", delay=20)
            writer.remove(task["id"])
            return {"ok": True}
        result = self.scheduler.run_due(execute)
        self.assertFalse(result[0]["ok"])
        self.assertIn("已删除", result[0]["error"])
        self.assertEqual([task["command"] for task in Scheduler(self.path).tasks], ["added"])

    def test_returned_failure_retries_with_backoff_and_bounded_exhaustion(self):
        self.scheduler.add("fails", "once", delay=0)
        execute = Mock(return_value={"ok": False, "summary": "not playing"})
        first = self.scheduler.run_due(execute)
        self.assertFalse(first[0]["ok"])
        task = self.scheduler.tasks[0]
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(task["max_retries"], 2)
        self.assertEqual(task["last_error"], "not playing")
        self.assertEqual(task["last_result"], execute.return_value)
        self.assertTrue(task["enabled"])
        self.assertEqual(self.scheduler.run_due(execute), [])
        self.now += timedelta(seconds=30)
        self.assertFalse(self.scheduler.run_due(execute)[0]["ok"])
        self.now += timedelta(seconds=60)
        self.assertFalse(self.scheduler.run_due(execute)[0]["ok"])
        task = self.scheduler.tasks[0]
        self.assertEqual(task["attempts"], 3)
        self.assertFalse(task["enabled"])
        self.assertIsNone(task["next_run"])
        self.now += timedelta(days=1)
        self.assertEqual(self.scheduler.run_due(execute), [])
        self.assertEqual(execute.call_count, 3)

    def test_exceptions_and_false_are_failures_successful_retry_disables_once(self):
        self.scheduler.add("retry", "once", delay=0)
        execute = Mock(side_effect=[RuntimeError("offline"), False, {"ok": True}])
        self.assertFalse(self.scheduler.run_due(execute)[0]["ok"])
        self.assertEqual(self.scheduler.tasks[0]["last_error"], "offline")
        self.now += timedelta(seconds=30)
        self.assertFalse(self.scheduler.run_due(execute)[0]["ok"])
        self.now += timedelta(seconds=60)
        self.assertTrue(self.scheduler.run_due(execute)[0]["ok"])
        task = self.scheduler.tasks[0]
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertIsNone(task["last_error"])
        self.assertFalse(task["enabled"])

    def test_recurring_failures_advance_only_after_retries_exhausted(self):
        self.scheduler.add("recurring", "interval", interval=120, max_retries=0)
        self.now += timedelta(seconds=120)
        self.assertFalse(self.scheduler.run_due(lambda command: {"ok": False})[0]["ok"])
        task = self.scheduler.tasks[0]
        self.assertEqual(task["status"], "FAILED")
        self.assertTrue(task["enabled"])
        self.assertFalse(task["retry_pending"])
        self.assertEqual(datetime.fromisoformat(task["next_run"]), self.now + timedelta(seconds=120))
        self.now += timedelta(seconds=120)
        self.assertTrue(self.scheduler.run_due(lambda command: {"ok": True})[0]["ok"])
        self.assertEqual(self.scheduler.tasks[0]["attempts"], 1)

    def test_success_does_not_immediately_run_again(self):
        self.scheduler.add("once", "once", delay=0)
        self.scheduler.add("interval", "interval", interval=10)
        self.now += timedelta(seconds=10)
        execute = Mock(return_value={"ok": True})
        self.assertEqual(len(self.scheduler.run_due(execute)), 2)
        self.assertEqual(self.scheduler.run_due(execute), [])
        self.assertEqual(execute.call_count, 2)

    def test_expired_crash_claim_records_failure_before_delayed_retry(self):
        task = self.scheduler.add("crash", "once", delay=0, lease_seconds=10)
        claimed = self.scheduler._claim(task["id"])
        self.assertEqual(claimed["status"], "RUNNING")
        self.assertEqual(Scheduler(self.path).due(), [])
        self.now += timedelta(seconds=11)
        recovered = Scheduler(self.path)
        self.assertEqual(recovered.due(), [])
        task = recovered.tasks[0]
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["lease_expirations"], 1)
        self.assertIn("结果未知", task["last_error"])
        self.assertNotIn("claim_token", task)
        self.now += timedelta(seconds=30)
        self.assertTrue(recovered.run_due(lambda command: {"ok": True})[0]["ok"])
        self.assertEqual(recovered.tasks[0]["attempts"], 2)

    def test_expired_worker_cannot_commit_success(self):
        self.scheduler.add("late", "once", delay=0, lease_seconds=1, max_retries=0)
        def late(command):
            self.now += timedelta(seconds=2)
            return {"ok": True}
        result = self.scheduler.run_due(late)
        self.assertFalse(result[0]["ok"])
        self.assertEqual(self.scheduler.tasks[0]["status"], "FAILED")
        self.assertFalse(self.scheduler.tasks[0]["enabled"])

    def test_control_baseexceptions_propagate_and_claim_can_recover(self):
        self.scheduler.add("cancel", "once", delay=0)
        class Cancelled(BaseException):
            pass
        with self.assertRaises(Cancelled):
            self.scheduler.run_due(Mock(side_effect=Cancelled()))
        self.assertEqual(Scheduler(self.path).tasks[0]["status"], "RUNNING")

    def test_corrupt_json_and_task_shapes_are_not_silently_cleared(self):
        for contents in ("{broken", "[]", '{"tasks":{}}', '{"tasks":[{}]}',
                         '{"tasks":[],"value":NaN}'):
            Path(self.path).write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                Scheduler(self.path)
            self.assertEqual(Path(self.path).read_text(), contents)
            with self.assertRaises(ValueError):
                self.scheduler.add("must not overwrite", "once", delay=0)
            self.assertEqual(Path(self.path).read_text(), contents)

    def test_legacy_json_and_unknown_metadata_are_preserved(self):
        legacy = {"format_version": "legacy", "tasks": [{"id": "old", "command": "legacy",
                  "kind": "once", "enabled": True, "next_run": "2000-01-01T00:00:00",
                  "last_run": None, "custom": "preserve"}]}
        Path(self.path).write_text(json.dumps(legacy))
        scheduler = Scheduler(self.path)
        self.assertTrue(scheduler.run_due(lambda command: {"ok": True})[0]["ok"])
        result = json.loads(Path(self.path).read_text())
        self.assertEqual(result["format_version"], "legacy")
        self.assertEqual(result["tasks"][0]["custom"], "preserve")
        self.assertFalse(result["tasks"][0]["enabled"])

    def test_invalid_add_ranges_rejected_without_writing(self):
        for kwargs in ({"kind": "interval", "interval": 0}, {"kind": "interval", "interval": -1},
                       {"kind": "interval", "interval": True}, {"kind": "once", "delay": -1},
                       {"kind": "once", "delay": 0, "max_retries": -1},
                       {"kind": "once", "delay": 0, "lease_seconds": 0},
                       {"kind": "once", "delay": 0, "timezone": "Unknown/Nowhere"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.scheduler.add("invalid", **kwargs)
        self.assertEqual(self.scheduler.tasks, [])
        self.assertFalse(Path(self.path).exists())

    def test_cron_rejects_out_of_range_zero_negative_steps_and_malformed(self):
        for expr in ("60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8",
                     "*/0 * * * *", "*/-1 * * * *", "5-2 * * * *", "1,,2 * * * *",
                     "-1 * * * *", "* * * *", "* * * * * *", "foo * * * *"):
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                self.scheduler.add("bad cron", "cron", cron=expr)

    def test_cron_weekday_seven_and_vixie_dom_dow_or(self):
        sunday = datetime(2026, 6, 7, 8, 0)
        self.assertTrue(_cron_match("0 8 * * 7", sunday))
        self.assertTrue(_cron_match("0 8 * * 0", sunday))
        self.assertTrue(_cron_match("0 8 * * 6-7", sunday))
        self.assertTrue(_cron_match("0 8 1 * 7", sunday))
        self.assertTrue(_cron_match("0 8 1 * 7", datetime(2026, 6, 1, 8)))
        self.assertFalse(_cron_match("0 8 1 * 7", datetime(2026, 6, 2, 8)))
        self.assertFalse(_cron_match("0 8 * * 7", datetime(2026, 6, 1, 8)))
        self.assertTrue(_match_field("5-15/5,30", 10, 0, 59))

    def test_cron_boundaries_leap_year_and_impossible_dates_are_bounded(self):
        self.assertEqual(_next_cron("*/15 * * * *", datetime(2026, 6, 1, 12, 15)), datetime(2026, 6, 1, 12, 30))
        self.assertEqual(_next_cron("0 0 29 2 *", datetime(2025, 3, 1)), datetime(2028, 2, 29))
        self.assertEqual(_next_cron("0 0 29 2 *", datetime(2096, 3, 1)), datetime(2104, 2, 29))
        with self.assertRaisesRegex(ValueError, "八年"):
            _next_cron("0 0 30 2 *", self.now)

    def test_explicit_timezone_schedule_due_and_failure_retry(self):
        self.now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        task = self.scheduler.add("Shanghai", "cron", cron="1 8 * * *", timezone="Asia/Shanghai")
        self.assertEqual(task["next_run"], "2026-06-01T08:01:00+08:00")
        self.assertEqual(self.scheduler.due(), [])
        self.now += timedelta(minutes=1)
        failed = self.scheduler.run_due(lambda command: {"ok": False})
        self.assertFalse(failed[0]["ok"])
        self.assertEqual(self.scheduler.tasks[0]["next_run"], "2026-06-01T08:01:30+08:00")
        self.assertEqual(self.scheduler.run_due(lambda command: {"ok": True}), [])
        self.now += timedelta(seconds=30)
        self.assertTrue(self.scheduler.run_due(lambda command: {"ok": True})[0]["ok"])
        self.assertEqual(self.scheduler.tasks[0]["next_run"], "2026-06-02T08:01:00+08:00")

    def test_timezone_once_naive_wall_time_and_local_default_compatibility(self):
        named = self.scheduler.add("named", "once", when="2026-06-02 08:00", timezone="Asia/Shanghai")
        local = self.scheduler.add("local", "once", delay=0)
        self.assertEqual(named["next_run"], "2026-06-02T08:00:00+08:00")
        self.assertEqual(local["timezone"], "local")
        self.assertEqual(local["next_run"], self.now.isoformat(timespec="seconds"))

    def test_dst_gap_skipped_and_repeated_wall_time_fires_once(self):
        zone = ZoneInfo("America/New_York")
        after = datetime(2026, 3, 8, 0, 0, tzinfo=zone)
        self.assertEqual(_next_cron("30 2 * * *", after), datetime(2026, 3, 9, 2, 30, tzinfo=zone))
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        next_run = _next_cron("30 1 * * *", first)
        self.assertEqual(next_run, datetime(2026, 11, 2, 1, 30, tzinfo=zone))

    def test_loop_keeps_existing_callback_and_stop_flag_api(self):
        self.scheduler.add("once", "once", delay=0)
        execute = Mock(return_value={"ok": True})
        with patch("builtins.print"), patch("adbtv.scheduler.time.sleep", side_effect=AssertionError("no sleep")):
            self.scheduler.loop(execute, stop_flag=lambda: True)
        execute.assert_called_once_with("once")


if __name__ == "__main__":
    unittest.main()
