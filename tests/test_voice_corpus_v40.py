"""Curated Chinese routing regression corpus, using fixture/mock-LM outputs.

This evaluates deterministic router behavior, NOT real-model comprehension,
ASR accuracy, real media availability, successful execution, or audible TTS.
Semantic labels are hand-authored expected routes; model_result is supplied
verbatim by FixtureLLM. No network, ADB, controller, or privileged mocks run.
"""
from collections import Counter
import copy
import json
from pathlib import Path
import unittest

from adbtv.conversation import ConversationManager
from adbtv.voice_intelligence import (
    DECISION_SCHEMA, IntelligentVoiceRouter, validate_decision,
)


CORPUS_PATH = Path(__file__).parent / "fixtures" / "voice_zh_v40.json"
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


class FixtureLLM:
    """The only model used here: returns the authored fixture, never a network call."""

    enabled = True

    def __init__(self, result=None):
        self.result = result
        self.requests = []

    def chat(self, messages, *, system):
        self.requests.append(json.loads(messages[0]["content"]))
        if self.result is None:
            raise AssertionError("Rule-only case unexpectedly called the model")
        return json.dumps(self.result, ensure_ascii=False)


def fixture_config():
    # Explicit app aliases keep expectations independent of deployment settings.
    return {
        "voice": {"intelligence": {
            "enabled": True, "confidence_threshold": .8,
            "llm_timeout_seconds": 1,
        }},
        "apps": {
            "feiniu_tv": {"label": "飞牛TV", "package": "fixture.feiniu"},
            "bilibili": {"label": "B站", "package": "fixture.bilibili"},
            "iqiyi": {"label": "爱奇艺", "package": "fixture.iqiyi"},
            "tencent": {"label": "腾讯视频", "package": "fixture.tencent"},
        },
    }


def evaluate_fixture_case(case):
    """Return actual router decision and fixture call evidence; no accuracy claim.

    Context comes from the public conversation API. A pending question contains
    no invented candidate or search result. Dialogue resolution has separate tests.
    """
    manager = ConversationManager()
    if case.get("context_profile") == "pending_question":
        manager.ask("fixture-tv", "confirmation", "是否确认？")
    context = manager.snapshot("fixture-tv")
    llm = FixtureLLM(copy.deepcopy(case.get("model_result")))
    router = IntelligentVoiceRouter(fixture_config(), llm=llm)
    return router.decide(case["text"], context), llm, context


class ChineseVoiceCorpusTests(unittest.TestCase):
    def test_corpus_size_contract_and_coverage(self):
        self.assertGreaterEqual(len(CORPUS), 120)
        self.assertEqual(len({c["id"] for c in CORPUS}), len(CORPUS))
        self.assertEqual(len({c["text"] for c in CORPUS}), len(CORPUS))
        counts = Counter(c["category"] for c in CORPUS)
        self.assertTrue({
            "simple_control", "native_home", "native_information",
            "complex_combination", "fuzzy_search", "missing_reference",
            "asr_homophone_safe", "cancellation_phase4_negative",
            "native_conflict_mixed", "tts_unavailable_negative",
        }.issubset(counts))
        for case in CORPUS:
            with self.subTest(case=case["id"]):
                self.assertTrue({"id", "text", "category", "expected_route", "mode"}.issubset(case))
                self.assertRegex(case["text"], r"[\u4e00-\u9fff]")
                self.assertIn(case["expected_route"], ("agent", "native", "clarify", "ignore"))
                self.assertIn(case["mode"], ("rule", "semantic"))
                if case["mode"] == "semantic":
                    self.assertEqual(set(case["model_result"]), set(DECISION_SCHEMA["required"]))
                    validate_decision(case["model_result"])
                    # Search fixtures express conditions, never model-invented results.
                    self.assertNotIn("candidate_id", case["model_result"]["parameters"])
                    self.assertNotIn("results", case["model_result"]["parameters"])
                else:
                    self.assertNotIn("model_result", case)

    def test_curated_routes_with_fixture_mock_lm(self):
        for case in CORPUS:
            with self.subTest(case=case["id"], text=case["text"], category=case["category"]):
                actual, llm, context = evaluate_fixture_case(case)
                self.assertEqual(actual.route, case["expected_route"], actual.audit())
                if "expected_intent" in case:
                    self.assertEqual(actual.intent, case["expected_intent"], actual.audit())
                expected_calls = int(case["mode"] == "semantic")
                self.assertEqual(len(llm.requests), expected_calls, actual.audit())
                self.assertEqual(actual.source, "llm" if expected_calls else "rule")
                if expected_calls:
                    self.assertEqual(llm.requests[0]["utterance"], case["text"])
                    self.assertEqual(llm.requests[0]["context"], context)
                self.assertFalse(actual.native_suppressed)
                if "expected_confirmation" in case:
                    self.assertEqual(actual.requires_confirmation, case["expected_confirmation"])
                if "expected_conflict" in case:
                    self.assertEqual(actual.conflict_risk, case["expected_conflict"])
                if case["category"] in {
                    "cancellation_phase4_negative", "tts_unavailable_negative",
                    "asr_homophone_safe", "missing_reference",
                }:
                    self.assertIn(actual.route, ("clarify", "ignore"))
                    self.assertEqual(actual.intent, "unknown")


if __name__ == "__main__":
    unittest.main()
