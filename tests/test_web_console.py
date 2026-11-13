"""Local HTTP integration with temporary state and mocked devices/models."""
import copy
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from adbtv.config import DEFAULTS, load_config
from adbtv.server import make_server, redact_secrets
from adbtv.execution import DeviceBusy, TaskControl, runtime_dir


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["adb"].update(serial="mock-tv", fake_state=str(Path(self.tmp.name) / "tv.json"))
        self.cfg["execution"]["state_dir"] = self.tmp.name
        self.cfg["scheduler"]["store"] = str(Path(self.tmp.name) / "schedule.json")
        self.cfg["memory"].update(enabled=True, path=str(Path(self.tmp.name) / "memory.sqlite3"))
        self.token = "test-token-that-is-long-enough"
        self.cfg["server"].update(host="127.0.0.1", port=0, token=self.token, queue_size=1)
        self.contexts = []
        self.release = threading.Event()
        self.run_callback = lambda cfg, command, task_id: {"ok": True, "status": "SUCCESS"}
        self.context_patch = patch("adbtv.server.build_context", side_effect=self.build_context)
        self.context_patch.start()
        self.start_server()

    def build_context(self, cfg):
        agent = Mock()
        agent.run.side_effect = lambda command, task_id=None: self.run_callback(cfg, command, task_id)
        device = Mock()
        device.info.return_value = {"model": "mock", "serial": "mock-tv"}
        sched = Mock(tasks=[])
        context = (Mock(), device, Mock(), agent, sched)
        self.contexts.append((cfg, context))
        return context

    def start_server(self):
        self.server = make_server(self.cfg)
        self.port = self.server.server_address[1]
        # 短轮询：shutdown() 立即返回，避免每个用例多等 0.5s。
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05))
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(all(not t.is_alive() for t in self.server.workers))

    def tearDown(self):
        self.release.set()
        self.stop_server()
        self.context_patch.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"X-Auth-Token": self.token} if auth else {}
        data = json.dumps(body) if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        conn.close()
        return response.status, json.loads(raw) if "json" in content_type else raw

    def submit(self, command="打开飞牛"):
        status, result = self.request("POST", "/api/runs", {"command": command})
        self.assertEqual(status, 202, result)
        return result["task_id"]

    def wait_record(self, task_id, state=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, record = self.request("GET", "/api/runs/" + task_id)
            self.assertEqual(status, 200)
            if record["state"] == state if state else record["state"] not in ("RUNNING", "QUEUED", "PAUSED"):
                return record
            self.release.wait(.01)
        self.fail("task did not reach expected state")

    def block_worker(self):
        entered = threading.Event()
        def run(cfg, command, task_id):
            entered.set()
            self.release.wait(5)
            return {"ok": True, "status": "SUCCESS"}
        self.run_callback = run
        first = self.submit("first")
        self.assertTrue(entered.wait(2))
        return first

    def test_anonymous_remote_actions_rejected(self):
        for method, path in [("GET", "/api/info"), ("POST", "/api/cmd"), ("POST", "/api/tool"), ("GET", "/api/config"), ("GET", "/api/tasks"), ("GET", "/api/runs"), ("GET", "/api/skills"), ("GET", "/api/screenshot")]:
            self.assertEqual(self.request(method, path, {} if method == "POST" else None, auth=False)[0], 401)
        self.contexts[0][1][3].run.assert_not_called()

    def test_health_and_anonymous_console_index(self):
        self.assertEqual(self.request("GET", "/health", auth=False), (200, {"ok": True}))
        status, html = self.request("GET", "/?entry=login", auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'id="login-form"', html)
        status, script = self.request("GET", "/static/console.js", auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b"EventSource(", script)
        self.assertNotIn(b"?token=", script)
        self.assertIn(b"getReader()", script)
        self.assertIn(b"'X-Auth-Token':token", script)
        self.assertIn(b"reader.cancel()", script)
        self.assertIn(b"s.definition", script)
        self.assertIn(b"s.validation", script)

    def test_async_submission_and_header_events(self):
        task_id = self.submit()
        record = self.wait_record(task_id)
        self.assertEqual(record["owner_pid"], os.getpid())
        self.assertTrue(record["server_instance_id"])
        status, events = self.request("GET", "/api/runs/" + task_id + "/events?view=progress")
        self.assertEqual(status, 200)
        self.assertIn(b"data: ", events)
        self.assertIn(b"SUCCEEDED", events)
        self.assertEqual(self.request("GET", "/api/runs/" + task_id + "/events?token=" + self.token, auth=False)[0], 401)
        self.assertEqual(len(self.contexts), 2)
        self.assertIsNot(self.contexts[0][1][0], self.contexts[1][1][0])

    def test_unknown_task_returns_immediate_404(self):
        for suffix in ("", "/events"):
            self.assertEqual(self.request("GET", "/api/runs/unknown" + suffix)[0], 404)

    def test_sse_reconnect_sends_current_snapshot(self):
        task = TaskControl(self.cfg, "mock")
        task.finish("CANCELLED", {"ok": False, "status": "CANCELLED"})
        first = self.request("GET", "/api/runs/" + task.id + "/events")
        second = self.request("GET", "/api/runs/" + task.id + "/events")
        self.assertEqual(first, second)
        self.assertEqual(first[1].count(b"data: "), 1)

    def test_memory_and_skills_include_review_details(self):
        from adbtv.memory import MemoryStore
        store = MemoryStore(self.cfg["memory"]["path"])
        store.record_task({"id": "http-history"}, {"status": "SUCCESS"})
        definition = {"name": "测试", "package": "com.example.tv", "steps": []}
        store.propose_skill(definition)
        status, data = self.request("GET", "/api/memory")
        self.assertEqual(status, 200)
        self.assertEqual(data["stats"]["total_tasks"], 1)
        status, data = self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertEqual(data["skills"][0]["definition"], definition)
        self.assertIn("validation", data["skills"][0])

    def test_skill_approval_requires_confirmation(self):
        from adbtv.memory import MemoryStore
        sid = MemoryStore(self.cfg["memory"]["path"]).propose_skill({"name": "测试", "package": "com.example.tv", "steps": []})
        self.assertEqual(self.request("POST", f"/api/skills/{sid}/approve", {})[0], 400)

    def test_unknown_endpoint_and_bad_json(self):
        self.assertEqual(self.request("GET", "/api/unknown")[0], 404)
        self.assertEqual(self.request("POST", "/api/runs", [])[0], 400)

    def test_save_read_restart_preserves_aliases_and_private_permissions(self):
        source = Path(self.tmp.name) / "source.yaml"
        source.write_text(yaml.safe_dump({"aliases": {"野神": "完整片名"}, "apps": {"custom": {"package": "com.example"}}}))
        self.stop_server()
        self.cfg["_source"] = str(source)
        self.start_server()
        status, body = self.request("POST", "/api/config", {"provider": "openai", "base_url": "https://example.test/v1", "model": "mock-model", "api_key": "private-model-secret", "use_llm": False})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(source.stat().st_mode & 0o777, 0o600)
        loaded = load_config(str(source))
        self.assertEqual(loaded["llm"]["api_key"], "private-model-secret")
        self.assertEqual(loaded["aliases"], {"野神": "完整片名"})
        self.assertEqual(loaded["apps"]["custom"]["package"], "com.example")
        status, safe = self.request("GET", "/api/config")
        self.assertNotIn("private-model-secret", json.dumps(safe))
        self.stop_server()
        self.cfg = loaded
        self.cfg["server"].update(port=0, token=self.token)
        self.start_server()
        self.assertEqual(self.request("GET", "/api/config")[1]["llm"]["model"], "mock-model")

    def test_default_source_created_in_runtime_and_reloaded(self):
        self.assertEqual(self.request("POST", "/api/config", {"model": "persisted-model"})[0], 200)
        self.assertTrue((runtime_dir(self.cfg) / "config.yaml").is_file())
        self.stop_server()
        self.start_server()
        self.assertEqual(self.request("GET", "/api/config")[1]["llm"]["model"], "persisted-model")

    def test_invalid_configuration_does_not_change_file(self):
        self.request("POST", "/api/config", {"model": "original"})
        path = runtime_dir(self.cfg) / "config.yaml"
        original = path.read_bytes()
        for body in ({"provider": "invalid"}, {"base_url": "file:///tmp/x"}, {"model": []}, {"api_key": 123}, {"use_llm": "yes"}, {"path": "/tmp/x"}):
            self.assertEqual(self.request("POST", "/api/config", body)[0], 400)
            self.assertEqual(path.read_bytes(), original)

    def test_config_recursive_secret_filter(self):
        value = {"headers": {"Authorization": "secret", "nested": [{"api_key": "private", "model": "okay"}]}, "access_token": "token"}
        self.assertEqual(redact_secrets(value), {"headers": {"nested": [{"model": "okay"}]}})
        self.assertEqual(redact_secrets({"max_tokens": 1200}), {"max_tokens": 1200})
        self.assertEqual(redact_secrets("https://user:password@example.test/v1?api_key=private&version=1"), "https://example.test/v1?version=1")

    def test_running_and_queued_tasks_keep_config_snapshot(self):
        first = self.block_worker()
        queued = self.submit("queued")
        old_model = self.contexts[-1][0]["llm"]["model"]
        self.assertEqual(self.request("POST", "/api/config", {"model": "new-model", "api_key": "new-key"})[0], 200)
        self.assertEqual(self.contexts[-1][0]["llm"]["model"], old_model)
        self.release.set()
        self.wait_record(first)
        self.wait_record(queued)
        self.assertEqual(self.contexts[-1][0]["llm"]["model"], old_model)
        third = self.submit("new")
        self.wait_record(third)
        self.assertEqual(self.contexts[-1][0]["llm"]["model"], "new-model")
        self.assertIsNot(self.contexts[-1][0], self.contexts[-2][0])

    def test_bounded_queue_and_immediate_queued_cancel(self):
        first = self.block_worker()
        queued = self.submit("queued")
        before = set((runtime_dir(self.cfg) / "tasks").glob("*.json"))
        self.assertEqual(self.request("POST", "/api/runs", {"command": "overflow"})[0], 429)
        self.assertEqual(before, set((runtime_dir(self.cfg) / "tasks").glob("*.json")))
        self.assertEqual(self.request("POST", "/api/runs/" + queued + "/cancel")[0], 200)
        self.wait_record(queued, "CANCELLED")
        self.assertFalse(self.release.is_set())
        replacement = self.submit("replacement")
        self.release.set()
        self.wait_record(first)
        self.wait_record(replacement)
        commands = [call.args[0] for _, ctx in self.contexts for call in ctx[3].run.call_args_list]
        self.assertNotIn("queued", commands)
        self.assertEqual(len(self.server.workers), 2)

    def test_waiting_deadline_expires_while_worker_busy(self):
        self.stop_server()
        self.cfg["server"]["queue_timeout"] = .25
        self.start_server()
        self.block_worker()
        queued = self.submit("expire")
        self.wait_record(queued, "TIMED_OUT")
        self.assertFalse(self.release.is_set())

    def test_busy_device_has_conflict_status(self):
        self.contexts[0][1][1].info.side_effect = DeviceBusy("device busy")
        self.assertEqual(self.request("GET", "/api/info")[0], 409)

    def test_legacy_command_uses_executor_and_preserves_result(self):
        status, result = self.request("POST", "/api/cmd", {"command": "mock"})
        self.assertEqual((status, result), (200, {"ok": True, "status": "SUCCESS"}))
        self.assertEqual(len(self.contexts), 2)

    def test_atomic_write_failure_preserves_file_and_memory(self):
        self.request("POST", "/api/config", {"model": "before-failure"})
        path = runtime_dir(self.cfg) / "config.yaml"
        original = path.read_bytes()
        real_replace = os.replace
        def fail_config(source, destination):
            if Path(destination) == path:
                raise OSError("private-provider-secret")
            return real_replace(source, destination)
        with patch("adbtv.server.os.replace", side_effect=fail_config):
            status, body = self.request("POST", "/api/config", {"model": "after-failure"})
        self.assertEqual(status, 500)
        self.assertNotIn("private-provider-secret", json.dumps(body))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.request("GET", "/api/config")[1]["llm"]["model"], "before-failure")
        self.assertEqual(list(path.parent.glob(".config-*")), [])

    def test_restart_recovers_only_dead_console_instances(self):
        import fcntl
        from adbtv.execution import atomic_json
        self.stop_server()
        records = {
            "deadconsole": {"state": "RUNNING", "server_instance_id": "previous", "owner_pid": 99999999},
            "liveconsole": {"state": "QUEUED", "server_instance_id": "live", "owner_pid": os.getpid()},
            "scheduler": {"state": "RUNNING", "pid": 99999999},
            "permission": {"state": "PAUSED", "server_instance_id": "unknown", "owner_pid": 99999998},
        }
        for task_id, record in records.items():
            atomic_json(runtime_dir(self.cfg) / "tasks" / (task_id + ".json"), dict(id=task_id, **record))
        instances = runtime_dir(self.cfg) / "instances"
        instances.mkdir(parents=True, exist_ok=True)
        lease = instances / "live.lock"
        fd = os.open(lease, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.start_server()
            dead = self.request("GET", "/api/runs/deadconsole")[1]
            self.assertEqual(dead["state"], "INTERRUPTED")
            self.assertEqual(dead["result"]["status"], "UNKNOWN")
            # A live instance lease protects its QUEUED task even if PID matches.
            self.assertEqual(self.request("GET", "/api/runs/liveconsole")[1]["state"], "QUEUED")
            # No instance lease means the PAUSED task is uncertain, not live.
            self.assertEqual(self.request("GET", "/api/runs/permission")[1]["state"], "INTERRUPTED")
            self.assertEqual(self.request("GET", "/api/runs/scheduler")[1]["state"], "RUNNING")
            self.assertEqual(len(self.contexts), 2)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_worker_survives_control_signal_and_reclaims_failed_context(self):
        from adbtv.execution import TaskCancelled
        self.run_callback = Mock(side_effect=TaskCancelled("cancel"))
        cancelled = self.submit()
        self.wait_record(cancelled, "CANCELLED")
        self.run_callback = lambda cfg, command, task_id: {"ok": True, "status": "SUCCESS"}
        successor = self.submit()
        self.wait_record(successor, "SUCCEEDED")
        self.assertTrue(all(t.is_alive() for t in self.server.workers))

    def test_shutdown_closes_live_streams(self):
        task = TaskControl(self.cfg, "mock")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/api/runs/" + task.id + "/events", headers={"X-Auth-Token": self.token})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.readline().startswith(b"data: "))
        self.assertEqual(len(self.server.streams), 1)
        self.stop_server()
        self.assertEqual(len(self.server.streams), 0)
        response.read()
        conn.close()
        self.start_server()

    def test_scheduler_health_is_not_invented(self):
        status, result = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(result["executor"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()


class VoiceEndpointTests(unittest.TestCase):
    """Phase 3/5 console endpoints: audio delivery and pluggable input."""

    def setUp(self):
        import copy
        from adbtv.config import DEFAULTS
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["voice"]["intelligence"]["enabled"] = True
        self.cfg["voice"]["tts"].update({"enabled": True, "cache_dir": self.tmp.name,
                                         "include_url": True, "async": True})
        self.cfg["llm"]["provider"] = "none"        # tests must not hit a network model
        self.cfg["server"].update(host="127.0.0.1", port=0)
        self.cfg["execution"]["state_dir"] = str(Path(self.tmp.name) / "runtime")
        self.cfg["memory"]["enabled"] = False
        self.context_patch = patch("adbtv.server.build_context", self._context)
        self.context_patch.start()
        self.addCleanup(self.context_patch.stop)
        self.server = make_server(self.cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05))
        self.thread.start()
        self.addCleanup(self._stop)

    def _context(self, cfg):
        device = Mock()
        device.current_package.return_value = "com.trim.tv"
        device.playback_status.return_value = {"playing": False, "state": "PAUSED"}
        device.volume.return_value = {"known": True, "index": 5, "min": 0, "max": 100}
        agent = Mock()
        agent.run.return_value = {"status": "SUCCESS", "ok": True, "summary": "ok"}
        return Mock(), device, Mock(), agent, Mock()

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, auth=True):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Auth-Token": self._token()} if auth else {}
        data = json.dumps(body) if body is not None else None
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload

    def _token(self):
        from adbtv.execution import runtime_dir
        return (runtime_dir(self.cfg) / "console.token").read_text().strip()

    def _token_ready(self):
        from adbtv.execution import runtime_dir
        return (runtime_dir(self.cfg) / "console.token").is_file()

    def test_voice_text_requires_auth(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        status, _ = self.request("POST", "/api/voice", {"text": "暂停"}, auth=False)
        self.assertEqual(status, 401)

    def test_web_microphone_text_runs_through_dialogue(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        status, payload = self.request("POST", "/api/voice",
                                       {"text": "暂停", "source": "web_microphone"})
        self.assertEqual(status, 200, payload)
        body = json.loads(payload)
        self.assertEqual(body["source"], "web_microphone")
        self.assertEqual(body["status"], "SUCCESS")
        self.assertIn("session_id", body)

    def test_external_asr_is_reserved_not_faked(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        status, payload = self.request("POST", "/api/voice",
                                       {"text": "暂停", "source": "external_asr"})
        # Reserved channel must fail explicitly, never pretend to work.
        self.assertEqual(status, 501, payload)
        self.assertIn("error", json.loads(payload))

    def test_audio_endpoint_serves_only_existing_private_files(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        (Path(self.tmp.name) / "abcd.wav").write_bytes(b"RIFFdemo")
        status, payload = self.request("GET", "/api/voice/audio/abcd.wav")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"RIFFdemo")
        self.assertIn(self.request("GET", "/api/voice/audio/nope.wav")[0], (400, 404))
        self.assertEqual(self.request("GET", "/api/voice/audio/..%2Fetc%2Fpasswd")[0], 400)

    def test_voice_status_reports_tv_audio_unavailable(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        status, payload = self.request("GET", "/api/voice/status")
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertFalse(body["tv_audio"], "TV audio must be declared unavailable")
        self.assertTrue("不可用" in body["tv_audio_note"] or "unavailable" in body["delivery"])
        self.assertIn(body["delivery"], ("web_audio", "unavailable_no_base_url"))
        self.assertIn("auth", body)


class VoiceTTSApiTests(VoiceEndpointTests):
    """Task 3 over HTTP: job status and authenticated audio fetch."""

    def setUp(self):
        super().setUp()
        from adbtv.tts import Synthesis
        # Deterministic: never call a real model in tests; deliverable audio URL.
        responder = self.server.voice_dialogue.responder
        responder.base_url = "http://nas:8771"
        responder.tts.available = lambda: (True, "")
        self.server.voice_dialogue.responder.tts.synthesize = (
            lambda text: Synthesis(True, text, str(Path(self.tmp.name) / "tts.wav"),
                                   900, False, 1.0))
        (Path(self.tmp.name) / "tts.wav").write_bytes(b"RIFFtest")

    def voice_turn(self, text="暂停"):
        status, payload = self.request("POST", "/api/voice",
                                       {"text": text, "source": "web_microphone"})
        return status, json.loads(payload)

    def test_job_becomes_ready_and_audio_requires_auth(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        status, body = self.voice_turn()
        self.assertEqual(status, 200, body)
        job_id = ((body.get("speech") or {}).get("job_id")) or body.get("job_id")
        self.assertTrue(job_id, body)
        deadline = time.time() + 10
        job = {}
        while time.time() < deadline:
            code, raw = self.request("GET", f"/api/voice/tts/{job_id}")
            job = json.loads(raw)
            if job.get("state") in ("READY", "FAILED"):
                break
            time.sleep(0.05)
        self.assertEqual(job.get("state"), "READY", job)
        name = job["audio_url"].rsplit("/", 1)[-1]
        self.assertEqual(self.request("GET", f"/api/voice/audio/{name}", auth=False)[0], 401)
        code, payload = self.request("GET", f"/api/voice/audio/{name}")
        self.assertEqual(code, 200)
        self.assertEqual(payload, b"RIFFtest")

    def test_unknown_job_is_404(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        self.assertEqual(self.request("GET", "/api/voice/tts/deadbeef")[0], 404)

    def test_job_status_requires_auth(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        self.assertEqual(self.request("GET", "/api/voice/tts/x", auth=False)[0], 401)


class VoiceBusyTests(VoiceEndpointTests):
    """A device lease held elsewhere must fail fast, not queue the browser."""

    def test_voice_returns_409_when_device_is_busy(self):
        if not self._token_ready():
            self.skipTest("token not generated")
        from adbtv.execution import device_lease
        with device_lease(self.cfg):
            status, payload = self.request(
                "POST", "/api/voice", {"text": "暂停", "source": "web_microphone"})
        self.assertEqual(status, 409, payload)
        self.assertIn("其他任务", json.loads(payload)["error"])


class WebVoiceUiTests(unittest.TestCase):
    """Task 2/3: the page must really use /api/voice and authenticated fetch."""

    def html(self):
        from pathlib import Path as _Path
        return (_Path(__file__).resolve().parents[1] / "adbtv" / "web" / "index.html").read_text()

    def test_voice_area_posts_to_voice_endpoint(self):
        html = self.html()
        self.assertIn("/api/voice", html)
        self.assertIn('id="voice-panel"', html)

    def test_voice_ui_uses_vendored_alpine(self):
        html = self.html()
        self.assertIn('/vendor/alpine-csp-3.14.9.min.js', html)
        self.assertNotIn('cdn.jsdelivr.net', html)
        self.assertNotIn('unpkg.com', html)
        self.assertIn('x-data="voicePanel"', html)

    def test_voice_ui_renders_candidates_and_confirmation(self):
        html = self.html()
        self.assertIn("candidates", html)
        self.assertIn("确认", html)
        self.assertIn("取消", html)

    def test_audio_fetch_uses_console_token(self):
        script = (Path(__file__).resolve().parents[1] / "adbtv" / "web" / "console.js").read_text()
        self.assertIn("/api/voice/audio/", script)
        self.assertIn("/api/voice/tts/", script)
        self.assertIn("X-Auth-Token", script)

    def test_regular_task_input_still_uses_runs(self):
        html = self.html()
        self.assertIn("/api/runs", html)
