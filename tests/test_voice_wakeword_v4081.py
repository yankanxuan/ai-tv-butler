"""b8.1: wake-word longest-match plus prefix authorization must not regress."""
import copy
import unittest
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.voice import VoiceRouter
from adbtv.voice_intelligence import IntelligentVoiceRouter


def listener_cfg(prefix_only=(), **extra):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["voice"]["intelligence"].update(enabled=True, native_settle_seconds=0)
    cfg["voice"]["listener"].update({"prefix_only": list(prefix_only), **extra})
    return cfg


class WakeWordLongestMatchTests(unittest.TestCase):
    def test_all_wake_words_are_removed(self):
        cases = {
            "小爱小爱播放三体": "播放三体",
            "小爱同学播放三体": "播放三体",
            "你好小爱播放三体": "播放三体",
            "小爱播放三体": "播放三体",
            "小爱小爱小爱播放三体": "播放三体",   # repeated wake word
            "小爱同学小爱同学打开飞牛": "打开飞牛",
            "小爱小爱": "",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(VoiceRouter.normalize(text), expected)

    def test_wake_word_inside_a_title_is_not_stripped(self):
        # Only a leading wake word is removed; titles containing it stay intact.
        self.assertEqual(VoiceRouter.normalize("播放小爱小爱"), "播放小爱小爱")

    def test_repeated_verbs_still_collapse_after_wake_strip(self):
        self.assertEqual(VoiceRouter.normalize("小爱小爱播播放三体"), "播放三体")


class PrefixAuthorizationTests(unittest.TestCase):
    def test_legitimate_prefix_is_not_rejected_by_normalization(self):
        router = VoiceRouter(listener_cfg(prefix_only=["小爱同学"]))
        for text in ("小爱同学暂停", "小爱同学，暂停", "小爱同学小爱同学暂停"):
            with self.subTest(text=text):
                handle, reason = router.route(text)
                # Authorized means "past the prefix gate", not "bypassed everything".
                self.assertTrue(handle, reason)
                self.assertNotEqual(reason, "not-in-prefix-allowlist")

    def test_missing_prefix_still_rejected(self):
        router = VoiceRouter(listener_cfg(prefix_only=["小爱同学"]))
        for text in ("暂停", "你好小爱暂停", "小爱暂停"):
            with self.subTest(text=text):
                handle, reason = router.route(text)
                self.assertFalse(handle)
                self.assertEqual(reason, "not-in-prefix-allowlist")

    def test_prefix_cannot_be_smuggled_mid_sentence(self):
        router = VoiceRouter(listener_cfg(prefix_only=["小爱同学"]))
        self.assertFalse(router.route("打开小爱同学暂停")[0])

    def test_intelligent_router_prefix_policy_matches(self):
        router = IntelligentVoiceRouter(listener_cfg(prefix_only=["小爱同学"]), Mock(enabled=False))
        clean, decision = router.policy_text("小爱同学播放三体")
        self.assertIsNone(decision)
        self.assertEqual(clean, "播放三体")
        _, denied = router.policy_text("播放三体")
        self.assertEqual(denied.route, "ignore")
        self.assertEqual(denied.reason, "prefix_policy")

    def test_prefix_does_not_bypass_deny_list(self):
        cfg = listener_cfg(prefix_only=["小爱同学"], deny=["天气"])
        router = VoiceRouter(cfg)
        self.assertTrue(router.route("小爱同学暂停")[0])
        handle, reason = router.route("小爱同学今天天气怎么样")
        self.assertFalse(handle, reason)

    def test_empty_prefix_configuration_keeps_old_behaviour(self):
        router = VoiceRouter(listener_cfg(prefix_only=[]))
        self.assertTrue(router.route("暂停")[0])