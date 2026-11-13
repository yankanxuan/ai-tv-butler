"""Regression checks for factual inventory dialogue, without a live NAS/TV."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.media_index import MediaIndex
from adbtv.voice_intelligence import IntelligentVoiceRouter, validate_decision, DecisionError
from adbtv.voice_runtime import VoiceDialogue, LibraryVoiceCatalog


class BrowseRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['voice']['intelligence'] = {'enabled': True, 'llm_timeout_seconds': .1}
        self.model = Mock(enabled=False)
        self.router = IntelligentVoiceRouter(self.cfg, llm=self.model)

    def test_inventory_variants_route_without_llm(self):
        for text in ('现在有哪些资源', '现在资源库里边有什么', '你现在有哪些资源',
                     '资源库里有什么', '请列出影视库', '现在有多少部电影', '媒体库有什么电视剧'):
            with self.subTest(text=text):
                decision = self.router.decide(text)
                self.assertEqual(decision.intent, 'browse_library')
                self.assertEqual(decision.route, 'agent')
                self.assertEqual(decision.parameters['scope'], 'local')
        self.model.chat.assert_not_called()

    def test_online_only_is_not_mistaken_for_local(self):
        for text in ('在线片源有哪些', '查看所有在线资源', '网络片源有哪些'):
            with self.subTest(text=text):
                self.assertEqual(self.router.decide(text).parameters['scope'], 'online')
        self.assertEqual(self.router.decide('本地和在线有哪些资源').parameters['scope'], 'all')

    def test_existing_commands_do_not_become_inventory(self):
        for text, expected in [('现在播放的是什么', 'query_media'),
                               ('暂停', 'pause'), ('打开飞牛', 'open'),
                               ('当前应用是什么', 'query_app')]:
            with self.subTest(text=text):
                self.assertEqual(self.router.decide(text).intent, expected)

    def test_model_can_request_safe_read_only_action(self):
        result = dict(route='agent', intent='browse_library', target='',
                      parameters={'scope': 'local', 'kind': 'movie'},
                      confidence=.95, reply='', risk='none')
        self.assertEqual(validate_decision(result).intent, 'browse_library')
        with self.assertRaises(DecisionError):
            validate_decision({**result, 'parameters': {'scope': '../../tmp'}})
        self.model.enabled = True
        self.model.chat.return_value = json.dumps(result)
        decision = self.router.decide('简单介绍一下 NAS 上收藏了什么', {})
        self.assertEqual(decision.intent, 'browse_library')
        self.assertFalse(decision.requires_confirmation)

    def test_offline_fallback_is_diagnostic(self):
        decision = self.router.decide('随便来点好看的')
        self.assertEqual(decision.reason, 'model_unavailable')
        self.assertIn('大模型', decision.reply)


class BrowseDialogueTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['voice']['intelligence'] = {'enabled': True, 'native_settle_seconds': 0}
        self.model = Mock(enabled=False)
        self.router = IntelligentVoiceRouter(self.cfg, llm=self.model)
        self.agent = Mock()
        self.agent.run.return_value = {'status': 'SUCCESS', 'ok': True}

    def make(self, catalog):
        return VoiceDialogue(self.cfg, agent=self.agent, catalog=catalog, router=self.router)

    def test_actual_index_counts_and_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '电影' / '星际穿越').mkdir(parents=True)
            (root / '剧集' / '三体').mkdir(parents=True)
            (root / '电影' / '星际穿越' / 'E01.mp4').write_bytes(b'')
            (root / '剧集' / '三体' / 'S01E01.mkv').write_bytes(b'')
            idx = MediaIndex([tmp]).scan()
            cat = LibraryVoiceCatalog(self.cfg, index=idx)
            data = cat.overview()
            self.assertEqual(data['total'], 2)
            self.assertEqual(data['movies'], 1)
            self.assertEqual(data['series'], 1)
            dialogue = self.make(cat)
            answer = dialogue.handle('现在资源库里边有什么', tv_id='tv1', source='text')
            self.assertEqual(answer['status'], 'SUCCESS')
            self.assertEqual(answer['decision']['intent'], 'browse_library')
            self.assertIn('共 2 部影视作品', answer['reply'])
            self.assertIn('星际穿越', answer['reply'])
            self.assertIn('三体', answer['reply'])
            self.assertEqual(len(answer['candidates']), 2)
            self.agent.run.assert_not_called()
            # Selection remains factual after the browse response.
            chosen = dialogue.handle('第一部', tv_id='tv1', source='text')
            self.assertEqual(chosen['status'], 'NEEDS_CLARIFICATION')
            self.assertIsNotNone(dialogue.sessions.get('tv1').selected_content)

    def test_no_index_does_not_claim_library_empty(self):
        cat = LibraryVoiceCatalog(self.cfg)
        result = self.make(cat).handle('资源库有哪些', tv_id='tv1', source='text')
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIn('media.enabled', result['reply'])
        self.assertNotIn('共 0', result['reply'])
        self.agent.run.assert_not_called()

    def test_online_unknown_is_not_fabricated(self):
        cat = LibraryVoiceCatalog(self.cfg)
        result = self.make(cat).handle('在线片源有哪些', tv_id='tv1', source='text')
        self.assertEqual(result['status'], 'UNSUPPORTED')
        self.assertIn('不能核实', result['reply'])
        self.assertEqual(result['candidates'], [])
        self.agent.run.assert_not_called()

    def test_cache_empty_index_is_not_rescanned_every_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = MediaIndex([tmp]).scan()
            cat = LibraryVoiceCatalog(self.cfg, index=index)
            self.assertEqual(cat.overview()['total'], 0)
            stamp = cat._loaded_at
            self.assertEqual(cat.overview()['total'], 0)
            self.assertEqual(cat._loaded_at, stamp)


if __name__ == '__main__':
    unittest.main()
