"""Memory integration regressions; isolated SQLite, fake observations, no TV/network.

The model-plan fixture explicitly supplies the _goal boundary: production managed
planning is currently local. These tests do not claim a live-model integration.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from adbtv.agent import Agent, GoalRejected
from adbtv.memory import MemoryStore
from adbtv.perception import ScreenState
from adbtv.verifier import GoalSpec


class DeterministicLLM:
    enabled = True

    def __init__(self):
        self.calls = []

    def chat(self, messages, *, system):
        self.calls.append((messages, system))
        return '{"action":"open","package":"com.example.tv"}'


class MemoryV408Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.cfg = {
            "memory": {"enabled": True, "path": str(self.path), "record_history": True},
            "execution": {"state_dir": str(Path(self.tmp.name) / "runtime")},
            "agent": {"use_llm": False},
            "apps": {"example": {"package": "com.example.tv"}},
        }
        # Spec-limited mocks cannot accidentally dispatch to any real device.
        self.device = Mock(spec=["adb", "current_package", "launch_app", "key"])
        self.device.adb = Mock(spec=["checkpoint"])
        self.device.current_package.return_value = "com.example.tv"
        self.toolbox = Mock(spec=["call", "describe", "names"])
        self.llm = Mock(spec=["enabled", "chat"], enabled=False)
        self.agent = Agent(self.device, self.toolbox, self.llm, self.cfg)
        self.state = ScreenState(app="com.example.tv", activity="MainActivity")
        observation = patch("adbtv.agent.Agent._observe_goal", return_value=self.state)
        observation.start()
        self.addCleanup(observation.stop)

    def rows(self, table):
        # Query actual SQLite independently of MemoryStore's history/stats APIs.
        if table not in ("tasks", "paths", "preferences"):
            raise ValueError(table)
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    def assert_no_tv_actions(self):
        self.device.launch_app.assert_not_called()
        self.device.key.assert_not_called()
        self.toolbox.call.assert_not_called()

    def test_offline_tasks_persist_without_any_memory_warning(self):
        for task_id in ("offline1", "offline2"):
            result = self.agent.run("当前应用是什么", task_id=task_id)
            self.assertTrue(result["ok"], result)
            self.assertEqual((result["status"], result["mode"]), ("SUCCESS", "offline"))
            self.assertNotIn("memory_warning", result)
        tasks = self.rows("tasks")
        self.assertEqual({row["id"] for row in tasks}, {"offline1", "offline2"})
        self.assertTrue(all(row["status"] == "SUCCESS" for row in tasks))
        for row in tasks:
            persisted = json.loads(row["result_json"])
            self.assertTrue(persisted["ok"])
            self.assertNotIn("memory_warning", persisted)
        paths = self.rows("paths")
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0]["attempts"], paths[0]["successes"], paths[0]["failures"]), (2, 2, 0))
        self.assertEqual(paths[0]["package"], "")
        self.llm.chat.assert_not_called()
        self.assert_no_tv_actions()

    def test_fixture_model_plan_persists_verified_success(self):
        fixture = DeterministicLLM()
        self.agent.llm = fixture
        self.cfg["agent"]["use_llm"] = True
        self.cfg["memory"]["store_context"] = True

        def fixture_goal(instruction):
            reply = fixture.chat([{"role": "user", "content": instruction}], system="goal fixture")
            return GoalSpec(**json.loads(reply))

        with patch.object(self.agent, "_goal", side_effect=fixture_goal):
            result = self.agent.run("fixture model open request", task_id="model1")
        self.assertEqual(len(fixture.calls), 1)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["status"], result["mode"]), ("SUCCESS", "managed"))
        self.assertNotIn("memory_warning", result)
        tasks, paths = self.rows("tasks"), self.rows("paths")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "SUCCESS")
        self.assertTrue(json.loads(tasks[0]["result_json"])["ok"])
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0]["package"], paths[0]["successes"], paths[0]["failures"]),
                         ("com.example.tv", 1, 0))
        preference = json.loads(self.rows("preferences")[0]["value_json"])
        self.assertEqual((preference["session"], preference["package"]), ("model1", "com.example.tv"))
        self.assert_no_tv_actions()

    def test_rejected_goal_records_task_without_path_or_warning(self):
        with patch.object(self.agent, "_goal", side_effect=GoalRejected("missing exact title")):
            result = self.agent.run("播放《missing》", task_id="rejected1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("memory_warning", result)
        tasks = self.rows("tasks")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "UNKNOWN")
        self.assertFalse(json.loads(tasks[0]["result_json"])["ok"])
        self.assertEqual(self.rows("paths"), [])
        self.assert_no_tv_actions()

    def test_unknown_goal_records_no_executed_path(self):
        result = self.agent.run("帮我订披萨", task_id="unknown1")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("memory_warning", result)
        self.assertEqual(len(self.rows("tasks")), 1)
        self.assertEqual(self.rows("paths"), [])
        self.assert_no_tv_actions()

    def test_failed_execution_records_failed_path_not_verified_success(self):
        with patch("adbtv.skill_engine.SkillEngine.execute", side_effect=ValueError("fixture failure")), \
                patch.object(self.agent, "_execute_with_recovery", side_effect=lambda action, *_: action()):
            result = self.agent.run("打开example", task_id="failed1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("memory_warning", result)
        self.assertEqual(self.rows("tasks")[0]["status"], "FAILED")
        paths = self.rows("paths")
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0]["attempts"], paths[0]["successes"], paths[0]["failures"]), (1, 0, 1))
        self.assertIsNone(MemoryStore(self.path).best_path(paths[0]["key"]))
        self.assert_no_tv_actions()

    def test_unwritable_initialization_is_visible_and_task_still_succeeds(self):
        # Inject denial for this DB only; task-control runtime files stay writable.
        original_open = os.open

        def deny_database(path, *args, **kwargs):
            if str(path) == str(self.path):
                raise PermissionError("fixture database unwritable")
            return original_open(path, *args, **kwargs)

        with patch("adbtv.memory.os.open", side_effect=deny_database):
            result = self.agent.run("当前应用是什么", task_id="denied1")
        self.assertTrue(result["ok"], result)
        self.assertIn("unwritable", result["memory_warning"])
        self.assertFalse(self.path.exists())
        self.assert_no_tv_actions()

    def test_readonly_write_failure_is_visible_and_does_not_create_rows(self):
        MemoryStore(self.path)
        original_connect = MemoryStore._connect
        original_record = MemoryStore.record_task

        def readonly_connect(store):
            db = original_connect(store)
            db.execute("PRAGMA query_only=ON")
            return db

        def readonly_record(store, task, result):
            with patch.object(MemoryStore, "_connect", readonly_connect):
                return original_record(store, task, result)

        # Initialization needs its write transaction; deny only the actual writes.
        with patch.object(MemoryStore, "record_task", readonly_record):
            result = self.agent.run("当前应用是什么", task_id="readonly1")
        self.assertTrue(result["ok"], result)
        self.assertIn("readonly", result["memory_warning"].lower())
        self.assertEqual(self.rows("tasks"), [])
        self.assertEqual(self.rows("paths"), [])
        self.assert_no_tv_actions()

    def test_corruption_preserves_file_and_valid_backup_restores_persistence(self):
        self.agent.run("当前应用是什么", task_id="before1")
        backup = Path(self.tmp.name) / "valid-backup.sqlite3"
        MemoryStore(self.path).backup(backup)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        # Every MemoryStore operation has closed its connection; no active WAL.
        corrupt = b"fixture invalid SQLite database\x00" * 32
        self.path.write_bytes(corrupt)
        result = self.agent.run("当前应用是什么", task_id="corrupt1")
        self.assertTrue(result["ok"], result)
        self.assertIn("database", result["memory_warning"].lower())
        self.assertEqual(self.path.read_bytes(), corrupt)
        with self.assertRaises(sqlite3.DatabaseError):
            MemoryStore(self.path)
        # Restore explicitly into the known isolated test DB (no automatic reset).
        shutil.copyfile(backup, self.path)
        recovered = self.agent.run("当前应用是什么", task_id="after1")
        self.assertTrue(recovered["ok"], recovered)
        self.assertNotIn("memory_warning", recovered)
        tasks, paths = self.rows("tasks"), self.rows("paths")
        self.assertEqual({row["id"] for row in tasks}, {"before1", "after1"})
        self.assertTrue(all(row["status"] == "SUCCESS" for row in tasks))
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0]["attempts"], paths[0]["successes"], paths[0]["failures"]), (2, 2, 0))
        self.assert_no_tv_actions()


if __name__ == "__main__":
    unittest.main()
