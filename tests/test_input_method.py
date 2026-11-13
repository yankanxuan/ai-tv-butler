"""Real AdbClient argument quoting and IME transaction, with no subprocess/device."""
import base64
import shlex
import subprocess
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from adbtv.adb import AdbClient, AdbError
from adbtv.device import TVDevice
from adbtv.execution import TaskCancelled

SOGOU = "com.sohu.inputmethod.sogou/.SogouIME"
ADB = "com.android.adbkeyboard/.AdbIME"
ADB_FULL = "com.android.adbkeyboard/com.android.adbkeyboard.AdbIME"


class IMEBackend:
    def __init__(self):
        self.current = SOGOU
        self.available = [SOGOU, ADB]
        self.calls = []
        self.text = ""
        self.deliver = True
        self.broadcast_code = 0
        self.broadcast_output = "Broadcast completed: result=0"
        self.refuse_switch = False
        self.refuse_restore = False
        self.fallback_list = False
        self.normalize = False
        self.dumps = 0
        self.dump_failures = 0
        self.editors = None
        self.after_broadcast = None

    def run(self, command, **kwargs):
        args = shlex.split(command[command.index("shell") + 1])
        self.calls.append(args)
        output, code = "", 0
        if args == ["settings", "get", "secure", "default_input_method"]:
            output = ADB_FULL if self.normalize and self.current == ADB else self.current
        elif args[:2] == ["ime", "list"]:
            if self.fallback_list and "-a" in args:
                code = 1
            else:
                output = "\n".join(self.available)
        elif args[:2] == ["ime", "enable"]:
            output = "Input method enabled"
        elif args[:2] == ["ime", "set"]:
            if args[2] == SOGOU and self.refuse_restore:
                code = 1
            elif not self.refuse_switch:
                self.current = args[2]
            output = "Input method selected"
        elif args[:2] == ["am", "broadcast"]:
            if self.deliver and self.current in (ADB, ADB_FULL):
                self.text = base64.b64decode(args[-1]).decode("utf-8")
            code, output = self.broadcast_code, self.broadcast_output
            if self.after_broadcast:
                self.after_broadcast()
        elif args[:2] == ["input", "text"]:
            self.text = args[2].replace("%s", " ")
        elif args[:2] == ["uiautomator", "dump"]:
            self.dumps += 1
            if self.dump_failures:
                self.dump_failures -= 1
                code, output = 1, "UI unavailable"
            else:
                output = "UI hierchary dumped to: " + args[2]
        elif args[0] == "cat":
            editors = self.editors if self.editors is not None else [("EditText", self.text, True)]
            nodes = []
            for cls, text, focused in editors:
                quoted = escape(text, {'"': '&quot;'})
                nodes.append(f'<node class="android.widget.{cls}" text="{quoted}" focused="{str(focused).lower()}" bounds="[0,0][100,20]"/>')
            output = '<hierarchy>' + ''.join(nodes) + '</hierarchy>'
        else:
            raise AssertionError("unexpected command " + repr(args))
        return subprocess.CompletedProcess(command, code, output, "failed" if code else "")


