"""V4.4 phase 3: multi-turn dialogue context (references, preferences, focus).

Pure in-memory, deterministic clock, no network and no library on disk: every
candidate row here is a factual dictionary supplied by a trusted integration.
"""
import json
import unittest

from adbtv.conversation import ConversationManager

PREFERENCE_KEYS = {"kind", "genres", "exclude_genres", "max_episodes",
                   "min_episodes", "mood", "exclude_ids", "last_query"}

ROWS = [
    {"id": "s1", "title": "师兄太稳健 (2026)", "kind": "short_drama", "episodes": 120,
     "genres": ["脑洞", "爽剧"], "studio": "红果", "added_days_ago": 3},
    {"id": "s2", "title": "我不是大师 (2026)", "kind": "short_drama", "episodes": 90,
     "genres": ["逆袭"], "studio": "红果", "added_days_ago": 9},
    {"id": "s3", "title": "疑案追凶", "kind": "series", "episodes": 24,
     "genres": ["悬疑"], "region": "中国大陆"},
]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class DialogueContextTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.manager = ConversationManager(ttl_seconds=180, clock=self.clock)
        self.manager.set_candidates("tv", ROWS)

    def ids(self):
        return [row["id"] for row in self.manager.get("tv").recent_results]

    # -- one-based ordinals ---------------------------------------------------
    def test_chinese_and_arabic_ordinals_resolve_by_position(self):
        self.assertEqual(self.manager.reference("tv", "第二个讲什么？")["id"], "s2")
        self.assertEqual(self.manager.reference("tv", "第2部")["id"], "s2")
        self.assertEqual(self.manager.reference("tv", "第三个")["id"], "s3")
        self.assertEqual(self.manager.reference("tv", "最后一个")["id"], "s3")
        self.assertEqual(self.manager.reference("tv", "第一部")["id"], "s1")
        self.assertEqual(self.manager.reference("tv", "第十二个"), None)   # beyond 3 rows

    def test_out_of_range_ordinal_never_clamps_to_an_edge(self):
        for text in ("第9个", "第九个", "第四个", "第99部"):
            with self.subTest(text=text):
                self.assertIsNone(self.manager.reference("tv", text))
        # A failed ordinal does not invent a focus either.
        self.assertIsNone(self.manager.focus("tv"))

    def test_episode_or_season_ordinal_is_not_a_candidate_position(self):
        self.manager.reference("tv", "第二个")
        for text in ("播放第二集", "第二季"):
            with self.subTest(text=text):
                self.assertIsNone(self.manager.reference("tv", text))

    def test_ordinal_after_preference_rebuild_uses_current_rows_only(self):
        self.manager.reference("tv", "第三个")
        self.manager.set_candidates("tv", ROWS[:2])
        self.assertIsNone(self.manager.reference("tv", "第三个"))
        self.assertEqual(self.manager.reference("tv", "第二个")["id"], "s2")

    # -- demonstratives -------------------------------------------------------
    def test_demonstratives_resolve_to_the_last_referenced_candidate(self):
        self.assertEqual(self.manager.reference("tv", "第二个讲什么？")["id"], "s2")
        for text in ("刚才那个", "上一个", "这个", "它", "那部", "这片", "这部"):
            with self.subTest(text=text):
                self.assertEqual(self.manager.reference("tv", text)["id"], "s2")
                self.assertEqual(self.manager.focus("tv")["id"], "s2")

    def test_demonstrative_without_a_reference_never_picks_the_first_candidate(self):
        for text in ("刚才那个", "上一个", "这个", "它", "那部", "这片"):
            with self.subTest(text=text):
                self.assertIsNone(self.manager.reference("tv", text))
        self.assertIsNone(self.manager.focus("tv"))

    # -- spoken title fragments ----------------------------------------------
    def test_full_and_partial_title_fragments_resolve(self):
        self.assertEqual(self.manager.reference("tv", "师兄太稳健")["id"], "s1")
        self.assertEqual(self.manager.reference("tv", "我想看稳健那个")["id"], "s1")
        self.assertEqual(self.manager.reference("tv", "疑案追凶怎么样")["id"], "s3")
        self.assertEqual(self.manager.focus("tv")["id"], "s3")

    def test_ambiguous_title_fragment_matches_nothing(self):
        """Contract rule: a fragment shared by two works is ambiguous -> None."""
        self.manager.set_candidates("tv", [{"id": "a", "title": "疑案一"},
                                           {"id": "b", "title": "疑案二"}])
        self.assertIsNone(self.manager.reference("tv", "疑案"))
        # The unambiguous full title still resolves.
        self.assertEqual(self.manager.reference("tv", "疑案二")["id"], "b")

    def test_unknown_utterance_is_not_guessed(self):
        self.manager.reference("tv", "第二个")
        for text in ("有没有类似的？", "不要太长的。", "随便说点什么", "今天天气不错"):
            with self.subTest(text=text):
                self.assertIsNone(self.manager.reference("tv", text))
        # A failed resolution keeps the previous focus instead of clearing it.
        self.assertEqual(self.manager.focus("tv")["id"], "s2")

    # -- preferences ----------------------------------------------------------
    def test_preferences_expose_every_stable_key(self):
        preferences = self.manager.preferences("tv")
        self.assertEqual(set(preferences), PREFERENCE_KEYS)
        self.assertEqual(preferences["genres"], [])
        self.assertEqual(preferences["exclude_ids"], [])
        self.assertIsNone(preferences["max_episodes"])
        self.assertIsNone(preferences["kind"])

    def test_long_work_cue_caps_max_episodes(self):
        merged = self.manager.update_preferences("tv", {"text": "不要太长的。"})
        self.assertEqual(merged["max_episodes"], 40)
        # A stricter later cap wins; a looser one never relaxes the wish.
        self.assertEqual(self.manager.update_preferences("tv", {"max_episodes": 30})["max_episodes"], 30)
        self.assertEqual(self.manager.update_preferences("tv", {"max_episodes": 200})["max_episodes"], 30)

    def test_negated_genre_goes_to_exclude_genres(self):
        merged = self.manager.update_preferences("tv", {"text": "不要恐怖的。"})
        self.assertEqual(merged["exclude_genres"], ["恐怖"])
        self.assertEqual(merged["genres"], [])
        merged = self.manager.update_preferences("tv", {"text": "别太吓人，不要恐怖片"})
        self.assertEqual(merged["exclude_genres"], ["恐怖"])

    def test_positive_genre_is_merged_not_replaced(self):
        self.manager.update_preferences("tv", {"genres": ["悬疑"]})
        merged = self.manager.update_preferences("tv", {"genres": ["悬疑", "犯罪"]})
        self.assertEqual(merged["genres"], ["悬疑", "犯罪"])

    def test_swap_cue_excludes_the_current_focus(self):
        self.manager.reference("tv", "第二个")
        merged = self.manager.update_preferences("tv", {"text": "换一个。"})
        self.assertEqual(merged["exclude_ids"], ["s2"])
        merged = self.manager.update_preferences("tv", {"text": "换一个。"})
        self.assertEqual(merged["exclude_ids"], ["s2"])       # union, no duplicates

    def test_similar_cue_inherits_focus_kind_and_genres(self):
        self.manager.reference("tv", "第三个")
        merged = self.manager.update_preferences("tv", {"text": "有没有类似的？"})
        self.assertEqual(merged["kind"], "series")
        self.assertEqual(merged["genres"], ["悬疑"])

    def test_structured_merge_semantics_are_monotonic(self):
        self.manager.update_preferences("tv", {"kind": "series", "min_episodes": 10,
                                               "max_episodes": 60, "mood": "轻松",
                                               "exclude_ids": ["s1"]})
        merged = self.manager.update_preferences("tv", {"min_episodes": 20,
                                                        "exclude_ids": ["s2"]})
        self.assertEqual(merged["kind"], "series")
        self.assertEqual(merged["min_episodes"], 20)
        self.assertEqual(merged["max_episodes"], 60)
        self.assertEqual(merged["mood"], "轻松")
        self.assertEqual(merged["exclude_ids"], ["s1", "s2"])
        self.assertEqual(merged["min_episodes"] < merged["max_episodes"], True)

    def test_preference_payload_is_bounded_and_sanitized(self):
        merged = self.manager.update_preferences("tv", {
            "genres": ["/secret/movie.mp4", "悬疑", "悬疑", "token abcdef"],
            "exclude_ids": ["s1;reboot", "s1"],
            "last_query": {"query": "悬疑", "genre": "悬疑", "kind": "series",
                           "path": "/secret/movie.mp4"},
        })
        self.assertEqual(merged["genres"], ["悬疑"])
        self.assertEqual(merged["exclude_ids"], ["s1"])
        self.assertEqual(merged["last_query"],
                         {"query": "悬疑", "genre": "悬疑", "kind": "series"})
        encoded = json.dumps(self.manager.snapshot("tv"), ensure_ascii=False)
        for forbidden in ("/secret", "token abcdef", "reboot"):
            self.assertNotIn(forbidden, encoded)

    def test_unknown_preference_keys_are_dropped_not_stored(self):
        merged = self.manager.update_preferences("tv", {"region": "美国", "path": "/tmp/x"})
        self.assertEqual(set(merged), PREFERENCE_KEYS)
        self.assertNotIn("region", merged)

    # -- topic switch and correction -----------------------------------------
    def test_topic_switch_updates_focus_and_old_ordinals_do_not_leak(self):
        self.assertEqual(self.manager.reference("tv", "第二个")["id"], "s2")
        self.manager.set_candidates("tv", [{"id": "d", "title": "暗河"},
                                           {"id": "e", "title": "长夜"}])
        self.assertEqual(self.manager.reference("tv", "暗河")["id"], "d")
        self.assertEqual(self.manager.focus("tv")["id"], "d")
        self.assertIsNone(self.manager.reference("tv", "第三个"))
        self.assertEqual(self.manager.reference("tv", "第二个")["id"], "e")

    def test_failed_reference_does_not_pretend_the_old_focus_matched(self):
        self.manager.reference("tv", "第二个")
        self.manager.set_candidates("tv", [{"id": "d", "title": "暗河"}])
        self.assertIsNone(self.manager.reference("tv", "完全不存在的片名"))
        self.assertEqual(self.manager.focus("tv")["id"], "s2")   # unchanged, not replaced

    def test_referenced_work_is_restorable_after_a_preference_rebuild(self):
        self.assertEqual(self.manager.reference("tv", "第二个")["id"], "s2")
        # A stricter preference rebuilds the rows and drops the referenced work.
        self.manager.update_preferences("tv", {"text": "不要太长的。"})
        self.manager.set_candidates("tv", [{"id": "s3", "title": "疑案追凶"}])
        restored = self.manager.reference("tv", "就看刚才那个。")
        self.assertEqual(restored["id"], "s2")
        self.assertIn("s2", self.ids())
        self.assertEqual(self.ids(), ["s3", "s2"])       # appended, never prepended
        self.assertEqual(self.manager.reference("tv", "第一个")["id"], "s3")
        self.assertEqual(self.manager.select("tv", candidate_id=restored["id"])["id"], "s2")

    def test_selection_and_rejection_move_the_focus(self):
        self.manager.select("tv", index=1)
        self.assertEqual(self.manager.focus("tv")["id"], "s1")
        self.manager.reject_selected("tv")
        self.assertIsNone(self.manager.focus("tv"))

    # -- played history -------------------------------------------------------
    def test_note_played_records_known_work_only(self):
        self.assertIsNone(self.manager.note_played("tv", "s2", season=1, episode=3))
        played = self.manager.get("tv").played_history
        self.assertEqual([(item["id"], item["season"], item["episode"]) for item in played],
                         [("s2", 1, 3)])
        self.manager.note_played("tv", "s2")
        self.assertEqual(len(self.manager.get("tv").played_history), 1)
        self.manager.note_played("tv", "never-seen")
        self.assertEqual([item["id"] for item in self.manager.get("tv").played_history], ["s2"])
        with self.assertRaises(ValueError):
            self.manager.note_played("tv", "s1;reboot")

    # -- export / hydrate -----------------------------------------------------
    def test_export_hydrate_roundtrip_including_new_fields(self):
        self.manager.reference("tv", "第二个")
        self.manager.update_preferences("tv", {"text": "不要太长的，不要恐怖的。"})
        exported = self.manager.export("tv")
        self.assertEqual(set(exported["preferences"]), PREFERENCE_KEYS)
        self.assertEqual(exported["focus_item"]["id"], "s2")

        restored = ConversationManager(ttl_seconds=180, clock=self.clock)
        self.assertTrue(restored.hydrate("tv", exported))
        self.assertEqual(restored.preferences("tv"), exported["preferences"])
        self.assertEqual(restored.focus("tv")["id"], "s2")
        self.assertEqual([row["id"] for row in restored.get("tv").recent_results],
                         ["s1", "s2", "s3"])
        self.assertEqual(restored.reference("tv", "刚才那个")["id"], "s2")
        self.assertEqual(restored.get("tv").recent_results[0]["kind"], "short_drama")

    def test_old_snapshot_without_new_keys_hydrates_with_defaults(self):
        legacy = {
            "session_id": "a" * 32,
            "current_app": "com.example.tv",
            "recent_results": [{"id": "x", "title": "旧片"}],
            "selected_content": {"id": "x", "title": "旧片"},
            "played_history": [],
            "recent_turns": [],
            "assistant": {"last_intent": "search"},
            "expires_at": self.clock.now + 60,
        }
        restored = ConversationManager(ttl_seconds=180, clock=self.clock)
        self.assertTrue(restored.hydrate("tv", legacy))
        self.assertEqual(restored.preferences("tv"),
                         {"kind": None, "genres": [], "exclude_genres": [],
                          "max_episodes": None, "min_episodes": None, "mood": None,
                          "exclude_ids": [], "last_query": {}})
        self.assertIsNone(restored.focus("tv"))
        self.assertIsNone(restored.reference("tv", "刚才那个"))
        self.assertEqual(restored.reference("tv", "第一个")["id"], "x")
        # The new fields simply appear on the next export.
        self.assertIn("preferences", restored.export("tv"))

    def test_invalid_new_fields_in_a_snapshot_do_not_raise(self):
        snapshot = {
            "recent_results": [{"id": "x", "title": "旧片"}],
            "preferences": "not-a-dict",
            "focus_item": {"id": "x;reboot", "title": "bad"},
            "expires_at": self.clock.now + 60,
        }
        restored = ConversationManager(ttl_seconds=180, clock=self.clock)
        self.assertTrue(restored.hydrate("tv", snapshot))
        self.assertEqual(set(restored.preferences("tv")), PREFERENCE_KEYS)
        self.assertIsNone(restored.focus("tv"))

    # -- TTL ------------------------------------------------------------------
    def test_expiry_clears_candidates_preferences_and_focus(self):
        self.manager.reference("tv", "第二个")
        self.manager.update_preferences("tv", {"text": "不要太长的，不要恐怖的。"})
        self.clock.now += 180
        self.assertEqual(self.manager.get("tv").recent_results, [])
        self.assertEqual(self.manager.preferences("tv"),
                         {"kind": None, "genres": [], "exclude_genres": [],
                          "max_episodes": None, "min_episodes": None, "mood": None,
                          "exclude_ids": [], "last_query": {}})
        self.assertIsNone(self.manager.focus("tv"))
        self.assertIsNone(self.manager.reference("tv", "刚才那个"))

    def test_pending_confirmation_semantics_are_unchanged(self):
        import secrets
        nonce = secrets.token_hex(16)
        self.manager.reference("tv", "第二个")
        self.manager.update_preferences("tv", {"text": "不要太长的。"})
        pending = self.manager.set_pending("tv", nonce=nonce, command="play 123",
                                           intent="play", candidate_id="s2")
        self.assertEqual(pending["candidate_id"], "s2")
        self.assertEqual(self.manager.take_pending("tv")["nonce"], nonce)
        self.assertIsNone(self.manager.take_pending("tv"))      # still one-shot

    # -- clarify --------------------------------------------------------------
    def test_empty_candidates_and_bad_input_return_none_without_raising(self):
        empty = ConversationManager(ttl_seconds=180, clock=self.clock)
        for text in ("第二个", "最后一个", "刚才那个", "它", "师兄太稳健", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(empty.reference("tv", text))
        for bad in (None, 42, [], {}):
            with self.subTest(bad=bad):
                self.assertIsNone(empty.reference("tv", bad))
        self.assertIsNone(empty.focus("tv"))
        self.assertEqual(empty.preferences("tv")["exclude_ids"], [])


if __name__ == "__main__":
    unittest.main()
