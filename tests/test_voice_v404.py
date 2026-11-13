"""V4.0.0b4: shared-session replacement, expiry and Web/TTS delivery."""
import copy
import json
import time
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.conversation import ConversationManager
from adbtv.voice_input import SharedSessionStore


def snapshot_without_selection():
    return {"session_id": None, "current_app": "com.trim.tv",
            "recent_results": [{"id": "b", "title": "暗河"}],
            "current_playback": None, "pending_action": None,
            "played_history": []}


class HydrateReplacementTests(unittest.TestCase):
    def manager(self, clock=None):
        return ConversationManager(ttl_seconds=180, clock=clock or time.monotonic)

    def test_hydrate_clears_selection_absent_from_snapshot(self):
        manager = self.manager()
        manager.set_results("tv", [{"id": "a", "title": "三体"}], source="library")
        manager.select("tv", index=1)
        manager.hydrate("tv", snapshot_without_selection())
        self.assertIsNone(manager.snapshot("tv")["selected_content"],
                          "a snapshot without a selection must clear the old one")

    def test_hydrate_replaces_every_shared_field(self):
        manager = self.manager()
        manager.set_results("tv", [{"id": "a", "title": "三体"}], source="library")
        manager.select("tv", index=1)
        manager.update_observation("tv", app="com.mitv.mivideoplayer",
                                   playback={"title": "三体", "state": "playing"})
        manager.remember_play("tv", {"id": "a", "title": "三体"})
        manager.ask("tv", "selection", "请选择")
        manager.hydrate("tv", snapshot_without_selection())
        snap = manager.snapshot("tv")
        self.assertEqual(snap["current_app"], "com.trim.tv")
        self.assertEqual([r["id"] for r in snap["recent_results"]], ["b"])
        self.assertIsNone(snap["selected_content"])
        self.assertIsNone(snap["current_playback"])
        self.assertEqual(snap["played_history"], [])
        self.assertIsNone(snap["waiting_question"])

    def test_expired_shared_snapshot_is_not_restored(self):
        clock = [1000.0]
        manager = self.manager(clock=lambda: clock[0])
        manager.set_results("tv", [{"id": "a", "title": "三体"}], source="library")
        exported = manager.export("tv")
        self.assertIn("expires_at", exported)
        clock[0] = float(exported["expires_at"]) + 1
        restored = self.manager(clock=lambda: clock[0])
        self.assertFalse(restored.hydrate("tv", exported))
        snap = restored.snapshot("tv")
        self.assertEqual(snap["recent_results"], [])
        self.assertIsNone(snap["selected_content"])

    def test_live_snapshot_restores_selection_and_deadline(self):
        clock = [2000.0]
        manager = self.manager(clock=lambda: clock[0])
        manager.set_results("tv", [{"id": "a", "title": "三体"}], source="library")
        manager.select("tv", index=1)
        exported = manager.export("tv")
        restored = self.manager(clock=lambda: clock[0])
        self.assertTrue(restored.hydrate("tv", exported))
        self.assertEqual(restored.snapshot("tv")["selected_content"]["title"], "三体")
        clock[0] = float(exported["expires_at"]) - 1
        self.assertTrue(restored.hydrate("tv", exported), "still inside the idle window")

    def test_corrupt_snapshot_fails_safe(self):
        manager = self.manager()
        for broken in ({"expires_at": "not-a-number"},
                       {"recent_results": "not-a-list"},
                       {"selected_content": {"id": "../../etc/passwd"}},
                       {"pending_action": {"nonce": "zz", "command": "adb shell rm -rf /"}}):
            with self.subTest(broken=broken):
                # Must not raise and must not import unsafe values.
                manager.hydrate("tv", broken)
                snap = manager.snapshot("tv")
                self.assertIsNone(snap["pending_action"])
                for row in snap["recent_results"]:
                    self.assertNotIn("..", row["id"])

    def test_pending_confirmation_keeps_independent_ttl(self):
        clock = [3000.0]
        manager = self.manager(clock=lambda: clock[0])
        manager.set_pending("tv", nonce="a" * 32, command="播放《三体》第1季第一集")
        self.assertIsNotNone(manager.take_pending("tv"))
        manager.set_pending("tv", nonce="b" * 32, command="播放《三体》第1季第一集")
        clock[0] += 61          # beyond the 60 s confirmation window
        self.assertIsNone(manager.take_pending("tv"))


class SharedStoreConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SharedSessionStore(self.tmp.name)

    def test_two_containers_clear_then_restore(self):
        first = ConversationManager()
        first.set_results("tv", [{"id": "a", "title": "三体"}], source="library")
        first.select("tv", index=1)
        with self.store.transaction("tv") as state:
            state.clear()
            state.update(first.export("tv"))
        # Second container clears the selection explicitly.
        second = ConversationManager()
        second.hydrate("tv", self.store.load("tv"))
        second.set_results("tv", [{"id": "b", "title": "暗河"}], source="library")
        with self.store.transaction("tv") as state:
            state.clear()
            state.update(second.export("tv"))
        third = ConversationManager()
        third.hydrate("tv", self.store.load("tv"))
        snap = third.snapshot("tv")
        self.assertEqual([r["id"] for r in snap["recent_results"]], ["b"])
        self.assertIsNone(snap["selected_content"], "cleared selection must not come back")

    def test_session_id_preserved_across_round_trips(self):
        manager = ConversationManager()
        original = manager.snapshot("tv")["session_id"]
        with self.store.transaction("tv") as state:
            state.clear()
            state.update(manager.export("tv"))
        restored = ConversationManager()
        restored.hydrate("tv", self.store.load("tv"))
        self.assertEqual(restored.snapshot("tv")["session_id"], original)


if __name__ == "__main__":
    unittest.main()

