"""Real listener ingress + worker boundary, catalog facts, and safety checks."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from adbtv.config import DEFAULTS
from adbtv.voice import VoiceListener, Utterance
from adbtv.voice_intelligence import IntelligentVoiceRouter
from adbtv.voice_runtime import VoiceDialogue, LibraryVoiceCatalog
from adbtv.conversation import ConversationManager
from adbtv.media_index import MediaIndex
from tests.test_voice_dialogue_v40 import Catalog


class IngressTests(unittest.TestCase):
    def cfg(self,tmp):
        c=copy.deepcopy(DEFAULTS)
        c['voice']['intelligence']['enabled']=True
        c['voice']['listener'].update(state_file=str(Path(tmp)/'voice_state.json'),queue_size=4)
        return c

    def line(self,text):
        return str(time.time())+' I tag: processAsrResult '+json.dumps({'payload':{'is_final':True,'results':[{'text':text}]}})

    def test_reader_enqueues_context_answers_without_calling_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            callback=Mock()
            listener=VoiceListener(self.cfg(tmp),callback)
            for text in ['找部悬疑剧','国产的','第二部']:
                self.assertTrue(listener.ingest_line(self.line(text)))
            callback.assert_not_called()
            self.assertEqual(listener._queue.qsize(),3)
            listener._drain()
            self.assertEqual(callback.call_count,3)
            self.assertEqual(listener._queue.unfinished_tasks,0)

    def test_slow_callback_cannot_block_asr_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            started=threading.Event();release=threading.Event()
            def slow(u):started.set();release.wait(2)
            listener=VoiceListener(self.cfg(tmp),slow)
            listener.start()
            try:
                listener.ingest_line(self.line('找部悬疑剧'))
                self.assertTrue(started.wait(1))
                begin=time.monotonic()
                for text in ['国产的','第二部','从第一集开始']:
                    self.assertTrue(listener.ingest_line(self.line(text)))
                self.assertLess(time.monotonic()-begin,.5)
                self.assertEqual(listener._queue.qsize(),3)
            finally:release.set();listener.stop()

    def test_intelligent_audit_hides_raw_text_and_model_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=self.cfg(tmp)
            model=Mock(enabled=False)
            dialogue=VoiceDialogue(cfg,agent=Mock(),catalog=Catalog(),router=IntelligentVoiceRouter(cfg,model))
            listener=VoiceListener(cfg,lambda u:dialogue.handle(u.text,tv_id='tv'))
            listener.ingest_line(self.line('找部悬疑剧'));listener._drain()
            raw=listener._events_path.read_text()
            self.assertNotIn('找部悬疑剧',raw)
            self.assertNotIn('迷城',raw)
            events=[json.loads(line) for line in raw.splitlines()]
            decision=events[-1]['decision']
            self.assertIn('route',decision)
            self.assertNotIn('target',decision)
            # TTS is disabled by default: the metric exists but no audio is produced.
            self.assertEqual(events[-1]['metrics']['tts_ms'], 0.0)
            self.assertNotIn('audio_url', raw)


class FactualCatalogTests(unittest.TestCase):
    def test_genre_and_region_must_come_from_nfo_not_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            show=root/'悬疑国产大全 (2026)'/'Season 01';show.mkdir(parents=True)
            (show/'悬疑国产大全 S01E001.mp4').write_bytes(b'video')
            index=MediaIndex([str(root)]).scan()
            catalog=LibraryVoiceCatalog(DEFAULTS,index)
            self.assertEqual(catalog.search(genre='悬疑',region='国产'),[])
            (show.parent/'tvshow.nfo').write_text('<tvshow><genre>悬疑</genre><country>中国大陆</country></tvshow>')
            catalog=LibraryVoiceCatalog(DEFAULTS,index)
            self.assertEqual(len(catalog.search(genre='悬疑',region='国产')),1)

    def test_missing_episode_not_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            show=Path(tmp)/'片名 (2026)'/'Season 01';show.mkdir(parents=True)
            (show/'片名 S01E001.mp4').write_bytes(b'video')
            catalog=LibraryVoiceCatalog(DEFAULTS,MediaIndex([tmp]).scan())
            self.assertIsNone(catalog.resolve('片名',1,99))


class ConversationSafetyTests(unittest.TestCase):
    def make(self,sessions=None,device=None):
        cfg=copy.deepcopy(DEFAULTS);cfg['voice']['intelligence'].update(enabled=True,native_settle_seconds=0)
        model=Mock(enabled=False);agent=Mock()
        agent.run.return_value={'status':'SUCCESS','ok':True}
        service=VoiceDialogue(cfg,agent=agent,catalog=Catalog(),router=IntelligentVoiceRouter(cfg,model),sessions=sessions,device=device)
        return service,agent

    def test_expired_confirmation_never_executes(self):
        now=[100.0]
        sessions=ConversationManager(ttl_seconds=10,clock=lambda:now[0])
        service,agent=self.make(sessions)
        service.handle('播放三体',tv_id='a')
        now[0]+=11
        self.assertEqual(service.handle('确认',tv_id='a')['status'],'NEEDS_CLARIFICATION')
        agent.run.assert_not_called()

    def test_new_request_invalidates_old_proposal(self):
        service,agent=self.make()
        service.handle('播放三体',tv_id='a')
        service.handle('今天天气怎么样',tv_id='a')
        result=service.handle('确认',tv_id='a')
        self.assertEqual(result['status'],'NEEDS_CLARIFICATION')
        agent.run.assert_not_called()

    def test_invalid_episode_zero_never_becomes_episode_one(self):
        service,agent=self.make()
        service.handle('找部悬疑剧',tv_id='a',source='text')
        service.handle('第二部',tv_id='a',source='text')
        result=service.handle('从第0集开始',tv_id='a',source='text')
        self.assertEqual(result['status'],'NEEDS_CLARIFICATION')
        agent.run.assert_not_called()

    def test_native_already_paused_is_observed_not_toggled(self):
        device=Mock()
        device.current_package.return_value='com.trim.tv'
        device.playback_status.return_value={'state':'PAUSED','playing':False}
        service,agent=self.make(device=device)
        result=service.handle('暂停',tv_id='a')
        self.assertEqual(result['status'],'SUCCESS')
        self.assertTrue(result['coordinated'])
        agent.run.assert_not_called()
        self.assertFalse(result['decision']['native_suppressed'])


class VoiceMemoryPrivacyTests(unittest.TestCase):
    def test_task_history_can_be_disabled_while_memory_remains_enabled(self):
        from adbtv.memory import MemoryStore
        from adbtv.cli import _ctx
        with tempfile.TemporaryDirectory() as tmp:
            cfg=copy.deepcopy(DEFAULTS)
            cfg['adb'].update(simulator='inprocess',serial='fake-tv:5555',fake_state=str(Path(tmp)/'tv.json'))
            cfg['execution']['state_dir']=str(Path(tmp)/'runtime')
            cfg['agent']['use_llm']=False
            cfg['memory'].update(enabled=True,path=str(Path(tmp)/'memory.sqlite3'),record_history=False)
            store=MemoryStore(cfg['memory']['path'])
            store.set_preference('preferred_app','feiniu_tv',explicit=True)
            _,_,_,_,agent=_ctx(cfg)
            outcome=agent.run('当前应用是什么')
            self.assertEqual(outcome['status'],'SUCCESS')
            self.assertEqual(store.history(),[])
            self.assertIn('preferred_app',store.preferences())