class InputMethodTests(unittest.TestCase):
    def setUp(self):
        self.backend = IMEBackend()
        self.cfg = {"adb": {"binary": "mock-adb", "serial": "mock-tv"}, "ui": {"input_timeout": 0}}
        self.adb = AdbClient(self.cfg)
        self.device = TVDevice(self.adb, self.cfg)
        self.process_patch = patch("adbtv.adb.subprocess.run", side_effect=self.backend.run)
        self.process = self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def broadcasts(self):
        return [args for args in self.backend.calls if args[:2] == ["am", "broadcast"]]

    def test_ascii_remote_quotes_do_not_add_backslashes(self):
        text = "A&B 'quoted' ; $(touch /tmp/nope)"
        result = self.device.input_text(text)
        self.assertTrue(result["ok"])
        self.assertEqual(self.backend.text, text)
        self.assertEqual(self.backend.calls, [["input", "text", text.replace(" ", "%s")]])
        self.assertEqual(self.process.call_count, 1)

    def test_empty_and_invalid_inputs_do_not_touch_device(self):
        self.assertEqual(self.device.input_text("")["method"], "noop")
        for text in (None, 123, "bad\x00input"):
            with self.assertRaises(ValueError):
                self.device.input_text(text)
        self.process.assert_not_called()

    def test_chinese_temporarily_selects_and_restores_sogou(self):
        result = self.device.input_text("野神第一集")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["acknowledged"])
        self.assertEqual(self.backend.current, SOGOU)
        self.assertEqual(self.backend.text, "野神第一集")
        self.assertEqual([args for args in self.backend.calls if args[:2] == ["ime", "set"]], [["ime", "set", ADB], ["ime", "set", SOGOU]])
        self.assertEqual(self.backend.dumps, 1)

    def test_existing_adb_ime_needs_no_switch_or_restoration(self):
        self.backend.current = ADB
        self.assertTrue(self.device.input_text("野神")["verified"])
        self.assertFalse(any(args[:2] in (["ime", "set"], ["ime", "enable"]) for args in self.backend.calls))

    def test_component_normalization_accepts_equivalent_adb_service(self):
        self.backend.normalize = True
        self.assertTrue(self.device.input_text("野神")["ok"])
        self.assertEqual(self.backend.current, SOGOU)
        self.backend.current = ADB_FULL
        self.backend.calls.clear()
        self.assertTrue(self.device.input_text("山海")["ok"])
        self.assertFalse(any(args[:2] == ["ime", "set"] for args in self.backend.calls))

    def test_absent_adb_ime_does_not_broadcast_or_install(self):
        self.backend.available = [SOGOU]
        with self.assertRaisesRegex(AdbError, "未发现可用ADBKeyboard"):
            self.device.input_text("野神")
        self.assertEqual(self.broadcasts(), [])
        self.assertFalse(any(args[0] in ("pm", "install") or args[:2] == ["ime", "set"] for args in self.backend.calls))

    def test_unsupported_full_list_falls_back_to_enabled_list(self):
        self.backend.fallback_list = True
        self.assertTrue(self.device.input_text("野神")["ok"])
        self.assertIn(["ime", "list", "-s"], self.backend.calls)

    def test_invalid_original_component_is_never_passed_to_set(self):
        for original in ("null", "", "com.test/.IME;am start anything", "com.test/.IME\ncom.other/.IME"):
            self.backend.current = original
            with self.assertRaises(AdbError):
                self.device.input_text("野神")
        self.assertEqual(self.broadcasts(), [])
        self.assertFalse(any(args[:2] == ["ime", "set"] for args in self.backend.calls))

    def test_untrusted_installed_component_is_never_selected(self):
        self.backend.available = [SOGOU, ADB + ";input keyevent HOME", "com.fake.keyboard/.AdbIME"]
        with self.assertRaises(AdbError):
            self.device.input_text("野神")
        self.assertEqual(self.broadcasts(), [])

    def test_ineffective_switch_refuses_broadcast_and_restores(self):
        self.backend.refuse_switch = True
        with self.assertRaisesRegex(AdbError, "未成为当前输入法"):
            self.device.input_text("野神")
        self.assertEqual(self.broadcasts(), [])
        self.assertIn(["ime", "set", SOGOU], self.backend.calls)

    def test_broadcast_ack_without_visible_text_is_not_success(self):
        self.backend.deliver = False
        result = self.device.input_text("野神")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["acknowledged"])
        self.assertEqual(self.backend.current, SOGOU)

    def test_only_current_edittext_actual_text_counts(self):
        for editors in ([("TextView", "野神", True)], [("EditText", "other", True), ("EditText", "野神", False)], [("EditText", "野神", False), ("EditText", "other", False)]):
            self.backend.editors = editors
            self.assertFalse(self.device.input_text("野神")["verified"])
        self.backend.editors = [("EditText", "prefix 野神 suffix", True)]
        self.assertTrue(self.device.input_text("野神")["verified"])

    def test_broadcast_failure_restores_original_ime(self):
        self.backend.broadcast_code = 1
        with self.assertRaisesRegex(AdbError, "广播失败"):
            self.device.input_text("野神")
        self.assertEqual(self.backend.current, SOGOU)

    def test_permission_denial_is_not_broadcast_success(self):
        self.backend.broadcast_output = "Permission Denial: broadcast rejected"
        with self.assertRaises(AdbError):
            self.device.input_text("野神")
        self.assertEqual(self.backend.current, SOGOU)

    def test_polling_is_bounded_by_three_attempts(self):
        self.cfg["ui"]["input_timeout"] = 100
        self.backend.deliver = False
        with patch("time.sleep") as sleep:
            self.assertFalse(self.device.input_text("野神")["ok"])
        self.assertEqual(self.backend.dumps, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_expired_poll_budget_does_not_start_another_read(self):
        self.cfg["ui"]["input_timeout"] = .1
        self.backend.deliver = False
        with patch("time.monotonic", side_effect=[0., .2]), patch("time.sleep") as sleep:
            self.assertFalse(self.device.input_text("野神")["ok"])
        self.assertEqual(self.backend.dumps, 1)
        sleep.assert_not_called()

    def test_transient_ui_failure_can_be_verified_on_second_read(self):
        self.cfg["ui"]["input_timeout"] = 2
        self.backend.dump_failures = 1
        with patch("time.sleep"):
            result = self.device.input_text("野神")
        self.assertTrue(result["verified"])
        self.assertEqual(self.backend.dumps, 2)

    def test_cancellation_propagates_but_restores_with_thread_local_checkpoint(self):
        cancelled = False
        def mark_cancelled():
            nonlocal cancelled
            cancelled = True
        def hook(args):
            if cancelled:
                raise TaskCancelled("cancelled")
        self.adb.checkpoint = hook
        self.backend.after_broadcast = mark_cancelled
        with self.assertRaises(TaskCancelled):
            self.device.input_text("野神")
        self.assertEqual(self.backend.current, SOGOU)
        self.assertIs(self.adb.checkpoint, hook)
        self.assertFalse(self.adb._checkpoint_state.active)
        self.assertEqual(self.backend.calls[-2:], [["ime", "set", SOGOU], ["settings", "get", "secure", "default_input_method"]])
        self.assertEqual(self.backend.dumps, 0)

    def test_cancel_during_broadcast_does_not_get_swallowed(self):
        self.backend.after_broadcast = lambda: (_ for _ in ()).throw(TaskCancelled("cancel"))
        with self.assertRaises(TaskCancelled):
            self.device.input_text("野神")
        self.assertEqual(self.backend.current, SOGOU)

    def test_restore_failure_does_not_return_success(self):
        self.backend.refuse_restore = True
        with self.assertRaisesRegex(AdbError, "恢复原输入法失败"):
            self.device.input_text("野神")

    def test_restore_failure_does_not_mask_cancellation(self):
        self.backend.refuse_restore = True
        self.backend.after_broadcast = lambda: (_ for _ in ()).throw(TaskCancelled("cancel"))
        with self.assertRaises(TaskCancelled):
            self.device.input_text("野神")

    def test_invalid_poll_configuration_fails_before_switch(self):
        for timeout in (-1, True, "2", float("inf"), float("nan")):
            self.cfg["ui"]["input_timeout"] = timeout
            with self.assertRaises(ValueError):
                self.device.input_text("野神")
        self.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