class TTSJobDeliveryTests(unittest.TestCase):
    """Task 3: per-request jobs, READY audio via authenticated fetch."""

    def build(self, *, available=True, synthesize=None):
        from adbtv.voice_response import VoiceResponder
        from adbtv.tts import Synthesis
        cfg = copy.deepcopy(DEFAULTS)
        cfg["voice"]["tts"].update({"enabled": True, "include_url": True,
                                    "audio_base_url": "http://nas:8771", "async": True})
        responder = VoiceResponder(cfg)
        responder.tts.available = lambda: (True, "") if available else (False, "piper 不可用")
        responder.tts.synthesize = synthesize or (
            lambda text: Synthesis(True, text, "/tmp/x.wav", 900, False, 1.0))
        return responder

    def wait(self, responder, job_id, states=("READY", "FAILED"), timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = responder.job(job_id)
            if job and job["state"] in states:
                return job
            time.sleep(0.02)
        return responder.job(job_id)

    def test_every_request_gets_a_unique_job(self):
        responder = self.build()
        first = responder.respond("success", "第一条")
        second = responder.respond("success", "第二条")
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["job_state"], "QUEUED")

    def test_job_reaches_ready_with_audio_url(self):
        responder = self.build()
        result = responder.respond("success", "已完成")
        job = self.wait(responder, result["job_id"])
        self.assertEqual(job["state"], "READY")
        self.assertIn("/api/voice/audio/", job["audio_url"])
        self.assertNotIn("played", " ".join(job.keys()))

    def test_missing_piper_reports_failed_not_ready(self):
        from adbtv.tts import Synthesis
        responder = self.build(available=False,
                               synthesize=lambda text: Synthesis(False, text, error="piper not installed"))
        result = responder.respond("success", "已完成")
        job = self.wait(responder, result["job_id"])
        self.assertEqual(job["state"], "FAILED")
        self.assertIn("piper", str(job.get("reason") or ""))

    def test_worker_survives_synthesis_failure(self):
        calls = {"n": 0}
        from adbtv.tts import Synthesis
        def flaky(text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return Synthesis(True, text, "/tmp/x.wav", 900, False, 1.0)
        responder = self.build(synthesize=flaky)
        first = responder.respond("success", "一")
        self.wait(responder, first["job_id"])
        second = responder.respond("success", "二")
        job = self.wait(responder, second["job_id"])
        self.assertEqual(job["state"], "READY")
        self.assertTrue(responder._worker.is_alive())

    def test_synthesised_is_not_reported_as_played(self):
        responder = self.build()
        result = responder.respond("success", "已完成")
        job = self.wait(responder, result["job_id"])
        self.assertEqual(job["state"], "READY")
        self.assertEqual(responder.stats()["played"], 0)
        self.assertFalse(responder.status()["tv_audio"])


class SharedSessionSourceTests(unittest.TestCase):
    """Different inputs keep one session; expired state and stale confirms fail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SharedSessionStore(self.tmp.name)

    def test_two_sources_share_one_session(self):
        from tests.test_voice_v403 import build
        voice, _ = build(self.store)
        microphone, _ = build(self.store)
        first = voice.submit_text("找部悬疑剧", source="xiaomi_logcat")
        second = microphone.submit_text("第二部", source="web_microphone")
        self.assertEqual(first["session_id"], second["session_id"])

    def test_idle_expiry_drops_shared_state_after_180s(self):
        clock = [5000.0]
        manager = ConversationManager(ttl_seconds=180, clock=lambda: clock[0])
        manager.set_results("tv", [{"id": "a", "title": "三体"}], source="library")
        manager.select("tv", index=1)
        with self.store.transaction("tv") as state:
            state.clear(); state.update(manager.export("tv"))
        clock[0] += 181
        restored = ConversationManager(ttl_seconds=180, clock=lambda: clock[0])
        loaded = self.store.load("tv")
        self.assertFalse(restored.hydrate("tv", loaded))
        self.assertEqual(restored.snapshot("tv")["recent_results"], [])

    def test_stale_confirmation_cannot_execute_twice_after_restore(self):
        from tests.test_voice_v403 import build
        voice, _ = build(self.store)
        voice.submit_text("播放三体", source="xiaomi_logcat")
        console, agent = build(self.store)
        self.assertEqual(console.submit_text("确认", source="web_microphone")["status"], "SUCCESS")
        again, agent2 = build(self.store)
        self.assertEqual(again.submit_text("确认", source="xiaomi_logcat")["status"],
                         "NEEDS_CLARIFICATION")
        agent2.run.assert_not_called()


class TitledLookupTests(unittest.TestCase):
    """“找<片名>” must be a real library lookup, not a model guess."""

    def dialogue(self):
        from adbtv.voice_intelligence import IntelligentVoiceRouter
        from adbtv.voice_runtime import VoiceDialogue
        from tests.test_voice_dialogue_v40 import Catalog
        cfg = copy.deepcopy(DEFAULTS)
        cfg["voice"]["intelligence"].update(enabled=True, native_settle_seconds=0)
        return VoiceDialogue(cfg, agent=Mock(), catalog=Catalog(),
                             router=IntelligentVoiceRouter(cfg, Mock(enabled=False)))

    def test_titled_lookup_becomes_a_query(self):
        d = self.dialogue()
        for text, query in (("找穿成野神", "穿成野神"), ("搜索一下三体", "三体"),
                            ("搜三体", "三体"), ("帮我找暗河", "暗河")):
            with self.subTest(text=text):
                decision = d._context_decision(text, "tv")
                self.assertEqual(decision.intent, "search")
                self.assertEqual(decision.parameters.get("query"), query)

    def test_measure_words_do_not_leak_into_query(self):
        decision = self.dialogue()._context_decision("找个电影", "tv")
        self.assertEqual(decision.parameters.get("kind"), "movie")
        self.assertNotIn("query", decision.parameters)

    def test_genre_search_still_wins_when_genre_present(self):
        decision = self.dialogue()._context_decision("找部悬疑剧", "tv")
        self.assertEqual(decision.parameters.get("genre"), "悬疑")


class RealSpeechVariantsTests(unittest.TestCase):
    """b8 real-mic capture: normal speech habits must still resolve."""

    def dialogue(self):
        from adbtv.voice_intelligence import IntelligentVoiceRouter
        from adbtv.voice_runtime import VoiceDialogue
        from tests.test_voice_dialogue_v40 import Catalog
        cfg = copy.deepcopy(DEFAULTS)
        cfg["voice"]["intelligence"].update(enabled=True, native_settle_seconds=0)
        return VoiceDialogue(cfg, agent=Mock(), catalog=Catalog(),
                             router=IntelligentVoiceRouter(cfg, Mock(enabled=False)))

    def test_wake_word_and_repeated_verb_are_stripped(self):
        from adbtv.voice import VoiceRouter
        self.assertEqual(VoiceRouter.normalize("小爱同学播放第一个"), "播放第一个")
        self.assertEqual(VoiceRouter.normalize("播播放我的世界"), "播放我的世界")
        self.assertEqual(VoiceRouter.normalize("小爱同学"), "")

    def test_spoken_ordinals_resolve_with_common_prefixes(self):
        d = self.dialogue()
        for text in ("先放第一个", "我要看第一个", "小爱同学播放第一个", "要看第2个"):
            with self.subTest(text=text):
                decision = d._context_decision(text, "tv")
                self.assertEqual(decision.intent, "select")
                self.assertEqual(decision.parameters["index"], 1 if "第2" not in text else 2)

    def test_spoken_confirmation_variants(self):
        d = self.dialogue()
        for text in ("确认播放", "可以", "行", "没问题", "确定播放"):
            with self.subTest(text=text):
                self.assertEqual(d._context_decision(text, "tv").intent, "confirm")

    def test_open_app_then_action_without_comma(self):
        decision = self.dialogue()._context_decision("打开飞牛播放短剧", "tv")
        self.assertEqual(decision.intent, "sequence")
        steps = decision.parameters["steps"]
        self.assertEqual(steps[0]["parameters"]["app"], "飞牛")
        self.assertEqual(steps[1]["intent"], "search")

    def test_media_type_request_becomes_a_real_search(self):
        decision = self.dialogue()._context_decision("播放一部短剧", "tv")
        self.assertEqual(decision.intent, "search")
        self.assertEqual(decision.parameters.get("kind"), "series")

    def test_real_asr_verb_drop_and_repeat_are_tolerated(self):
        from adbtv.voice import VoiceRouter
        d = self.dialogue()
        # Round-3 real capture produced "播一部短剧" and "确定确定播放".
        decision = d._context_decision("播一部短剧", "tv")
        self.assertEqual(decision.intent, "search")
        self.assertEqual(decision.parameters.get("kind"), "series")
        self.assertEqual(VoiceRouter.normalize("确定确定播放"), "确定播放")
        self.assertEqual(d._context_decision("确定确定播放", "tv").intent, "confirm")

    def test_unknown_title_still_needs_confirmation_not_invention(self):
        d = self.dialogue()
        self.assertIsNone(d._context_decision("播放我的世界", "tv"))
