"""V4.0.0b3 regressions: cross-container confirmation and TTS worker survival."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.voice_input import SharedSessionStore, VoiceInputRegistry
from adbtv.voice_intelligence import IntelligentVoiceRouter
from adbtv.voice_response import VoiceResponder
from adbtv.voice_runtime import VoiceDialogue
from adbtv.voice_interrupt import InterruptChannel
from adbtv.tts import Synthesis
from tests.test_voice_dialogue_v40 import Catalog


def config(**voice):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["voice"]["intelligence"].update(enabled=True, native_settle_seconds=0)
    for key, value in voice.items():
        cfg["voice"][key] = dict(cfg["voice"].get(key) or {}, **value)
    return cfg


def build(store, cfg=None, tv="tv-1", device=None):
    cfg = cfg or config()
    agent = Mock()
    agent.run.return_value = {"status": "SUCCESS", "ok": True}
    dialogue = VoiceDialogue(cfg, agent=agent, catalog=Catalog(), device=device,
                             router=IntelligentVoiceRouter(cfg, Mock(enabled=False)))
    return VoiceInputRegistry(dialogue, store=store, tv_id=tv), agent


class CrossContainerConfirmationTests(unittest.TestCase):
    """P0-1: a confirmation must survive process boundaries and be one-shot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SharedSessionStore(self.tmp.name)

    def test_voice_then_console_confirmation_executes_play(self):
        voice, agent = build(self.store)
        self.assertEqual(voice.submit_text("播放三体", source="xiaomi_logcat")["status"],
                         "NEEDS_CONFIRMATION")
        console, console_agent = build(self.store)          # "second process"
        result = console.submit_text("确认", source="web_microphone")
        self.assertEqual(result["status"], "SUCCESS", result)
        self.assertEqual(console_agent.run.call_count, 1)
        self.assertIn("三体", console_agent.run.call_args[0][0])
        agent.run.assert_not_called()

    def test_console_then_voice_confirmation(self):
        console, agent = build(self.store)
        console.submit_text("播放三体", source="web_microphone")
        voice, voice_agent = build(self.store)
        self.assertEqual(voice.submit_text("确认", source="xiaomi_logcat")["status"], "SUCCESS")
        self.assertEqual(voice_agent.run.call_count, 1)

    def test_session_id_is_unified_across_processes(self):
        voice, _ = build(self.store)
        first = voice.submit_text("播放三体", source="xiaomi_logcat")["session_id"]
        console, _ = build(self.store)
        second = console.submit_text("确认", source="web_microphone")["session_id"]
        self.assertEqual(first, second)

    def test_confirmation_is_one_shot(self):
        voice, _ = build(self.store)
        voice.submit_text("播放三体", source="xiaomi_logcat")
        console, agent = build(self.store)
        self.assertEqual(console.submit_text("确认", source="web_microphone")["status"], "SUCCESS")
        again = build(self.store)[0].submit_text("确认", source="web_microphone")
        self.assertEqual(again["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(agent.run.call_count, 1)

    def test_expired_confirmation_is_not_executed(self):
        clock = [1000.0]
        cfg = config(conversation={"ttl_seconds": 20, "max_turns": 8})
        voice, _ = build(self.store, cfg)
        voice.dialogue.sessions._clock = lambda: clock[0]
        voice.dialogue.clock = lambda: clock[0]
        voice.submit_text("播放三体", source="xiaomi_logcat")
        clock[0] += 30
        console, agent = build(self.store, cfg)
        console.dialogue.sessions._clock = lambda: clock[0]
        console.dialogue.clock = lambda: clock[0]
        result = console.submit_text("确认", source="web_microphone")
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION", result)
        agent.run.assert_not_called()

    def test_new_request_invalidates_pending_confirmation(self):
        voice, _ = build(self.store)
        voice.submit_text("播放三体", source="xiaomi_logcat")
        voice.submit_text("今天天气怎么样", source="xiaomi_logcat")
        console, agent = build(self.store)
        self.assertEqual(console.submit_text("确认", source="web_microphone")["status"],
                         "NEEDS_CLARIFICATION")
        agent.run.assert_not_called()

    def test_target_is_revalidated_before_execution(self):
        voice, _ = build(self.store)
        voice.submit_text("播放三体", source="xiaomi_logcat")
        # A different catalogue: the confirmed title no longer resolves.
        cfg = config()
        agent = Mock()
        agent.run.return_value = {"status": "SUCCESS", "ok": True}
        empty_catalog = Mock()
        empty_catalog.search.return_value = []
        empty_catalog.exact_title.return_value = False
        empty_catalog.resolve.return_value = None
        dialogue = VoiceDialogue(cfg, agent=agent, catalog=empty_catalog,
                                 router=IntelligentVoiceRouter(cfg, Mock(enabled=False)))
        console = VoiceInputRegistry(dialogue, store=self.store, tv_id="tv-1")
        result = console.submit_text("确认", source="web_microphone")
        self.assertNotEqual(result["status"], "SUCCESS", result)
        agent.run.assert_not_called()

    def test_concurrent_submissions_do_not_lose_pending_state(self):
        voice, _ = build(self.store)
        voice.submit_text("播放三体", source="xiaomi_logcat")
        errors = []
        def confirm():
            try:
                build(self.store)[0].submit_text("确认", source="web_microphone")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=confirm) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        saved = self.store.load("tv-1")
        self.assertIn("session_id", saved)

    def test_pending_never_stores_shell_text(self):
        voice, _ = build(self.store)
        voice.submit_text("播放三体", source="xiaomi_logcat")
        import hashlib
        name = "session-" + hashlib.sha256(b"tv-1").hexdigest()[:24] + ".json"
        raw = (Path(self.tmp.name) / name).read_text()
        self.assertNotIn("adb shell", raw)
        self.assertNotIn(";", raw)
        pending = json.loads(raw).get("pending_action")
        self.assertIsInstance(pending, dict)
        self.assertIn("command", pending)


class TTSWorkerSurvivalTests(unittest.TestCase):
    """P0-2: a missing audio URL must never kill the synthesis worker."""

    def responder(self, **tts):
        base = dict(enabled=True, include_url=True, audio_base_url="")
        base.update(tts)
        cfg = config(tts=base)
        responder = VoiceResponder(cfg)
        responder.tts.synthesize = lambda text: Synthesis(True, text, "/tmp/x.wav", 900, False, 1.0)
        return responder

    def test_missing_url_does_not_kill_worker(self):
        responder = self.responder()
        responder.respond("success", "已经暂停")
        for _ in range(100):
            if not (responder._worker and responder._worker.is_alive()) and responder._queue.qsize() == 0:
                break
            time.sleep(0.02)
        alive = bool(responder._worker and responder._worker.is_alive())
        self.assertTrue(alive, "worker must keep running without an audio URL")
        stats = responder.stats()
        self.assertEqual(stats["delivered"], 0, "no URL means it cannot be delivered")
        self.assertGreaterEqual(stats["synthesized"], 1)

    def test_hundred_requests_keep_worker_healthy(self):
        responder = self.responder(audio_base_url="http://nas")
        for index in range(100):
            responder.respond("success", f"第{index}条播报")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if responder.stats()["queued"] == 0:
                break
            time.sleep(0.05)
        stats = responder.stats()
        self.assertTrue(responder._worker and responder._worker.is_alive())
        self.assertEqual(stats["failures"], 0, "overflow is capacity, not a crash")
        self.assertEqual(stats["synthesized"], stats["delivered"])
        # Every request is either delivered or explicitly reported as overflow.
        self.assertEqual(stats["delivered"] + stats["overflow"], 100, stats)
        self.assertEqual(stats["spoken"], stats["delivered"], "spoken must not double count")

    def test_queue_full_is_reported_honestly(self):
        responder = self.responder(audio_base_url="http://nas")
        responder.tts.synthesize = lambda text: (time.sleep(0.2) or
                                                 Synthesis(True, text, "/tmp/x.wav", 900, False, 1.0))
        results = [responder.respond("success", f"慢{index}") for index in range(6)]
        self.assertTrue(any(r.get("reason") == "tts_queue_full" for r in results),
                        "queue overflow must be visible, not silent success")
        overflow = [r for r in results if r.get("reason") == "tts_queue_full"]
        for result in overflow:
            self.assertFalse(result["speak"], "overflow must not claim speech")

    def test_worker_recovers_after_synthesis_exception(self):
        responder = self.responder(audio_base_url="http://nas")
        calls = {"n": 0}
        def flaky(text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return Synthesis(True, text, "/tmp/x.wav", 900, False, 1.0)
        responder.tts.synthesize = flaky
        responder.respond("success", "第一条")
        responder.respond("success", "第二条")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if responder.stats()["delivered"] >= 1:
                break
            time.sleep(0.05)
        self.assertTrue(responder._worker and responder._worker.is_alive())
        self.assertEqual(responder.stats()["delivered"], 1)
        self.assertGreaterEqual(responder.stats()["failures"], 1)

    def test_status_declares_tv_audio_unavailable_and_auth_policy(self):
        responder = self.responder()
        status = responder.status()
        self.assertFalse(status["tv_audio"])
        self.assertIn("delivery", status)
        self.assertIn("auth", status)


if __name__ == "__main__":
    unittest.main()

class CrossContainerInterruptTests(unittest.TestCase):
    """P1-1: cancellation across containers, authorisation and async media."""

    def channel(self, tasks, *, roles=None, prefixes=None, device=None):
        cfg = config()
        cfg["execution"]["state_dir"] = self.tmp.name
        if roles is not None:
            cfg["voice"]["interrupt"] = {"roles": roles}
        if prefixes is not None:
            cfg["voice"]["listener"]["prefix_only"] = prefixes
        directory = Path(self.tmp.name) / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        serial = (cfg.get("adb") or {}).get("serial") or ""
        for record in tasks:
            record.setdefault("device", serial)
            (directory / (record["id"] + ".json")).write_text(json.dumps(record))
        return InterruptChannel(cfg, device=device or Mock())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_console_task_is_cancelled_by_voice_entry(self):
        channel = self.channel([{"id": "cons01", "state": "RUNNING", "pid": 4242,
                                 "role": "console", "instruction": "x",
                                 "started": time.time()}])
        result = channel.handle("取消任务")
        self.assertEqual(result.status, "INTERRUPTED")
        self.assertTrue((Path(self.tmp.name) / "tasks" / "cons01.control.json").is_file())

    def test_scheduler_task_is_cancelled(self):
        channel = self.channel([{"id": "sch01", "state": "RUNNING", "pid": 4343,
                                 "role": "scheduler", "instruction": "x",
                                 "started": time.time()}])
        self.assertEqual(channel.handle("取消任务").status, "INTERRUPTED")

    def test_other_tv_is_never_cancelled(self):
        channel = self.channel([{"id": "othertv", "state": "RUNNING", "pid": 4242,
                                 "role": "console", "device": "another-tv:5555",
                                 "instruction": "x", "started": time.time()}])
        self.assertEqual(channel.handle("取消任务").status, "NO_TASK")

    def test_unauthorised_entry_cannot_interrupt(self):
        channel = self.channel([{"id": "cons02", "state": "RUNNING", "pid": 4242,
                                 "role": "console", "instruction": "x",
                                 "started": time.time()}], prefixes=["电视管家"])
        denied = channel.handle("取消任务")
        self.assertEqual(denied.status, "UNAUTHORIZED")
        self.assertFalse((Path(self.tmp.name) / "tasks" / "cons02.control.json").exists())

    def test_slow_media_action_does_not_block_caller(self):
        device = Mock()
        device.key.side_effect = lambda *a: time.sleep(1.5)
        device.playback_status.return_value = {"playing": False, "state": ""}
        channel = self.channel([], device=device)
        started = time.monotonic()
        result = channel.handle("停止播放")          # default: no wait
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, "ASR reader must not block on ADB")
        self.assertEqual(result.status, "PENDING")
        # The worker still completes the action in the background.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not device.key.called:
            time.sleep(0.05)
        self.assertTrue(device.key.called)


class ComplexSemanticsTests(unittest.TestCase):
    """P1-2: the six phrasings must work on factual state, never model guesses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SharedSessionStore(self.tmp.name)
        self.registry, self.agent = build(self.store)

    def say(self, text, confirm=False):
        result = self.registry.submit_text(text, source="web_microphone")
        if confirm and result["status"] == "NEEDS_CONFIRMATION":
            return self.registry.submit_text("确认", source="web_microphone")
        return result

    def test_play_history_enables_previous_item(self):
        self.say("播放三体", confirm=True)
        self.assertIn("三体", self.agent.run.call_args[0][0])
        result = self.say("算了，还是看上一部吧")
        self.assertEqual(result["status"], "NEEDS_CONFIRMATION", result)
        self.say("确认")
        self.assertIn("三体", self.agent.run.call_args[0][0])

    def test_similar_request_uses_real_metadata_only(self):
        self.say("播放三体", confirm=True)
        result = self.say("找一部类似刚才那部的剧")
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION", result)
        # Candidates must come from the factual catalogue and exclude the source.
        self.assertTrue(result["candidates"])
        self.assertNotIn("三体", [c["title"] for c in result["candidates"]])

    def test_swap_to_second_candidate(self):
        self.say("找部悬疑剧")
        self.say("第二部")           # selects 暗河
        result = self.say("不是这个，换第二个")
        self.assertEqual(result["status"], "NEEDS_CONFIRMATION", result)
        self.say("确认")
        self.assertIn("外乡谜案", self.agent.run.call_args[0][0])

    def test_resume_last_episode_uses_recorded_progress(self):
        self.say("播放三体", confirm=True)
        result = self.say("继续刚才看到的那一集")
        self.assertEqual(result["status"], "NEEDS_CONFIRMATION", result)
        self.say("确认")
        self.assertIn("三体", self.agent.run.call_args[0][0])
        command = self.agent.run.call_args[0][0]
        self.assertTrue("第1集" in command or "第一集" in command, command)

    def test_open_app_then_set_volume_in_one_utterance(self):
        result = self.say("打开 B 站，把音量设置成 8")
        self.assertEqual(result["status"], "NEEDS_CONFIRMATION", result)
        self.say("确认")
        command = self.agent.run.call_args[0][0]
        self.assertIn("B", command.upper())
        self.assertIn("音量", command)

    def test_search_then_first_episode_cannot_guess_series(self):
        result = self.say("先找国产悬疑剧，再从第一集开始")
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION", result)
        self.assertTrue(result["candidates"], "must show real candidates before playing")
        self.agent.run.assert_not_called()
