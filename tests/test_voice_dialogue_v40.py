"""End-to-end dialogue logic with factual catalog and a simulated executor."""
import copy
import unittest
from unittest.mock import Mock
from adbtv.config import DEFAULTS
from adbtv.voice_runtime import VoiceDialogue
from adbtv.voice_intelligence import IntelligentVoiceRouter


class Catalog:
    def __init__(self):
        self.rows=[
            {'id':'a','title':'迷城','genres':['悬疑'],'region':'中国大陆','kind':'series'},
            {'id':'b','title':'暗河','genres':['悬疑'],'region':'中国大陆','kind':'series'},
            {'id':'c','title':'外乡谜案','genres':['悬疑'],'region':'英国','kind':'series'},
            {'id':'d','title':'星际之门','genres':['科幻'],'region':'美国','kind':'movie'},
            {'id':'e','title':'三体','genres':['科幻'],'region':'中国大陆','kind':'series'},
        ]
    def search(self,query='',genre=None,region=None,kind=None):
        return [dict(r) for r in self.rows if (not query or query in r['title'])
            and (not genre or genre in r['genres']) and (not kind or kind=='any' or kind==r['kind'])]
    def resolve(self,title,season=None,episode=None):
        row=next((r for r in self.rows if r['title']==title),None)
        return dict(row,season=season or 1,episode=episode or 1) if row else None


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(DEFAULTS)
        self.cfg['voice']['intelligence']={'enabled':True,'native_settle_seconds':0}
        self.model=Mock();self.model.enabled=False
        router=IntelligentVoiceRouter(self.cfg,self.model)
        self.agent=Mock()
        self.agent.run.return_value={'status':'SUCCESS','ok':True,'summary':'完成'}
        self.dialogue=VoiceDialogue(self.cfg,agent=self.agent,catalog=Catalog(),router=router)
    def say(self,text,source='web_microphone',tv='tv1'):
        return self.dialogue.handle(text,tv_id=tv,source=source)

    def test_suspense_domestic_second_first_episode(self):
        first=self.say('找部悬疑剧')
        self.assertEqual(first['status'],'NEEDS_CLARIFICATION')
        self.assertEqual(len(first['candidates']),3)
        second=self.say('国产的')
        self.assertEqual(len(second['candidates']),2)
        self.say('第二部')
        self.assertEqual(self.dialogue.sessions.get('tv1').selected_content['title'],'暗河')
        asked=self.say('从第一集开始')
        self.assertEqual(asked['status'],'NEEDS_CONFIRMATION')
        result=self.say('确认')
        self.assertEqual(result['status'],'SUCCESS')
        self.assertIn('《暗河》',self.agent.run.call_args[0][0])
        self.assertIn('第一集',self.agent.run.call_args[0][0])

    def test_open_play_pause_resume(self):
        # Playback is confirmed by policy; the other three are idempotent controls.
        self.assertEqual(self.say('打开飞牛')['status'],'SUCCESS')
        self.assertEqual(self.say('播放三体')['status'],'NEEDS_CONFIRMATION')
        self.assertEqual(self.say('确认')['status'],'SUCCESS')
        for text in ['暂停','继续']:
            self.assertEqual(self.say(text)['status'],'SUCCESS')
        self.assertEqual(self.agent.run.call_count,4)
        state=self.dialogue.sessions.get('tv1')
        self.assertEqual(state.selected_content['title'],'三体')
        self.assertEqual(state.current_app,'com.trim.tv')

    def test_reject_never_autoplays_unknown_reference(self):
        self.say('找个科幻电影')
        reply=self.say('不要这个')
        self.assertEqual(reply['status'],'NEEDS_CLARIFICATION')
        self.assertEqual(self.say('换一个')['status'],'NEEDS_CLARIFICATION')
        self.agent.run.assert_not_called()

    def test_no_candidates_is_not_a_recommendation(self):
        self.assertEqual(self.say('播放我编造的作品')['status'],'NEEDS_CLARIFICATION')
        self.agent.run.assert_not_called()

    def test_ambiguous_selection_is_not_guessed(self):
        for text in ['第二部','从第一集开始','换一个']:
            self.assertEqual(self.say(text)['status'],'NEEDS_CLARIFICATION')
        self.agent.run.assert_not_called()

    def test_xiaomi_play_requires_explicit_confirmation(self):
        result=self.say('播放三体',source='xiaomi_logcat')
        self.assertEqual(result['status'],'NEEDS_CONFIRMATION')
        self.agent.run.assert_not_called()
        approved=self.say('确认',source='xiaomi_logcat')
        self.assertEqual(approved['status'],'SUCCESS')
        self.agent.run.assert_called_once()

    def test_confirmation_not_shared_between_tvs(self):
        self.say('播放三体',source='xiaomi_logcat')
        self.assertEqual(self.say('确认',source='xiaomi_logcat',tv='tv2')['status'],'NEEDS_CLARIFICATION')
        self.agent.run.assert_not_called()

    def test_native_domain_is_not_dispatched(self):
        self.assertEqual(self.say('关闭客厅的灯')['route'],'native')
        self.agent.run.assert_not_called()

    def test_model_candidates_are_not_observed_results(self):
        self.model.enabled=True
        import json
        self.model.chat.return_value=json.dumps(dict(route='agent',intent='select',target='',
            parameters={'candidate_id':'invented'},confidence=1,reply='',risk='none'))
        self.assertEqual(self.say('给我刚才那部')['status'],'NEEDS_CLARIFICATION')
        self.agent.run.assert_not_called()

    def test_model_cannot_supply_user_confirmation(self):
        import json
        self.say('播放三体',source='xiaomi_logcat')
        self.model.enabled=True
        self.model.chat.return_value=json.dumps(dict(route='agent',intent='confirm',target='',
            parameters={},confidence=1,reply='',risk='none'))
        result=self.say('随便吧',source='xiaomi_logcat')
        self.assertEqual(result['status'],'NEEDS_CONFIRMATION')
        self.agent.run.assert_not_called()

    def test_cancel_task_never_becomes_media_pause(self):
        import json
        self.model.enabled=True
        self.model.chat.return_value=json.dumps(dict(route='agent',intent='pause',target='',
            parameters={},confidence=1,reply='',risk='none'))
        result=self.say('取消当前任务')
        self.assertEqual(result['status'],'NEEDS_CLARIFICATION')
        self.agent.run.assert_not_called()
