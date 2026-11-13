"""V4 phase 2: ephemeral factual conversation state, without a controller."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import is_dataclass

from adbtv.conversation import ConversationManager


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.manager = ConversationManager(clock=self.clock)
        self.results = [
            {"id": "a", "title": "疑案一", "genres": ["悬疑"], "region": "中国大陆"},
            {"id": "b", "title": "疑案二", "genres": ["悬疑"], "region": "中国大陆", "season": 2, "episode": 3},
            {"id": "c", "title": "未知地区", "genres": ["悬疑"]},
            {"id": "d", "title": "海外疑案", "genres": ["悬疑"], "region": "美国"},
        ]

    def test_sessions_are_isolated_detached_and_clearable(self):
        first = self.manager.get("living")
        self.assertTrue(is_dataclass(first))
        self.assertNotEqual(first.session_id, self.manager.get("bedroom").session_id)
        self.manager.set_results("living", self.results)
        detached = self.manager.get("living")
        detached.recent_results[0]["title"] = "tampered"
        self.assertEqual(self.manager.get("living").recent_results[0]["title"], "疑案一")
        self.assertEqual(self.manager.get("bedroom").recent_results, [])
        self.manager.clear("living")
        self.assertNotEqual(first.session_id, self.manager.get("living").session_id)

    def test_expiry_clears_every_field_including_selection_and_question(self):
        self.manager.set_results("tv", self.results)
        self.manager.select("tv", index=2)
        self.manager.ask("tv", "selection", "请选择", ttl_seconds=180)
        self.manager.update_observation("tv", "com.example.tv", {"title": "疑案二", "state": "playing"})
        self.manager.update_result("tv", {"status": "SUCCESS"})
        self.manager.remember_turn("tv", "voice", "select", "success")
        old = self.manager.get("tv").session_id
        self.clock.now += 180
        session = self.manager.get("tv")
        self.assertNotEqual(old, session.session_id)
        for field in ("selected_content", "waiting_question", "current_app", "current_playback", "last_result"):
            self.assertIsNone(getattr(session, field))
        self.assertEqual(session.recent_results, [])
        self.assertEqual(session.recent_turns, [])

    def test_pronouns_missing_and_invalid_explicit_selections_never_guess(self):
        self.assertIsNone(self.manager.select("tv"))
        self.manager.set_results("tv", self.results)
        for index in (None, 0, -1, 5, True, "2"):
            self.assertIsNone(self.manager.select("tv", index=index))
        self.assertIsNone(self.manager.select("tv", candidate_id="missing"))
        self.assertIsNone(self.manager.select("tv", index=1, candidate_id="b"))
        self.assertIsNone(self.manager.get("tv").selected_content)

    def test_multiturn_suspense_domestic_second_and_episode_metadata(self):
        self.manager.set_results("tv", self.results)
        self.assertEqual(len(self.manager.refine("tv", genre="悬疑")), 4)
        domestic = self.manager.refine("tv", region="国产")
        self.assertEqual([r["id"] for r in domestic], ["a", "b"])
        self.manager.ask("tv", "selection", "请选择", {"count": 2})
        selected = self.manager.resolve("tv", index=2)
        self.assertEqual((selected["id"], selected["season"], selected["episode"]), ("b", 2, 3))
        self.assertIsNone(self.manager.get("tv").waiting_question)
        self.manager.update_observation("tv", "com.example.tv", selected | {"state": "playing"})
        self.assertEqual(self.manager.get("tv").current_playback["episode"], 3)
        self.manager.reject_selected("tv")
        session = self.manager.get("tv")
        self.assertIsNone(session.selected_content)
        self.assertEqual([r["id"] for r in session.recent_results], ["a"])
        self.assertEqual(session.current_playback["state"], "playing")

    def test_missing_region_and_genre_are_not_guessed_from_title(self):
        self.manager.set_results("tv", [{"id": "x", "title": "国产悬疑剧"}])
        self.assertEqual(self.manager.refine("tv", region="国产"), [])
        self.manager.set_results("tv", [{"id": "x", "title": "国产悬疑剧"}])
        self.assertEqual(self.manager.refine("tv", genre="悬疑"), [])

    def test_unknown_regions_do_not_become_confirmed_matches(self):
        self.manager.set_results("tv", [{"id": "x", "title": "Title", "region": "unknown"}])
        self.assertIsNone(self.manager.get("tv").recent_results[0]["region"])
        self.assertEqual(self.manager.refine("tv", region="unknown"), [])

    def test_replacing_results_discards_stale_selection_and_question(self):
        self.manager.set_results("tv", self.results)
        selected = self.manager.select("tv", candidate_id="b")
        selected["episode"] = 999
        self.assertEqual(self.manager.get("tv").selected_content["episode"], 3)
        self.manager.ask("tv", "selection", "请选择")
        self.manager.set_results("tv", [{"id": "new", "title": "New"}])
        self.assertIsNone(self.manager.get("tv").selected_content)
        self.assertIsNone(self.manager.resolve("tv", index=1))

    def test_question_expires_independently_and_needs_explicit_resolution(self):
        self.manager.set_results("tv", self.results)
        self.manager.ask("tv", "selection", "请选择", ttl_seconds=10)
        self.assertIsNone(self.manager.resolve("tv"))
        self.assertIsNotNone(self.manager.get("tv").waiting_question)
        self.clock.now += 10
        self.assertIsNone(self.manager.resolve("tv", index=1))
        session = self.manager.get("tv")
        self.assertIsNone(session.waiting_question)
        self.assertEqual(len(session.recent_results), 4)
        self.assertIsNone(session.selected_content)

    def test_untrusted_provenance_bad_ids_titles_and_duplicates_fail_atomically(self):
        self.manager.set_results("tv", self.results, source="observed_ui")
        for source in ("model", "llm", None, "Library"):
            with self.assertRaises(ValueError):
                self.manager.set_results("tv", self.results, source=source)
        for bad in ([{"id": "x", "title": "$(reboot)"}],
                    [{"id": "x", "title": "adb shell reboot"}],
                    [{"id": "x", "title": "token abcdefghijk"}],
                    [{"id": "x;reboot", "title": "Title"}],
                    [{"id": "x", "title": ""}],
                    [{"id": "x", "title": "a"}, {"id": "x", "title": "b"}]):
            with self.assertRaises(ValueError):
                self.manager.set_results("tv", bad)
        self.assertEqual(len(self.manager.get("tv").recent_results), 4)

    def test_bounds_and_privacy_across_every_context_field(self):
        manager = ConversationManager(max_turns=2, clock=self.clock)
        manager.set_results("tv", [{"id": str(i), "title": "Title", "path": "/secret/movie.mp4", "token": "abc", "details": {"raw_text": "private"}} for i in range(50)])
        self.assertEqual(len(manager.get("tv").recent_results), 32)
        for _ in range(5):
            manager.remember_turn("tv", "voice", "search", {"status": "SUCCESS", "utterance": "private original utterance"})
        manager.remember_turn("tv", "play /secret/file", "token=secret", "private original utterance")
        manager.ask("tv", "selection", "raw prompt /secret/file token=secret", {"count": 32, "utterance": "private original utterance"})
        manager.update_observation("tv", "com.example.tv", {"title": "Movie", "season": 1, "episode": 2, "state": "playing", "path": "/secret/movie.mp4", "token": "abc"})
        manager.update_result("tv", {"status": "ERROR", "reason_code": "not_found", "command": "adb shell ...", "error": "private original utterance /secret/file"})
        context = manager.snapshot("tv")
        self.assertEqual(len(context["recent_turns"]), 2)
        self.assertEqual(context["last_result"], {"status": "ERROR", "reason_code": "not_found"})
        encoded = json.dumps(context)
        for forbidden in ("/secret", "token", "private original utterance", "adb shell", "raw prompt", "raw_text"):
            self.assertNotIn(forbidden, encoded)
        context["recent_results"].clear()
        self.assertEqual(len(manager.get("tv").recent_results), 32)

    def test_threadsafe_updates_and_constructor_validation(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.manager.remember_turn("tv", "voice", "search", "success"), range(100)))
        self.assertEqual(len(self.manager.get("tv").recent_turns), 8)
        for kwargs in ({"ttl_seconds": 0}, {"ttl_seconds": float("nan")}, {"max_turns": 0}, {"max_turns": True}):
            with self.assertRaises(ValueError):
                ConversationManager(**kwargs)


if __name__ == "__main__":
    unittest.main()
