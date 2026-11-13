"""V4 phase 1: strict model output, local fast paths, bounded fallback."""
import copy
import json
import time
import unittest
from unittest.mock import Mock
from adbtv.config import DEFAULTS
from adbtv.voice_intelligence import IntelligentVoiceRouter, validate_decision, DecisionError


def answer(**kw):
    d=dict(route='clarify',intent='unknown',target='',parameters={},confidence=0.8,
           reply='想看哪一部？',risk='none')
    d.update(kw)
    return d


class StrictSchemaTests(unittest.TestCase):
    def test_valid_result(self):
        self.assertEqual(validate_decision(answer()).route,'clarify')

    def test_reject_bad_outputs(self):
        invalid=[answer(confidence=True),answer(confidence=float('nan')),answer(confidence=1.1),
                 answer(route='shell'),answer(risk='whatever'),answer(intent='adb'),
                 answer(parameters={'shell':'input keyevent 26'}),answer(extra='ignored'),
                 answer(target={'name':'x'}),answer(reply='x'*1001)]
        for d in invalid:
            with self.subTest(d=d), self.assertRaises(DecisionError):validate_decision(d)

    def test_reject_dangerous_operation_parameters(self):
        for p in [{'level':-1},{'level':True},{'level':101},{'level':'5'}]:
            with self.subTest(p=p),self.assertRaises(DecisionError):
                validate_decision(answer(route='agent',intent='volume_set',parameters=p))

    def test_reject_fake_candidate_payload(self):
        with self.assertRaises(DecisionError):
            validate_decision(answer(intent='search',parameters={'results':[{'title':'假的'}]}))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(DEFAULTS)
        self.cfg['voice']['intelligence']={'enabled':True,'llm_timeout_seconds':0.1}
        self.model=Mock();self.model.enabled=True
        self.router=IntelligentVoiceRouter(self.cfg,llm=self.model)

    def test_simple_commands_never_call_llm(self):
        for text in ['暂停','继续','音量调到8','打开飞牛','回到桌面','当前应用是什么']:
            with self.subTest(text=text):self.assertEqual(self.router.decide(text).route,'agent')
        self.model.chat.assert_not_called()

    def test_native_commands_never_call_llm(self):
        for text in ['关闭客厅的灯','打开空调','今天天气怎么样','现在几点了']:
            with self.subTest(text=text):self.assertEqual(self.router.decide(text).route,'native')
        self.model.chat.assert_not_called()

    def test_ambiguous_goes_to_model(self):
        self.model.chat.return_value=json.dumps(answer(),ensure_ascii=False)
        self.assertEqual(self.router.decide('找部悬疑剧').route,'clarify')
        self.model.chat.assert_called_once()

    def test_low_confidence_requests_clarification(self):
        self.model.chat.return_value=json.dumps(answer(route='agent',intent='play',target='三体',confidence=.2,risk='low'))
        d=self.router.decide('来点好看的')
        self.assertEqual(d.route,'clarify')

    def test_model_side_effect_requires_confirmation_even_if_confident(self):
        self.model.chat.return_value=json.dumps(answer(route='agent',intent='play',target='三体',confidence=1,risk='none'))
        d=self.router.decide('随便来点')
        self.assertEqual(d.route,'clarify')
        self.assertTrue(d.requires_confirmation)

    def test_model_invalid_json_is_safe(self):
        self.model.chat.return_value='{"route":"agent","shell":"rm -rf x"}'
        d=self.router.decide('随便来点')
        self.assertEqual(d.route,'clarify')
        self.assertIn('invalid',d.reason)

    def test_timeout_does_not_accumulate_model_calls(self):
        import threading
        release=threading.Event()
        def slow(*a,**k):release.wait(2);return json.dumps(answer())
        self.model.chat.side_effect=slow
        start=time.monotonic()
        try:
            first=self.router.decide('随便来点')
            second=self.router.decide('还有别的吗')
            self.assertEqual(first.route,'clarify');self.assertEqual(second.route,'clarify')
            self.assertLess(time.monotonic()-start,.5)
            self.assertEqual(self.model.chat.call_count,1)
        finally:release.set()

    def test_route_result_does_not_claim_native_suppression(self):
        d=self.router.decide('暂停')
        self.assertTrue(d.conflict_risk)
        self.assertFalse(d.native_suppressed)


class PolicyCompatibilityTests(unittest.TestCase):
    def test_prefix_restriction_is_preserved_and_removed_before_parsing(self):
        cfg=copy.deepcopy(DEFAULTS)
        cfg['voice']['intelligence']['enabled']=True
        cfg['voice']['listener']['prefix_only']=['电视管家']
        model=Mock(enabled=False)
        router=IntelligentVoiceRouter(cfg,model)
        self.assertEqual(router.decide('暂停').route,'ignore')
        self.assertEqual(router.decide('电视管家暂停').intent,'pause')
        model.chat.assert_not_called()

    def test_explicit_deny_still_wins(self):
        cfg=copy.deepcopy(DEFAULTS)
        cfg['voice']['listener']['deny']=['音量']
        model=Mock(enabled=False)
        self.assertEqual(IntelligentVoiceRouter(cfg,model).decide('音量调到8').route,'native')
        model.chat.assert_not_called()
