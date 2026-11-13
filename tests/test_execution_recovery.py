"""V2.1 lifecycle/recovery regression tests without a television."""
import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from adbtv.execution import (TaskControl, TaskCancelled, TaskTimeout, DeviceBusy,
                             device_lease, control_task)
from adbtv.recovery import Failure, RecoveryManager, classify, NoScreenChange
from adbtv.agent import _extract_json


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"adb": {"serial": "fake:5555"}, "execution": {
            "state_dir": self.tmp.name, "timeout": 3, "max_actions": 2}}

    def test_device_lease_blocks_other_thread_but_is_reentrant(self):
        seen = []
        def attempt():
            try:
                with device_lease(self.cfg):
                    seen.append("unexpected")
            except DeviceBusy:
                seen.append("busy")
        with device_lease(self.cfg):
            with device_lease(self.cfg):
                t = threading.Thread(target=attempt)
                t.start(); t.join(2)
        self.assertEqual(seen, ["busy"])
        with device_lease(self.cfg):
            pass

    def test_cancel_cannot_be_swallowed_by_generic_tool_errors(self):
        task = TaskControl(self.cfg, "测试取消")
        control_task(self.cfg, task.id, "cancel")
        with self.assertRaises(TaskCancelled):
            task.checkpoint()

    def test_pause_resume_and_cancel(self):
        task = TaskControl(self.cfg, "测试暂停")
        control_task(self.cfg, task.id, "pause")
        done = threading.Event()
        def worker():
            task.checkpoint()
            done.set()
        t = threading.Thread(target=worker)
        t.start()
        for _ in range(30):
            if task.state == "PAUSED": break
            time.sleep(.01)
        self.assertFalse(done.is_set())
        self.assertEqual(task.state, "PAUSED")
        control_task(self.cfg, task.id, "resume")
        t.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(task.state, "RUNNING")

    def test_time_and_action_limits(self):
        task = TaskControl(self.cfg, "测试上限")
        task.before_adb(["shell", "input keyevent KEYCODE_BACK"])
        task.before_adb(["shell", "input keyevent KEYCODE_BACK"])
        with self.assertRaises(TaskTimeout):
            task.before_adb(["shell", "input keyevent KEYCODE_BACK"])
        task.deadline = time.monotonic()-1
        with self.assertRaises(TaskTimeout):
            task.checkpoint()

    def test_json_must_be_an_object_without_extra_prose(self):
        for s in ("[]", "null", "1", 'prefix {"done":true}', '{"done":true} tail'):
            self.assertIsNone(_extract_json(s))
        self.assertEqual(_extract_json('{"tool":"tv_info","args":{}}')["tool"], "tv_info")

    def test_recovery_classification_and_bounded_retries(self):
        device, perception = Mock(), Mock()
        recovery = RecoveryManager(device, perception, {"recovery": {"max_attempts": 2, "per_failure": 1}})
        self.assertEqual(classify("device offline"), Failure.CONNECTION)
        self.assertEqual(classify(NoScreenChange()), Failure.NO_CHANGE)
        self.assertTrue(recovery.recover("device offline").retry)
        self.assertTrue(recovery.recover("device offline").handoff)
        device.adb.connect.assert_called_once_with(force=True)

    def test_unrecognized_popup_is_not_clicked(self):
        device, perception = Mock(), Mock()
        perception.observe.return_value.elements = []
        result = RecoveryManager(device, perception, {}).recover("popup blocked")
        self.assertTrue(result.handoff)
        perception.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
