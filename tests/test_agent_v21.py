"""Managed Agent tests: evidence, cancellation and model failure semantics."""
import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

from adbtv.agent import Agent
from adbtv.execution import TaskControl, control_task
from adbtv.llm import LLMError
from adbtv.perception import ScreenState


class ManagedAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"execution": {"state_dir": self.tmp.name, "timeout": 3},
                    "agent": {"fast_path": True, "use_llm": True, "max_steps": 4},
                    "apps": {"hongguo": {"package": "cn.hongguo.tv"}}}
        self.device = Mock()
        self.device.current_focus.return_value = "cn.hongguo.tv/.MainActivity"
        self.llm = Mock(enabled=True, timeout=1, vision_model="")
        self.llm.chat.return_value = '{"done":true,"summary":"我已经播放"}'
        self.toolbox = Mock()
        self.toolbox.describe.return_value = "tv_info"
        self.toolbox.names.return_value = ["tv_info"]
        self.perception = Mock()
        self.perception.observe.return_value = ScreenState(app="cn.hongguo.tv", activity=".MainActivity")
        self.patch = patch("adbtv.perception.Perception", return_value=self.perception)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def agent(self):
        return Agent(self.device, self.toolbox, self.llm, self.cfg)

    def test_model_done_cannot_override_missing_playback_or_content(self):
        result = self.agent().run("打开红果播放山海归途")
        self.assertFalse(result["ok"], result)
        self.assertIn(result["status"], ("PARTIAL", "UNKNOWN", "FAILED"))
        self.toolbox.call.assert_not_called()
        self.assertIn("verification", result)

    def test_model_json_failure_has_bounded_recovery(self):
        # A model that never returns valid JSON must fail after a bounded number
        # of attempts, not loop on the whole task budget.
        self.llm.chat.return_value = "[]"
        with self.assertRaises(LLMError):
            self.agent()._llm_run("打开红果播放山海归途")
        self.assertEqual(self.llm.chat.call_count, 3)

    def test_pre_cancelled_task_does_not_touch_tv(self):
        task = TaskControl(self.cfg, "取消测试")
        control_task(self.cfg, task.id, "cancel")
        result = self.agent().run("打开红果播放山海归途", task_id=task.id)
        self.assertEqual(result["status"], "CANCELLED")
        self.device.key.assert_not_called()
        self.toolbox.call.assert_not_called()

    def test_unavailable_model_does_not_turn_unknown_goal_into_play_hot(self):
        self.cfg["agent"]["use_llm"] = False
        result = self.agent().run("替我安排一下")
        self.assertEqual(result["status"], "UNKNOWN")
        self.toolbox.call.assert_not_called()
        self.device.launch_app.assert_not_called()

    def test_verified_open_succeeds_without_model(self):
        self.cfg["agent"]["use_llm"] = False
        self.device.launch_app.return_value = {"ok": True}
        result = self.agent().run("打开红果")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "SUCCESS")
        self.llm.chat.assert_not_called()

    def test_feiniu_deterministic_failure_does_not_enter_llm_loop(self):
        # An authoritative Feiniu/fast-key path that fails must fail after bounded
        # recovery; the generic LLM loop lacks Feiniu navigation knowledge and
        # would only burn the whole execution timeout.
        agent = self.agent()
        with patch("adbtv.skill_engine.SkillEngine.execute", side_effect=RuntimeError("飞牛导航失败")):
            result = agent.run("打开飞牛")
        self.assertFalse(result["ok"], result)
        self.assertNotEqual(result["status"], "SUCCESS")
        self.llm.chat.assert_not_called()

    def test_resolve_package_by_label_alias_and_known_name(self):
        from adbtv.apps import resolve_package
        from adbtv.adb import CmdResult
        adb = Mock()
        def shell(args):
            if args == ["pm", "list", "packages", "-3"]:
                return CmdResult(args, 0, "package:com.trim.tv\npackage:cn.hongguo.tv\n", "")
            if args == ["pm", "list", "packages", "-s"]:
                return CmdResult(args, 0, "package:android\n", "")
            return CmdResult(args, 0, "", "")
        adb.shell.side_effect = shell
        cfg = {"apps": {"feiniu_tv": {"package": "com.trim.tv", "label": "飞牛TV"},
                        "hongguo": {"package": "cn.hongguo.tv", "label": "红果免费短剧"}}}
        self.assertEqual(resolve_package(adb, "飞牛TV", cfg), "com.trim.tv")   # label
        self.assertEqual(resolve_package(adb, "飞牛", cfg), "com.trim.tv")     # known/label
        self.assertEqual(resolve_package(adb, "feiniu_tv", cfg), "com.trim.tv")  # alias key
        self.assertEqual(resolve_package(adb, "com.trim.tv", cfg), "com.trim.tv")  # direct
        self.assertEqual(resolve_package(adb, "红果", cfg), "cn.hongguo.tv")


if __name__ == "__main__":
    unittest.main()
