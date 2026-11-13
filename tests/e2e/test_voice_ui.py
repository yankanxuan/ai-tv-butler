"""Real-browser e2e for the console voice loop (Playwright, test-only).

The server runs in-process with a simulated TV and stubbed synthesis so the test
exercises real DOM/Alpine/fetch behaviour without touching the television.

Skipped (never "passed") when Playwright or its browser is unavailable.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]

try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # noqa: BLE001 - test-only optional dependency
    PLAYWRIGHT_AVAILABLE = False


def start_server(tmp: Path):
    from adbtv.config import DEFAULTS
    from adbtv.server import make_server
    from adbtv.tts import Synthesis

    cfg = copy.deepcopy(DEFAULTS)
    cfg["voice"]["intelligence"].update(enabled=True, native_settle_seconds=0)
    cfg["voice"]["tts"].update({"enabled": True, "cache_dir": str(tmp),
                                "include_url": True, "async": True,
                                "audio_base_url": "http://127.0.0.1"})
    cfg["server"].update({"host": "127.0.0.1", "port": 0})
    cfg["execution"]["state_dir"] = str(tmp / "runtime")
    cfg["scheduler"]["store"] = str(tmp / "schedule.json")
    cfg["voice"]["conversation"]["shared_dir"] = str(tmp / "voice-sessions")
    cfg["memory"].update(enabled=False, path=str(tmp / "memory.sqlite3"))
    cfg["llm"]["provider"] = "none"
    # A tiny factual library so candidates are real, not invented.
    library = tmp / "library"
    show = library / "悬疑剧场 (2026)" / "Season 01"
    show.mkdir(parents=True, exist_ok=True)
    (show / "悬疑剧场 S01E001.mp4").write_bytes(b"video")
    (show.parent / "tvshow.nfo").write_text(
        "<tvshow><genre>悬疑</genre><country>中国大陆</country></tvshow>")
    second = library / "暗河谜案 (2025)" / "Season 01"
    second.mkdir(parents=True, exist_ok=True)
    (second / "暗河谜案 S01E001.mp4").write_bytes(b"video")
    (second.parent / "tvshow.nfo").write_text(
        "<tvshow><genre>悬疑</genre><country>中国大陆</country></tvshow>")
    cfg["media"].update({"enabled": True, "roots": [str(library)], "cache": "",
                         "serve": False})

    device = Mock()
    device.current_package.return_value = "com.trim.tv"
    device.playback_status.return_value = {"playing": False, "state": "PAUSED"}
    device.volume.return_value = {"known": True, "index": 5, "min": 0, "max": 100}
    agent = Mock()
    agent.run.return_value = {"status": "SUCCESS", "ok": True, "summary": "ok"}

    scheduler = Mock(tasks=[])
    scheduler.worker_status.return_value = {"status": "unknown", "message": "测试调度服务"}
    with patch("adbtv.server.build_context", lambda c: (Mock(), device, Mock(), agent, scheduler)):
        server = make_server(cfg)
    responder = server.voice_dialogue.responder
    (tmp / "e2e.wav").write_bytes(b"RIFF" + b"\0" * 64)
    responder.tts.available = lambda: (True, "")
    responder.tts.synthesize = lambda text: Synthesis(True, text, str(tmp / "e2e.wav"),
                                                      900, False, 1.0)
    responder.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    return server, thread, agent


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright 未安装（测试依赖），e2e 未运行")
class VoiceConsoleE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.pw = sync_playwright().start()
            # Playwright's bundled browser revision may not match this appliance;
            # AI_TV_CHROMIUM lets CI/tests reuse an already-installed Chromium.
            import os
            executable = os.environ.get("AI_TV_CHROMIUM") or None
            # Default to the full Chromium build (new headless mode); the separate
            # chrome-headless-shell may be absent on appliances.
            options = {"args": ["--no-sandbox"]}
            if executable:
                options["executable_path"] = executable
            else:
                options["channel"] = "chromium"
            cls.browser = cls.pw.chromium.launch(**options)
        except Exception as exc:  # noqa: BLE001
            cls.pw = None
            raise unittest.SkipTest(f"Chromium 不可启动，e2e 未运行: {str(exc)[:120]}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "pw", None):
            cls.pw.stop()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server, self.thread, self.agent = start_server(Path(self.tmp.name))
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = (Path(self.tmp.name) / "runtime" / "console.token").read_text().strip()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def page(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_default_timeout(8000)
        return page

    def wait_text(self, page, element_id, needle, timeout=15):
        """Poll the DOM from Python: CSP blocks in-page string evaluation."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if needle in page.inner_text(element_id):
                    return True
            except Exception:  # element not ready yet
                pass
            page.wait_for_timeout(200)
        self.fail(f"{element_id} 未在 {timeout}s 内出现 {needle!r}")

    def login(self, page):
        page.goto(self.base)
        page.fill("#token", self.token)
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#voice-panel", state="visible")

    def send(self, page, text):
        page.fill("#voice-text", text)
        page.click("#voice-form button[type=submit]")

    def test_login_rejects_wrong_token(self):
        page = self.page(); page.goto(self.base)
        page.fill("#token", "wrong-token-value")
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#login-error:not(:empty)")
        self.assertEqual(page.inner_text("#login-error"), "认证失效，请重新登录")
        self.assertTrue(page.locator("#login").is_visible())
        self.assertFalse(page.locator("#console").is_visible())

    def test_script_csp_blocks_inline_and_eval_without_alpine_errors(self):
        page = self.page()
        errors = []
        violations = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: violations.append(msg.text) if msg.type == "error" else None)
        self.login(page)
        self.assertIn("已启用", page.locator("#voice-panel h2").inner_text())
        self.assertFalse(errors, errors)
        self.assertFalse([v for v in violations if "Alpine" in v or "Content Security Policy" in v or "script-src" in v], violations)
        response = page.request.get(self.base)
        policy = response.headers["content-security-policy"]
        script = next(d.strip() for d in policy.split(";") if d.strip().startswith("script-src "))
        self.assertEqual(script, "script-src 'self'")
        # These probes exercise browser enforcement; Playwright evaluation itself
        # runs in a privileged context and cannot establish CSP compatibility.
        with self.assertRaisesRegex(PlaywrightError, "Content Security Policy"):
            page.add_script_tag(content="window.__inlineProbe = true")
        self.assertIsNone(page.evaluate("window.__inlineProbe"))
        self.assertTrue(any("Content Security Policy" in v or "script-src" in v for v in violations))
        page.route("**/static/csp-eval-probe.js", lambda route: route.fulfill(
            content_type="text/javascript", body="""
                try { new Function('return 1')(); window.__evalProbe = 'allowed'; }
                catch (error) { window.__evalProbe = error.name; }
            """))
        page.add_script_tag(url=self.base + "/static/csp-eval-probe.js")
        self.assertEqual(page.evaluate("window.__evalProbe"), "EvalError")

    def test_microphone_stub_candidates_confirm_audio_auth_and_cancel(self):
        """A SpeechRecognition stub exercises the mic UI, not actual capture."""
        page = self.page()
        page.add_init_script("""
            window.SpeechRecognition = class {
                start() {
                    setTimeout(() => this.onresult({results: [[{transcript: '找部悬疑剧'}]]}), 0);
                }
            };
        """)
        requests = []
        page.on("request", lambda r: requests.append(r))
        self.login(page)
        page.click("#voice-mic")
        page.wait_for_selector("#voice-candidates button")
        first_title = page.locator("#voice-candidates .row span").first.inner_text().split('. ', 1)[1].split(' (', 1)[0]
        page.locator("#voice-candidates button").first.click()
        self.wait_text(page, "#voice-reply", "已选择")
        self.assertIn(first_title, page.inner_text("#voice-reply"))
        self.send(page, "第一集")
        page.wait_for_selector("#voice-actions", state="visible")
        page.click("#voice-actions button[data-voice='确认']")
        self.wait_text(page, "#voice-session", "状态 SUCCESS")
        self.agent.run.assert_called_once()
        self.assertIn(first_title, self.agent.run.call_args.args[0])
        self.wait_text(page, "#voice-tts", "READY")
        if not any("/api/voice/audio/" in r.url for r in requests):
            page.wait_for_event("request", predicate=lambda r: "/api/voice/audio/" in r.url)
        audio = [r for r in requests if "/api/voice/audio/" in r.url]
        self.assertTrue(audio)
        for request in audio:
            self.assertEqual(request.headers.get("x-auth-token"), self.token)
            self.assertNotIn("token=", request.url)
        self.assertEqual(page.request.get(audio[0].url).status, 401)
        turns = [r for r in requests if r.method == "POST" and r.url.endswith("/api/voice")]
        self.assertEqual([r.post_data_json["text"] for r in turns], ["找部悬疑剧", "第1部", "第一集", "确认"])
        self.assertTrue(all(r.post_data_json["source"] == "web_microphone" for r in turns))
        self.assertTrue(all(r.headers.get("x-auth-token") == self.token for r in turns))
        self.assertFalse(any(r.method == "POST" and r.url.endswith("/api/runs") for r in requests))
        before = self.agent.run.call_count
        self.send(page, "播放悬疑剧场")
        page.wait_for_selector("#voice-actions", state="visible")
        page.click("#voice-actions button[data-voice='取消']")
        page.wait_for_selector("#voice-actions", state="hidden")
        self.assertEqual(self.agent.run.call_count, before, "取消不得执行播放")
        self.assertNotIn("NEEDS_CONFIRMATION", page.inner_text("#voice-session"))

    def test_alpine_panel_renders_and_posts_to_voice(self):
        page = self.page()
        requests = []
        page.on("request", lambda r: requests.append(r.url) if "/api/voice" in r.url else None)
        self.login(page)
        self.send(page, "找部悬疑剧")
        self.wait_text(page, "#voice-session", "状态 NEEDS")
        self.assertTrue(any(r.endswith("/api/voice") for r in requests),
                        "网页必须调用 /api/voice（而不是 /api/runs）")
        self.assertIn("路由", page.inner_text("#voice-session"))

    def test_candidates_render_and_selection_continues(self):
        page = self.page(); self.login(page)
        self.send(page, "找部悬疑剧")
        page.wait_for_selector("#voice-candidates button")
        first = page.locator("#voice-candidates button").first
        first.click()
        self.wait_text(page, "#voice-reply", "已选择")
        self.assertIn("会话", page.inner_text("#voice-session"))

    def test_confirmation_flow_and_cancel(self):
        page = self.page(); self.login(page)
        self.send(page, "播放悬疑剧场")   # exists in the fixture library
        page.wait_for_selector("#voice-actions", state="visible", timeout=15000)
        page.click("#voice-actions button[data-voice='取消']")
        deadline = time.time() + 15
        while time.time() < deadline and page.locator("#voice-actions").is_visible():
            page.wait_for_timeout(200)
        self.assertFalse(page.locator("#voice-actions").is_visible())
        self.assertNotIn("NEEDS_CONFIRMATION", page.inner_text("#voice-session"))

    def test_tts_job_reaches_ready_and_audio_is_fetched_with_token(self):
        page = self.page()
        audio_requests = []
        page.on("request", lambda r: audio_requests.append(r.headers)
                if "/api/voice/audio/" in r.url else None)
        self.login(page)
        self.send(page, "暂停")
        self.wait_text(page, "#voice-tts", "READY")
        page.wait_for_timeout(500)
        self.assertTrue(audio_requests, "READY 后必须用 fetch 取音频")
        self.assertTrue(any((h or {}).get("x-auth-token") for h in audio_requests),
                        "音频请求必须携带控制台令牌")

    def test_management_tabs_still_fetch_with_console_token(self):
        page = self.page()
        requests = []
        page.on("request", lambda request: requests.append(request))
        self.login(page)
        for tab, endpoint in (("skills", "/api/skills"), ("schedule", "/api/tasks"),
                              ("model", "/api/config")):
            with page.expect_response(lambda response: response.url.endswith(endpoint)
                                      and response.status == 200):
                page.click(f"[data-tab='{tab}']")
            self.assertTrue(page.locator(f"#tab-{tab}").is_visible())
        for endpoint in ("/api/runs", "/api/memory", "/api/skills", "/api/tasks", "/api/config"):
            matching = [r for r in requests if r.url.endswith(endpoint)]
            self.assertTrue(matching, endpoint)
            self.assertTrue(all(r.headers.get("x-auth-token") == self.token for r in matching), endpoint)

    def test_regular_task_form_still_uses_runs(self):
        page = self.page()
        runs = []
        page.on("request", lambda r: runs.append(r) if r.method == "POST" and r.url.endswith("/api/runs") else None)
        self.login(page)
        page.fill("#command", "当前应用是什么")
        page.click("#command-form button[type=submit]")
        self.wait_text(page, "#task-result", "SUCCESS")
        self.assertTrue(runs, "普通任务仍必须 POST 到 /api/runs")
        self.assertEqual(runs[0].headers.get("x-auth-token"), self.token)
        self.assertEqual(runs[0].post_data_json, {"command": "当前应用是什么"})


if __name__ == "__main__":
    unittest.main()