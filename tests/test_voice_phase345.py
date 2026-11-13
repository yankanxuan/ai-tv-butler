"""Phase 3/4/5 tests: TTS policy, priority interruption, pluggable inputs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.tts import PiperTTS, TTSError
from adbtv.voice_response import VoiceResponder
from adbtv.voice_interrupt import InterruptChannel, classify_interrupt
from adbtv.voice import VoiceListener, Utterance


def channel_device():
    """Device id used by the test configuration, shared with task records."""
    cfg_ = copy.deepcopy(DEFAULTS)
    adb = cfg_.get("adb") or {}
    return adb.get("serial") or adb.get("host") or ""


def cfg(**voice):
    c = copy.deepcopy(DEFAULTS)
    c["voice"].update(voice)
    c["voice"].setdefault("tts", {})
    return c


class TTSTests(unittest.TestCase):
    def test_synthesis_is_private_bounded_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            tts = PiperTTS(DEFAULTS, binary=sys.executable, model=__file__,
                           cache_dir=tmp)
            # Fake piper: write a minimal valid WAV to the -f target.
            script = Path(tmp) / "fake_piper.py"
            script.write_text(
                "import sys,struct\n"
                "args=sys.argv\n"
                "out=args[args.index('-f')+1]\n"
                "data=b'\\x00\\x00'*2400\n"
                "with open(out,'wb') as f:\n"
                " f.write(b'RIFF'+struct.pack('<I',36+len(data))+b'WAVEfmt ')\n"
                " f.write(struct.pack('<IHHIIHH',16,1,1,8000,16000,2,16))\n"
                " f.write(b'data'+struct.pack('<I',len(data))+data)\n")
            tts.binary = sys.executable
            tts.model = str(script)
            # Use python -c via a wrapper so argv matches piper's -m/-f interface.
            wrapper = Path(tmp) / "piper_wrap"
            wrapper.write_text(
                "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, script))
            wrapper.chmod(0o755)
            tts.binary = str(wrapper)
            tts.model = "unused"
            tts.available = lambda: (True, "")
            first = tts.synthesize("已开始播放")
            self.assertTrue(first.ok, first.error)
            self.assertFalse(first.cached)
            self.assertGreater(first.duration_ms, 0)
            mode = os.stat(first.path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            second = tts.synthesize("已开始播放")
            self.assertTrue(second.cached)
            self.assertEqual(first.path, second.path)

    def test_empty_and_oversized_text_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tts = PiperTTS(DEFAULTS, cache_dir=tmp)
            self.assertFalse(tts.synthesize("").ok)
            self.assertFalse(tts.synthesize("长" * 500).ok)

    def test_missing_binary_reports_error_not_success(self):
        tts = PiperTTS(DEFAULTS, binary="/nonexistent/piper", model="/nonexistent")
        result = tts.synthesize("测试")
        self.assertFalse(result.ok)
        self.assertIn("piper", result.error)

    def test_real_piper_model_available_on_this_host(self):
        """Availability is environment-dependent; never assert playback."""
        tts = PiperTTS(DEFAULTS, model=str(Path.home() / ".piper/models/zh_CN-huayan-medium.onnx"))
        ok, reason = tts.available()
        if ok:
            result = tts.synthesize("测试")
            self.assertTrue(result.ok, result.error)
            self.assertGreater(result.duration_ms, 0)
        else:
            self.assertTrue(reason)


class ResponsePolicyTests(unittest.TestCase):
    def responder(self, asynchronous=True, **opts):
        c = cfg(tts=dict(enabled=True, **opts))
        c["voice"]["tts"]["async"] = asynchronous
        return VoiceResponder(c, tts=Mock(synthesize=lambda t: type(
            "S", (), {"ok": True, "path": "/tmp/x.wav", "duration_ms": 900,
                      "cached": False, "elapsed_ms": 3.0, "text": t, "error": "",
                      "to_dict": lambda self=None: {"ok": True},
                      "audio_url": lambda self, b: b + "/api/voice/audio/x.wav"})()),
            base_url="http://nas")

    def test_disabled_never_speaks(self):
        c = cfg(tts={"enabled": False})
        responder = VoiceResponder(c)
        plan = responder.plan("success", "已完成")
        self.assertFalse(plan.speak)
        self.assertEqual(plan.reason, "tts_disabled")

    def test_video_playback_suppresses_routine_speech(self):
        plan = self.responder().plan("success", "开始播放", playback_state="PLAYING")
        self.assertFalse(plan.speak)
        self.assertEqual(plan.reason, "video_playing")

    def test_failure_is_spoken_even_during_playback(self):
        plan = self.responder().plan("failure", "播放失败", playback_state="PLAYING")
        self.assertTrue(plan.speak)

    def test_confirmation_stays_text(self):
        plan = self.responder().plan("confirm", "确认吗")
        self.assertFalse(plan.speak)
        self.assertEqual(plan.channel, "tv_text")

    def test_start_ack_is_opt_in(self):
        self.assertFalse(self.responder().plan("start", "开始").speak)
        self.assertTrue(self.responder(ack_on_start=True).plan("start", "开始").speak)

    def test_async_response_is_pending_and_returns_immediately(self):
        responder = self.responder()
        started = time.monotonic()
        result = responder.respond("success", "已播放")
        self.assertLess(time.monotonic() - started, 0.2, "合成不得阻塞调用方")
        self.assertTrue(result["speak"])
        self.assertTrue(result["pending"])
        self.assertIsNone(result["audio_url"])
        self.assertIsNone(result["tts_ms"])

    def test_synchronous_delivery_reports_url_and_tts_ms(self):
        responder = self.responder(asynchronous=False)
        result = responder.respond("success", "已播放")
        self.assertTrue(result["speak"])
        self.assertEqual(result["channel"], "web_audio")
        self.assertIn("/api/voice/audio/", result["audio_url"])
        self.assertGreaterEqual(result["tts_ms"], 0)

    def test_suppressed_response_is_not_queued(self):
        responder = self.responder()
        result = responder.respond("success", "开始播放", playback_state="PLAYING")
        self.assertFalse(result["speak"])
        self.assertEqual(responder.stats()["queued"], 0)


class InterruptClassificationTests(unittest.TestCase):
    def test_task_and_media_are_distinct(self):
        self.assertEqual(classify_interrupt("取消任务"), ("cancel", "task"))
        self.assertEqual(classify_interrupt("停止播放"), ("cancel", "media"))
        self.assertEqual(classify_interrupt("暂停"), ("pause", "media"))
        self.assertEqual(classify_interrupt("暂停任务"), ("pause", "task"))
        self.assertEqual(classify_interrupt("停下"), ("ask", "ambiguous"))
        self.assertIsNone(classify_interrupt("播放三体"))
        self.assertIsNone(classify_interrupt("取消任务之后播放三体"))


class InterruptChannelTests(unittest.TestCase):
    def make(self, tasks):
        cfg_ = copy.deepcopy(DEFAULTS)
        self.tmp = tempfile.mkdtemp()
        cfg_["execution"]["state_dir"] = self.tmp
        directory = Path(self.tmp) / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        for record in tasks:
            (directory / (record["id"] + ".json")).write_text(json.dumps(record))
        return InterruptChannel(cfg_, device=Mock())

    def test_cancel_writes_control_for_own_running_task(self):
        channel = self.make([{"id": "abc123", "state": "RUNNING", "pid": os.getpid(),
                              "instruction": "x", "started": time.time()}])
        result = channel.handle("取消任务")
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "task")
        control = Path(self.tmp) / "tasks" / "abc123.control.json"
        self.assertTrue(control.is_file())
        self.assertEqual(json.loads(control.read_text()).get("action"), "cancel")

    def test_cancels_task_owned_by_another_container_for_same_tv(self):
        # Same device, different pid: cross-container cancel is the V4.0.0b3 rule.
        channel = self.make([{"id": "other1", "state": "RUNNING", "pid": 999999,
                              "device": channel_device(), "role": "console",
                              "instruction": "x", "started": time.time()}])
        result = channel.handle("取消任务")
        self.assertEqual(result.status, "INTERRUPTED")
        self.assertTrue((Path(self.tmp) / "tasks" / "other1.control.json").exists())

    def test_refuses_task_on_another_tv(self):
        channel = self.make([{"id": "othertv", "state": "RUNNING", "pid": 999999,
                              "device": "another-tv:5555", "role": "console",
                              "instruction": "x", "started": time.time()}])
        self.assertEqual(channel.handle("取消任务").status, "NO_TASK")
        self.assertFalse((Path(self.tmp) / "tasks" / "othertv.control.json").exists())

    def test_refuses_disallowed_owner_role(self):
        cfg_ = copy.deepcopy(DEFAULTS)
        self.tmp = tempfile.mkdtemp()
        cfg_["execution"]["state_dir"] = self.tmp
        cfg_["voice"]["interrupt"] = {"roles": ["voice"]}
        directory = Path(self.tmp) / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cons1.json").write_text(json.dumps(
            {"id": "cons1", "state": "RUNNING", "pid": 1, "role": "console",
             "device": channel_device(), "instruction": "x", "started": time.time()}))
        channel = InterruptChannel(cfg_, device=Mock())
        self.assertEqual(channel.handle("取消任务").status, "NO_TASK")

    def test_prefix_policy_applies_to_interrupts(self):
        cfg_ = copy.deepcopy(DEFAULTS)
        cfg_["voice"]["listener"]["prefix_only"] = ["电视管家"]
        channel = InterruptChannel(cfg_, device=Mock())
        denied = channel.handle("取消任务")
        self.assertEqual(denied.status, "UNAUTHORIZED")
        allowed = channel.handle("电视管家取消任务")
        self.assertEqual(allowed.status, "NO_TASK")

    def test_no_task_is_truthful_noop(self):
        channel = self.make([])
        result = channel.handle("取消任务")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "NO_TASK")

    def test_media_stop_uses_home_and_verifies_state(self):
        device = Mock()
        device.playback_status.return_value = {"playing": False, "state": ""}
        channel = self.make([])
        channel.device = device
        result = channel.handle("停止播放", wait=3.0)
        self.assertEqual(result.kind, "media")
        device.key.assert_called_once_with("home")

    def test_ambiguous_stop_asks_instead_of_acting(self):
        channel = self.make([])
        result = channel.handle("停下")
        self.assertEqual(result.status, "NEEDS_CLARIFICATION")
        channel.device.key.assert_not_called()

    def test_takeover_cancels_and_marks_state(self):
        channel = self.make([{"id": "abc123", "state": "RUNNING", "pid": os.getpid(),
                              "instruction": "x", "started": time.time()}])
        self.assertTrue(channel.handle("人工接管").ok)
        self.assertTrue(channel.takeover)


class PriorityListenerTests(unittest.TestCase):
    def listener(self, tmp, slow=False):
        c = copy.deepcopy(DEFAULTS)
        c["voice"]["intelligence"]["enabled"] = True
        c["voice"]["listener"].update(state_file=str(Path(tmp) / "s.json"), queue_size=2)
        calls = []
        def interrupt(text):
            calls.append(text)
            return {"ok": True, "status": "INTERRUPTED", "action": "cancel", "kind": "task"}
        slow_handler = (lambda u: time.sleep(1)) if slow else (lambda u: None)
        return VoiceListener(c, slow_handler, on_interrupt=interrupt), calls

    def line(self, text):
        return str(time.time()) + " I tag: processAsrResult " + json.dumps(
            {"payload": {"is_final": True, "results": [{"text": text}]}})

    def test_interrupt_bypasses_full_queue_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener, calls = self.listener(tmp, slow=True)
            listener.start()
            try:
                for text in ["第一条", "第二条", "第三条"]:
                    listener.ingest_line(self.line(text))
                self.assertEqual(listener._queue.qsize(), 2)   # bounded, overflowed
                started = time.monotonic()
                self.assertTrue(listener.ingest_line(self.line("取消任务")))
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertEqual(calls, ["取消任务"])
                self.assertEqual(listener.stats()["interrupts"], 1)
            finally:
                listener.stop()

    def test_interrupt_not_enqueued_as_normal_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener, calls = self.listener(tmp)
            listener.ingest_line(self.line("取消任务"))
            self.assertEqual(listener._queue.qsize(), 0)
            self.assertEqual(calls, ["取消任务"])

    def test_media_pause_is_not_treated_as_agent_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener, calls = self.listener(tmp)
            listener.ingest_line(self.line("暂停播放"))
            self.assertEqual(calls, ["暂停播放"])
            self.assertEqual(listener._queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()

class SharedSessionTests(unittest.TestCase):
    def test_store_roundtrip_is_private_and_validated(self):
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            store = __import__("adbtv.voice_input", fromlist=["SharedSessionStore"]).SharedSessionStore(tmp)
            state = {"current_app": "com.trim.tv", "recent_results": [
                {"id": "a1", "title": "三体", "genres": ["科幻"]}],
                "selected_content": {"id": "a1", "title": "三体"},
                "current_playback": {"title": "三体", "state": "playing"},
                "injected": "rm -rf /"}
            store.save("tv-1", state)
            files = list(Path(tmp).glob("session-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            loaded = store.load("tv-1")
            self.assertEqual(loaded["selected_content"]["title"], "三体")
            self.assertEqual(store.load("tv-2"), None)

    def test_hydrate_ignores_unsafe_values(self):
        from adbtv.conversation import ConversationManager
        manager = ConversationManager()
        manager.hydrate("tv", {"current_app": "com.trim.tv",
                               "recent_results": [{"id": "x", "title": "三体"},
                                                  {"id": "y", "title": "../etc/passwd"}],
                               "selected_content": {"id": "x", "title": "三体"},
                               "current_playback": {"title": "三体", "state": "playing"}})
        snap = manager.snapshot("tv")
        self.assertEqual(len(snap["recent_results"]), 1)
        self.assertEqual(snap["current_playback"]["state"], "playing")

    def test_registry_shares_one_session_across_sources(self):
        from adbtv.voice_input import VoiceInputRegistry, SharedSessionStore, UnsupportedInput
        from adbtv.voice_runtime import VoiceDialogue
        from adbtv.voice_intelligence import IntelligentVoiceRouter
        from tests.test_voice_dialogue_v40 import Catalog
        c = cfg(intelligence={"enabled": True, "native_settle_seconds": 0})
        agent = Mock(); agent.run.return_value = {"status": "SUCCESS", "ok": True}
        dialogue = VoiceDialogue(c, agent=agent, catalog=Catalog(),
                                 router=IntelligentVoiceRouter(c, Mock(enabled=False)))
        with tempfile.TemporaryDirectory() as tmp:
            store = SharedSessionStore(tmp)
            mic = VoiceInputRegistry(dialogue, store=store, tv_id="tv")
            mic.submit_text("找部悬疑剧", source="web_microphone")
            # A second process would hydrate this state from the same file.
            saved = store.load("tv")
            self.assertEqual(len(saved["recent_results"]), 3)
            with self.assertRaises(UnsupportedInput):
                mic.submit_text("找部剧", source="external_asr")
            self.assertFalse(mic.status()["continuous_listening"])
            self.assertFalse(mic.status()["sources"]["external_asr"]["implemented"])
