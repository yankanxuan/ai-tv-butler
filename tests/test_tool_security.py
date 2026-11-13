#!/usr/bin/env python3
"""Offline security regressions. Only mocks and the built-in fake ADB run."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adbtv.adb import AdbClient, AdbError, CmdResult
from adbtv.config import DEFAULTS
from adbtv.mcp_server import MCPServer
from adbtv.tools import ToolBox, ToolSpec, run_flow


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitv_security_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["adb"].update(binary=f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'tests/fake_adb.py'))}",
                               serial="fake-tv:5555", fake_state=str(self.root / "tv.json"))
        self.cfg["agent"]["log_dir"] = str(self.root / "logs")
        self.device = Mock()
        self.device.cfg = self.cfg
        self.toolbox = ToolBox(self.device)

    def test_remote_shell_quotes_each_argument(self):
        values = ["input", "text", "a b;$(touch /tmp/NEVER)`id`&|><'\"\\\n"]
        adb = AdbClient(self.cfg)
        with patch.object(adb, "run", return_value=CmdResult([], 0, "", "")) as run:
            adb.shell(values)
        args = run.call_args.args[0]
        self.assertEqual(args, ["shell", shlex.join(values)])
        self.assertEqual(shlex.split(args[1]), values)

    def test_fake_shell_single_string_preserves_malicious_text(self):
        marker = self.root / "injected"
        text = f"a; touch {marker}; $(id) `whoami` & 'quoted' \"double\""
        adb = AdbClient(self.cfg)
        self.assertTrue(adb.shell(["input", "text", text]).ok)
        state = json.loads((self.root / "tv.json").read_text())
        self.assertEqual(state["query"], text)
        self.assertFalse(marker.exists())
        # XML metacharacters must stay valid after the fake renders input text.
        from adbtv.device import TVDevice
        self.assertTrue(TVDevice(adb, self.cfg).elements())

    def test_string_shell_restricted_and_lists_required(self):
        adb = AdbClient(self.cfg)
        for command in ("id", "wm size; id", "input text hi", [], [1], ""):
            with self.subTest(command=command), self.assertRaises(ValueError):
                adb.shell(command)
        self.assertIn("1920x1080", adb.shell("wm size").out)

    def test_nonzero_out_and_binary_failures_are_detectable(self):
        result = CmdResult(["adb", "shell", "input keyevent KEYCODE_HOME"], 1, "", "offline")
        self.assertFalse(result.ok)
        with self.assertRaises(AdbError):
            _ = result.out
        with patch.object(AdbClient, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"offline")):
            with self.assertRaises(AdbError):
                AdbClient(self.cfg).screenshot_bytes()
        with patch.object(AdbClient, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")):
            with self.assertRaises(AdbError):
                AdbClient(self.cfg).screenshot_bytes()

    def test_checkpoint_receives_args_and_no_recursion(self):
        adb = AdbClient(self.cfg)
        calls = []
        def checkpoint(args):
            calls.append(args)
            adb.run(["devices"])
        adb.checkpoint = checkpoint
        with patch("adbtv.adb.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            adb.run(["shell", "wm size"])
        self.assertEqual(calls, [["shell", "wm size"]])
        self.assertEqual(run.call_count, 2)

    def test_checkpoint_zero_arg_compatibility_and_typeerror_propagation(self):
        adb = AdbClient(self.cfg)
        calls = []
        adb.checkpoint = lambda: calls.append(True)
        with patch("adbtv.adb.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            adb.run(["devices"])
        self.assertEqual(calls, [True])
        def broken(args):
            raise TypeError("inside checkpoint")
        adb.checkpoint = broken
        with patch("adbtv.adb.subprocess.run") as run:
            with self.assertRaisesRegex(TypeError, "inside checkpoint"):
                adb.run(["devices"])
            run.assert_not_called()
        self.assertFalse(adb._in_checkpoint)

    def test_missing_required_arguments_rejected_before_device(self):
        for name in ("tv_launch_app", "tv_key", "tv_tap", "tv_swipe", "tv_input_text", "tv_auto_play", "tv_run_flow"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.toolbox.call(name)
        self.device.assert_not_called()
        self.assertEqual(self.device.mock_calls, [])

    def test_invalid_argument_containers_not_treated_as_empty(self):
        for args in ([], False, 0, "", "text"):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.toolbox.call("tv_info", args)
        self.device.info.assert_not_called()

    def test_unknown_tools_and_extra_fields_rejected(self):
        for name in ("shell", "adb_shell", "__dict__", ["tv_info"], None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.toolbox.call(name, {})
        with self.assertRaises(ValueError):
            self.toolbox.call("tv_info", {"shell": "id"})
        self.assertEqual(self.device.mock_calls, [])

    def test_boolean_integer_and_bad_ranges_rejected(self):
        for args in ({"x": True, "y": 1}, {"x": 1.0, "y": 1}, {"x": -1, "y": 1}, {"x": 32768, "y": 1}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.toolbox.call("tv_tap", args)
        for timeout in (False, -1, 0, 121, "2"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.toolbox.call("tv_wait_text", {"text": "ready", "timeout": timeout})
        for duration in (False, 0, -1, 10001):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.toolbox.call("tv_swipe", dict(x1=0, y1=0, x2=1, y2=1, duration=duration))
        self.assertEqual(self.device.mock_calls, [])

    def test_text_limits_and_nul_rejected(self):
        for text in ("", "a" * 4097, "a\x00b", 123, None):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                self.toolbox.call("tv_input_text", {"text": text})
        self.device.input_text.assert_not_called()

    def test_defaults_and_tool_count_schema(self):
        self.toolbox.call("tv_swipe", dict(x1=0, y1=0, x2=1, y2=2))
        self.device.swipe.assert_called_once_with(0, 0, 1, 2, 300)
        specs = self.toolbox.specs()
        self.assertEqual(len(specs), 15)
        tap = next(spec for spec in specs if spec["name"] == "tv_tap")["inputSchema"]
        self.assertEqual(tap["required"], ["x", "y"])
        self.assertFalse(tap["additionalProperties"])
        tap["properties"]["x"]["minimum"] = -100
        with self.assertRaises(ValueError):
            self.toolbox.call("tv_tap", {"x": -1, "y": 0})

    def test_custom_toolspec_required_enum_and_defaults(self):
        spec = ToolSpec("custom", "example", {"mode": {"type": "string", "enum": ["safe"]},
                        "retry": {"type": "integer", "default": 2}}, lambda d, a: a, required=["mode"])
        self.assertEqual(spec.validate({"mode": "safe"}), {"mode": "safe", "retry": 2})
        for value in ({}, {"mode": "unsafe"}, {"mode": "safe", "retry": True}):
            with self.assertRaises(ValueError):
                spec.validate(value)

    def test_hooks_receive_validated_defaults_and_results(self):
        events = []
        self.toolbox.before_call = lambda name, args: events.append(("before", name, args))
        self.toolbox.after_call = lambda name, args, result: events.append(("after", name, args, result))
        self.device.wait_for_text.return_value = False
        result = self.toolbox.call("tv_wait_text", {"text": "ready"})
        self.assertEqual(result, {"ok": False, "found": False})
        self.assertEqual(events[0], ("before", "tv_wait_text", {"text": "ready", "timeout": 20}))
        self.assertEqual(events[1][-1], result)
        with self.assertRaises(ValueError):
            self.toolbox.call("tv_wait_text", {})
        self.assertEqual(len(events), 2)

    def test_screenshot_rejects_escape_absolute_and_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        logs = Path(self.cfg["agent"]["log_dir"])
        logs.mkdir()
        (logs / "escape").symlink_to(outside, target_is_directory=True)
        for path in (str(outside / "a.png"), "../outside/b.png", "escape/c.png", "data.json", str(logs)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.toolbox.call("tv_screenshot", {"path": path})
        self.device.adb.screenshot_bytes.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_screenshot_confined_and_cannot_overwrite(self):
        self.device.adb.screenshot_bytes.return_value = b"png"
        result = self.toolbox.call("tv_screenshot", {"path": "nested/screen.png"})
        target = Path(result["path"])
        self.assertTrue(target.is_relative_to(Path(self.cfg["agent"]["log_dir"])))
        self.assertEqual(target.read_bytes(), b"png")
        self.device.adb.screenshot_bytes.return_value = b"changed"
        with self.assertRaises(FileExistsError):
            self.toolbox.call("tv_screenshot", {"path": str(target)})
        self.assertEqual(target.read_bytes(), b"png")
        default = self.toolbox.call("tv_screenshot")
        self.assertTrue(Path(default["path"]).is_file())

    def server(self):
        server = MCPServer(self.cfg)
        server.toolbox = Mock()
        server.toolbox.specs.return_value = []
        server.toolbox.call.return_value = {"ok": True}
        return server

    def test_mcp_nonobjects_and_invalid_envelopes(self):
        server = self.server()
        for req in ([], [1], 1, None, "x", {}, {"id": 1, "jsonrpc": "2.0"},
                    {"jsonrpc": "1.0", "method": "ping"},
                    {"jsonrpc": "2.0", "id": True, "method": "ping"},
                    {"jsonrpc": "2.0", "method": 1}):
            with self.subTest(req=req):
                self.assertEqual(server.handle(req)["error"]["code"], -32600)
        server.toolbox.call.assert_not_called()

    def test_mcp_notifications_never_return_responses(self):
        server = self.server()
        requests = [("initialize", {}), ("ping", {}), ("tools/list", {}),
                    ("tools/call", {"name": "tv_info", "arguments": {}}),
                    ("tools/call", {"name": "tv_info", "arguments": []}),
                    ("unknown", {}), ("notifications/initialized", {}), ("ping", [])]
        for method, params in requests:
            with self.subTest(method=method, params=params):
                self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": method, "params": params}))
        server.toolbox.call.side_effect = ValueError("failed")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "bad"}}))
        self.assertIn("result", server.handle({"jsonrpc": "2.0", "method": "ping", "id": None}))

    def test_mcp_bad_params_and_tool_failures(self):
        server = self.server()
        for params in (None, [], False, {}, {"name": []}, {"name": "tv_info", "arguments": []},
                       {"name": "tv_info", "arguments": None}, {"name": "tv_info", "extra": 1}):
            with self.subTest(params=params):
                result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
                self.assertEqual(result["error"]["code"], -32602)
        server.toolbox.call.return_value = {"ok": False, "error": "not found"}
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "tv_info"}}
        self.assertTrue(server.handle(req)["result"]["isError"])
        server.toolbox.call.side_effect = ValueError("bad tool")
        self.assertTrue(server.handle(req)["result"]["isError"])

    def test_mcp_stdio_parse_errors_and_recovery(self):
        server = self.server()
        source = '{broken\nNaN\n[]\n{"jsonrpc":"2.0","method":"ping"}\n{"jsonrpc":"2.0","id":4,"method":"ping"}\n'
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(source)), patch("sys.stdout", output):
            server.serve_stdio()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([response.get("error", {}).get("code") for response in responses], [-32700, -32700, -32600, None])
        self.assertEqual(responses[-1]["id"], 4)

    def flow(self, steps):
        self.cfg["apps"]["custom"] = {"package": "com.example.tv", "flow": steps}
        return run_flow(self.device, "custom", self.cfg)

    def test_flow_wait_text_failure_stops_before_next_action(self):
        self.device.wait_for_text.return_value = False
        self.device.playback_status.return_value = {"playing": True}
        result = self.flow([{"action": "wait_text", "text": "ready"}, {"action": "key", "key": "play"}])
        self.assertFalse(result["ok"])
        self.device.key.assert_not_called()
        self.device.playback_status.assert_not_called()

    def test_flow_unknown_or_invalid_steps_validated_before_actions(self):
        for invalid in ({"action": "shell", "command": "id"}, {"action": "tap", "x": True, "y": 1},
                        {"action": "sleep", "seconds": -1}, {"action": "sleep", "seconds": float("inf")},
                        {"action": "key", "key": "play", "unknown": 1}, {"action": "wait_text"}, None):
            with self.subTest(invalid=invalid):
                result = self.flow([{"action": "key", "key": "home"}, invalid])
                self.assertFalse(result["ok"])
        self.assertEqual(self.device.mock_calls, [])

    def test_flow_launch_failure_cannot_report_playing_success(self):
        self.device.launch_app.return_value = {"ok": False}
        self.device.playback_status.return_value = {"playing": True}
        result = self.flow([{"action": "launch"}, {"action": "key", "key": "play"}])
        self.assertFalse(result["ok"])
        self.device.key.assert_not_called()
        self.device.playback_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
