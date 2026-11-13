"""SQLite persistence and reviewed Skill regression tests."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.memory import MemoryStore, SchemaVersionError
from adbtv.skill_engine import SkillEngine
from adbtv.perception import ScreenState
from adbtv.verifier import GoalSpec
from adbtv.tools import ToolBox


class MemorySkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/"memory.sqlite3")
        self.memory = MemoryStore(self.path)
        self.cfg = {"apps": {"feiniu_tv": {"package": "com.trim.tv"},
                             "bilibili": {"package": "com.xiaodianshi.tv.yst"}}}
        self.engine = SkillEngine(self.memory, self.cfg)

    def test_task_history_reopen_idempotency_and_metrics(self):
        task = {"id": "task1", "instruction": "打开飞牛", "started": time.time()}
        result = {"status": "SUCCESS", "ok": True, "elapsed": 1.2,
                  "steps": [{"tool": "tv_launch_app"}], "llm_calls": 0}
        self.memory.record_task(task, result)
        self.memory.record_task(task, result)
        rows = MemoryStore(self.path).history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "SUCCESS")
        self.assertEqual(rows[0]["result"]["elapsed"], 1.2)
        stats = self.memory.stats()
        self.assertEqual(stats["total_tasks"], 1)
        self.assertEqual(stats["successes"], 1)

    def test_no_passwords_raw_images_or_raw_ui_persisted(self):
        self.memory.record_task({"id": "private", "instruction": "password=secret", "token": "abc"},
                                {"status": "FAILED", "screenshot_bytes": b"image", "xml": "<node/>",
                                 "steps": [{"tool": "tv_input_text", "args": {"text": "private-search"}}]})
        row = self.memory.history()[0]
        self.assertEqual(row["task"]["instruction"], "[REDACTED]")
        self.assertEqual(row["task"]["token"], "[REDACTED]")
        self.assertEqual(row["result"]["screenshot_bytes"], "[OMITTED]")
        self.assertEqual(row["result"]["steps"][0]["args"]["text"], "[REDACTED]")
        self.assertNotIn("private-search", Path(self.path).read_bytes().decode("latin1", "ignore"))

    def test_preference_requires_explicit_user_choice(self):
        with self.assertRaises(ValueError):
            self.memory.set_preference("app", "feiniu")
        self.memory.set_preference("app", "feiniu", explicit=True)
        self.assertEqual(self.memory.preferences(), {"app": "feiniu"})

    def test_reliable_path_requires_verified_success(self):
        self.memory.remember_path("goal", "com.trim.tv", [{"skill_id": "a"}], verified=False)
        self.assertIsNone(self.memory.best_path("goal"))
        self.memory.remember_path("goal", "com.trim.tv", [{"skill_id": "a"}], verified=True)
        self.memory.remember_path("goal", "com.trim.tv", [{"skill_id": "a"}], verified=True)
        best = self.memory.best_path("goal")
        self.assertEqual(best["successes"], 2)
        self.assertEqual(best["attempts"], 3)

    def test_recovery_pages_and_backup(self):
        self.memory.record_page("com.trim.tv", "abc", {"activity": "MainActivity"})
        self.memory.record_recovery("offline", "reconnect", True)
        self.memory.record_recovery("offline", "reconnect", False)
        self.assertEqual(self.memory.pages()[0]["visits"], 1)
        backup = self.memory.backup(str(Path(self.tmp.name)/"backup.sqlite3"))
        self.assertEqual(MemoryStore(backup).stats()["recovery_methods"][0]["attempts"], 2)

    def test_cleanup_removes_only_terminal_records(self):
        self.memory.record_task({"id": "old", "created_at": 0}, {"status": "SUCCESS"})
        self.memory.record_task({"id": "active", "created_at": 0}, {"status": "RUNNING"})
        deleted = self.memory.cleanup(days=0)
        self.assertEqual(deleted["aged_tasks_deleted"], 1)
        self.assertEqual([row["id"] for row in self.memory.history()], ["active"])

    def test_newer_schema_is_rejected_not_modified(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=999")
        before = Path(self.path).read_bytes()
        with self.assertRaises(SchemaVersionError):
            MemoryStore(self.path)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_migration_backups_and_concurrent_readers(self):
        legacy = str(Path(self.tmp.name)/"legacy.sqlite3")
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE custom(value TEXT)")
            db.execute("PRAGMA user_version=0")
        MemoryStore(legacy).record_task({"id": "t"}, {"status": "SUCCESS"})
        self.assertTrue(list(Path(self.tmp.name).glob("legacy.sqlite3.premigration-*.bak")))
        self.assertEqual(len(list(Path(self.tmp.name).glob("legacy.sqlite3.premigration-*.bak"))), 1)

    def test_skill_cannot_be_executed_or_approved_without_validation(self):
        definition = {"name": "返回桌面", "package": "com.example.tv",
                      "trigger": {"action": "home"}, "steps": [{"tool": "tv_key", "args": {"key": "home"}}],
                      "success": {"action": "open"}}
        sid = self.engine.propose(definition, Mock(validate=Mock(), names=Mock(return_value=["tv_key"])))
        self.assertEqual(self.memory.get_skill(sid)["status"], "draft")
        self.assertFalse(any(row["status"] == "approved" for row in self.memory.list_skills()))
        self.assertEqual(self.engine.select(GoalSpec("home", package="com.example.tv"), "返回桌面", ScreenState())["id"], "builtin.home")
        with self.assertRaises(ValueError):
            self.engine.approve(sid, confirmed=True)
        self.assertEqual(self.engine.select(GoalSpec("home"), "返回桌面", ScreenState())["id"], "builtin.home")

    def test_definition_change_revokes_approval_and_code_is_rejected(self):
        sid = self.memory.propose_skill({"name": "测试", "package": "com.example.tv", "steps": []})
        self.memory.update_skill(sid, status="validated", validation={"ok": True, "environment": "isolated_fake_adb"})
        self.memory.update_skill(sid, status="approved", approval={"explicit": True})
        self.memory.update_skill(sid, definition={"name": "新步骤", "package": "com.example.tv", "steps": []})
        row = self.memory.get_skill(sid)
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["approval"], {})
        with self.assertRaises(ValueError):
            SkillEngine.validate_definition({"name": "恶意", "package": "com.example.tv", "code": "os.system('id')"}, Mock())

    def test_builtin_selection_and_preconditions(self):
        goal = GoalSpec("open", package="com.example.tv")
        state = ScreenState(app="com.example.tv", texts=["首页"])
        definition = {"name": "打开", "package": "com.example.tv",
                      "trigger": {"action": "open", "contains": ["打开"]},
                      "preconditions": {"package": "com.example.tv", "texts": ["首页"]},
                      "steps": [{"tool": "tv_info", "args": {}}]}
        self.assertTrue(self.engine.matches(definition, goal, "打开应用", state))
        self.assertFalse(self.engine.matches(definition, goal, "关闭应用", state))

    def test_long_title_and_episode_constraints_are_not_dropped(self):
        from adbtv.agent import Agent
        agent = Agent(Mock(), Mock(), Mock(), self.cfg)
        goal = agent._goal("在哔哩哔哩播放《ABCDEFGHIJKLMNOPQRSTUVWXYZ》第2季第3集")
        self.assertEqual(goal.title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.assertEqual(goal.season, 2)
        self.assertEqual(goal.episode, 3)
        self.assertEqual(agent._goal("在哔哩哔哩播放《未闭合").action, "unknown")

    def test_adb_checkpoint_is_thread_local(self):
        from adbtv.adb import AdbClient
        import threading
        adb = AdbClient({"adb": {"binary": "adb", "serial": "test"}})
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def hook(args):
            calls.append(args[0])
            if args[0] == "devices":
                entered.set(); release.wait(2)
        adb.checkpoint = hook
        seen = []
        with unittest.mock.patch("adbtv.adb.subprocess.run", side_effect=lambda *a, **k: seen.append(1) or Mock(returncode=0, stdout="", stderr="")):
            t = threading.Thread(target=lambda: adb.devices())
            t.start(); self.assertTrue(entered.wait(1))
            adb.run(["shell", "input keyevent 4"])
            release.set(); t.join(2)
        self.assertEqual(calls, ["devices", "shell"])


if __name__ == "__main__":
    unittest.main()


class PlanKeyHashingTests(unittest.TestCase):
    """`plan` may be a dict (offline) or a GoalSpec (model); hashing must accept both."""

    def test_dict_plan_does_not_emit_asdict_warning(self):
        import copy, tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from adbtv.config import DEFAULTS
        from adbtv.agent import Agent
        cfg = copy.deepcopy(DEFAULTS)
        with tempfile.TemporaryDirectory() as tmp:
            cfg["memory"].update(enabled=True, path=str(Path(tmp) / "m.sqlite3"),
                                 record_history=True)
            cfg["execution"]["state_dir"] = str(Path(tmp) / "runtime")
            device = Mock()
            device.current_package.return_value = "com.trim.tv"
            device.playback_status.return_value = {"playing": False, "state": "PAUSED"}
            device.wake.return_value = {"ok": True, "woke": False}
            toolbox = Mock()
            agent = Agent(device, toolbox, Mock(enabled=False), cfg)
            result = agent.run("当前应用是什么")
            self.assertNotIn("asdict", str(result.get("memory_warning") or ""))
