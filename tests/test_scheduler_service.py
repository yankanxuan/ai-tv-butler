"""Scheduler service lifecycle and cancellation tests; no device or model access."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from adbtv.scheduler import Scheduler

ROOT = Path(__file__).resolve().parent.parent


class SchedulerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "tasks.json")
        self.scheduler = Scheduler(self.path)

    def worker_document(self, **changes):
        document = {"pid": 999999, "updated": datetime.now(timezone.utc).isoformat(),
                    "status": "idle", "poll_seconds": 20, "worker_id": "test"}
        document.update(changes)
        Path(self.scheduler.worker_path).write_text(json.dumps(document))

    def heartbeat_threads(self):
        return {thread.ident for thread in threading.enumerate()
                if thread.name.startswith("scheduler-heartbeat-")}

    def test_cancelled_once_never_retries_or_keeps_claim(self):
        now = datetime(2026, 11, 11, 6)
        result = {"ok": False, "status": "CANCELLED", "summary": "用户取消"}
        execute = Mock(return_value=result)
        with patch("adbtv.scheduler._now", return_value=now):
            self.scheduler.add("cancelled", "once", delay=0, max_retries=100)
            record = self.scheduler.run_due(execute)[0]
        task = record["task"]
        self.assertFalse(record["ok"])
        self.assertEqual(task["status"], "CANCELLED")
        self.assertFalse(task["enabled"])
        self.assertFalse(task["retry_pending"])
        self.assertIsNone(task["next_run"])
        self.assertEqual(task["last_result"], result)
        self.assertIsNone(task["last_error"])
        self.assertNotIn("claim_token", task)
        self.assertNotIn("lease_until", task)
        with patch("adbtv.scheduler._now", return_value=now + timedelta(days=30)):
            self.assertEqual(self.scheduler.run_due(execute), [])
        execute.assert_called_once_with("cancelled")

    def test_cancelled_recurring_schedule_is_terminal_even_after_lease_expiry(self):
        start = datetime(2026, 11, 11, 6)
        clock = {"now": start}
        def execute(command):
            clock["now"] += timedelta(seconds=10)
            return {"status": "CANCELLED"}
        with patch("adbtv.scheduler._now", side_effect=lambda: clock["now"]):
            self.scheduler.add("recurring", "interval", interval=1, lease_seconds=1)
            clock["now"] += timedelta(seconds=2)
            task = self.scheduler.run_due(execute)[0]["task"]
            self.assertEqual(task["status"], "CANCELLED")
            self.assertFalse(task["enabled"])
            self.assertIsNone(task["next_run"])
            self.assertFalse(task["retry_pending"])
            clock["now"] += timedelta(days=1)
            self.assertEqual(self.scheduler.run_due(execute), [])

    def test_partial_and_failed_keep_bounded_retry(self):
        now = datetime(2026, 11, 11, 6)
        clock = {"now": now}
        with patch("adbtv.scheduler._now", side_effect=lambda: clock["now"]):
            self.scheduler.add("partial", "once", delay=0, max_retries=1)
            record = self.scheduler.run_due(lambda command: {"status": "PARTIAL"})[0]
            self.assertEqual(record["task"]["status"], "FAILED")
            self.assertTrue(record["task"]["retry_pending"])
            clock["now"] += timedelta(seconds=30)
            final = self.scheduler.run_due(lambda command: {"status": "FAILED"})[0]["task"]
            self.assertFalse(final["enabled"])
            self.assertIsNone(final["next_run"])
            self.assertEqual(final["attempts"], 2)

    def test_worker_status_missing_invalid_stale_future_and_stopped(self):
        status = self.scheduler.worker_status()
        self.assertFalse(status["online"])
        self.assertEqual(status["status"], "unknown")
        self.assertFalse(Path(self.scheduler.worker_path).exists())
        for text in ("{broken", "[]", '{"status":"idle"}',
                     '{"status":"idle","poll_seconds":NaN,"updated":"2026-01-01"}'):
            Path(self.scheduler.worker_path).write_text(text)
            self.assertFalse(self.scheduler.worker_status()["online"])
        self.worker_document(updated=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        stale = self.scheduler.worker_status()
        self.assertFalse(stale["online"])
        self.assertEqual(stale["status"], "unknown")
        self.assertIn("过期", stale["reason"])
        self.worker_document(updated=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        self.assertFalse(self.scheduler.worker_status()["online"])
        self.worker_document(status="stopped")
        stopped = self.scheduler.worker_status()
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(stopped["online"])

    def test_fresh_heartbeat_never_checks_pid_namespace(self):
        self.worker_document(status="executing", pid=99999999)
        with patch("os.kill", side_effect=AssertionError("PID must not be inspected")):
            status = self.scheduler.worker_status()
        self.assertTrue(status["online"])
        self.assertEqual(status["status"], "executing")
        self.assertEqual(status["pid"], 99999999)
        self.assertEqual(status["stale_after"], 60)

    def test_loop_normal_shutdown_joins_heartbeat_and_writes_stopped(self):
        before = self.heartbeat_threads()
        with patch("builtins.print"):
            self.scheduler.loop(Mock(), poll_seconds=0.1, stop_flag=lambda: True)
        self.assertEqual(self.heartbeat_threads(), before)
        stopped = self.scheduler.worker_status()
        self.assertFalse(stopped["online"])
        self.assertEqual(stopped["status"], "stopped")
        document = json.loads(Path(self.scheduler.worker_path).read_text())
        self.assertEqual(document["poll_seconds"], 0.1)
        self.assertIn("pid", document)
        self.assertTrue(datetime.fromisoformat(document["updated"]).tzinfo)
        self.assertFalse(list(Path(self.temp.name).glob(".worker-*.tmp")))

    def test_long_callback_keeps_heartbeat_and_storage_updates_unblocked(self):
        task = self.scheduler.add("long", "once", delay=0)
        before = self.heartbeat_threads()
        entered = threading.Event()
        heartbeat_during_callback = threading.Event()
        release = threading.Event()
        failures = []
        publications = []
        original_write = self.scheduler._write_worker
        def observe(status, poll, worker_id):
            original_write(status, poll, worker_id)
            publications.append((status, json.loads(Path(self.scheduler.worker_path).read_text())["updated"]))
            if status == "executing" and entered.is_set():
                heartbeat_during_callback.set()
        def execute(command):
            entered.set()
            if not release.wait(5):
                raise AssertionError("callback release timeout")
            return {"ok": True}
        def run():
            try:
                self.scheduler.loop(execute, poll_seconds=0.1, stop_flag=lambda: True)
            except BaseException as exc:
                failures.append(exc)
        with patch.object(self.scheduler, "_write_worker", side_effect=observe), patch("builtins.print"):
            runner = threading.Thread(target=run)
            runner.start()
            try:
                self.assertTrue(entered.wait(5))
                first = self.scheduler.worker_status()
                self.assertTrue(first["online"])
                self.assertEqual(first["status"], "executing")
                self.assertTrue(heartbeat_during_callback.wait(5))
                later = self.scheduler.worker_status()
                initial_update = next(updated for status, updated in publications if status == "executing")
                self.assertGreater(datetime.fromisoformat(later["updated"]), datetime.fromisoformat(initial_update))
                # This uses the task store lock while the callback is held open.
                other = Scheduler(self.path)
                other.add("concurrent", "once", delay=120)
                other.update(task["id"], enabled=False, command="next occurrence")
                self.assertTrue(runner.is_alive())
            finally:
                release.set()
                runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.heartbeat_threads(), before)
        self.assertEqual(self.scheduler.worker_status()["status"], "stopped")
        latest = Scheduler(self.path).tasks
        self.assertEqual(len(latest), 2)
        done = next(item for item in latest if item["id"] == task["id"])
        self.assertEqual(done["command"], "next occurrence")
        self.assertEqual(done["status"], "SUCCEEDED")
        self.assertFalse(done["enabled"])

    def test_loop_callback_control_exception_stops_and_reaps_heartbeat(self):
        self.scheduler.add("interrupt", "once", delay=0)
        before = self.heartbeat_threads()
        with self.assertRaises(KeyboardInterrupt):
            self.scheduler.loop(Mock(side_effect=KeyboardInterrupt()), poll_seconds=0.1)
        self.assertEqual(self.heartbeat_threads(), before)
        self.assertEqual(self.scheduler.worker_status()["status"], "stopped")

    def test_loop_storage_exception_stops_and_reaps_heartbeat(self):
        before = self.heartbeat_threads()
        with patch.object(self.scheduler, "run_due", side_effect=RuntimeError("corrupt")):
            with self.assertRaisesRegex(RuntimeError, "corrupt"):
                self.scheduler.loop(Mock(), poll_seconds=0.1)
        self.assertEqual(self.heartbeat_threads(), before)
        self.assertEqual(self.scheduler.worker_status()["status"], "stopped")

    def test_update_reloads_and_merges_stale_instances(self):
        first = self.scheduler.add("old", "once", delay=60)
        stale = Scheduler(self.path)
        second = self.scheduler.add("new", "once", delay=120)
        updated = stale.update(first["id"], enabled=False)
        self.assertFalse(updated["enabled"])
        changed = self.scheduler.update(first["id"], command="edited")
        self.assertEqual(changed["command"], "edited")
        self.assertFalse(changed["enabled"])
        current = Scheduler(self.path).tasks
        self.assertEqual({task["id"] for task in current}, {first["id"], second["id"]})
        self.assertEqual(self.scheduler.update("missing", enabled=True), None)

    def test_disabling_running_schedule_does_not_cancel_claim_or_current_command(self):
        now = datetime(2026, 11, 11, 6)
        clock = {"now": now}
        called = []
        def execute(command):
            called.append(command)
            writer = Scheduler(self.path)
            running = writer.tasks[0]
            changed = writer.update(running["id"], command="future command", enabled=False)
            for key in ("id", "status", "attempts", "claim_token", "lease_until"):
                self.assertEqual(changed[key], running[key])
            return {"ok": True}
        with patch("adbtv.scheduler._now", side_effect=lambda: clock["now"]):
            self.scheduler.add("original command", "interval", interval=1)
            clock["now"] += timedelta(seconds=2)
            record = self.scheduler.run_due(execute)[0]
            self.assertTrue(record["ok"])
            self.assertEqual(called, ["original command"])
            self.assertEqual(record["task"]["command"], "future command")
            self.assertFalse(record["task"]["enabled"])
            clock["now"] += timedelta(days=1)
            self.assertEqual(self.scheduler.run_due(execute), [])

    def test_update_validation_leaves_store_untouched(self):
        task = self.scheduler.add("original", "once", delay=60)
        before = Path(self.path).read_bytes()
        for kwargs in ({"enabled": 1}, {"enabled": None}, {"enabled": "false"},
                       {"command": ""}, {"command": "  "}, {"command": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.scheduler.update(task["id"], **kwargs)
            self.assertEqual(Path(self.path).read_bytes(), before)
        with self.assertRaises(TypeError):
            self.scheduler.update(task["id"], interval=10)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_idle_loop_publishes_repeated_heartbeat_until_stop(self):
        before = self.heartbeat_threads()
        repeated = threading.Event()
        stop = threading.Event()
        idle_writes = []
        original_write = self.scheduler._write_worker
        failures = []
        def write(status, poll, worker_id):
            original_write(status, poll, worker_id)
            if status == "idle":
                idle_writes.append(worker_id)
                if len(idle_writes) >= 2:
                    repeated.set()
        def run():
            try:
                self.scheduler.loop(Mock(), poll_seconds=0.1, stop_flag=stop.is_set)
            except BaseException as exc:
                failures.append(exc)
        with patch.object(self.scheduler, "_write_worker", side_effect=write):
            runner = threading.Thread(target=run)
            runner.start()
            try:
                self.assertTrue(repeated.wait(5))
                self.assertEqual(self.scheduler.worker_status()["status"], "idle")
                self.assertTrue(self.scheduler.worker_status()["online"])
            finally:
                stop.set()
                runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(set(idle_writes)), 1)
        self.assertEqual(self.scheduler.worker_status()["status"], "stopped")
        self.assertEqual(self.heartbeat_threads(), before)

    def test_heartbeat_write_failure_exits_loop_and_reaps_thread(self):
        before = self.heartbeat_threads()
        original_write = self.scheduler._write_worker
        writes = []
        def write(status, poll, worker_id):
            writes.append(status)
            if status == "idle" and writes.count("idle") >= 2:
                raise OSError("heartbeat disk failure")
            original_write(status, poll, worker_id)
        with patch.object(self.scheduler, "_write_worker", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "心跳写入失败"):
                self.scheduler.loop(Mock(), poll_seconds=0.1)
        self.assertEqual(self.heartbeat_threads(), before)
        self.assertEqual(self.scheduler.worker_status()["status"], "stopped")

    def test_simultaneous_update_merges_distinct_fields(self):
        task = self.scheduler.add("original", "once", delay=60)
        schedulers = [Scheduler(self.path), Scheduler(self.path)]
        barrier = threading.Barrier(3)
        failures = []
        def update(scheduler, changes):
            try:
                barrier.wait(timeout=5)
                scheduler.update(task["id"], **changes)
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=update, args=(schedulers[0], {"enabled": False})),
                   threading.Thread(target=update, args=(schedulers[1], {"command": "edited"}))]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        latest = Scheduler(self.path).tasks[0]
        self.assertEqual(latest["command"], "edited")
        self.assertFalse(latest["enabled"])
        self.assertEqual(latest["id"], task["id"])

    def test_scheduler_health_uses_freshness_and_stopped_is_failure(self):
        spec = importlib.util.spec_from_file_location("scheduler_health", ROOT / "scripts/healthcheck.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "load_config", return_value={"scheduler": {"store": self.path}}):
            with self.assertRaises(SystemExit):
                module.main("scheduler")
            self.worker_document(status="executing")
            module.main("scheduler")
            self.worker_document(status="stopped")
            with self.assertRaises(SystemExit):
                module.main("scheduler")
            self.worker_document(updated=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
            with self.assertRaises(SystemExit):
                module.main("scheduler")

    def test_invalid_poll_seconds_start_no_heartbeat(self):
        for poll in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(poll=poll), self.assertRaises(ValueError):
                self.scheduler.loop(Mock(), poll_seconds=poll)
        self.assertFalse(Path(self.scheduler.worker_path).exists())


if __name__ == "__main__":
    unittest.main()
