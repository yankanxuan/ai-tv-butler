"""V4.3.1 follow-up: one-word answers to a pending selection must not be lost.

Reproduces the web-console bug: after "找一部短剧" lists candidates and asks
"请选择第几部", the next turn's device observation calls update_observation(),
which clears ``waiting_question``. A bare answer like "3" then fell through to
the model/router and came back as NEEDS_CLARIFICATION ("大模型响应超时").
These tests use a device so the observation path actually runs.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.media_index import MediaIndex
from adbtv.voice_intelligence import IntelligentVoiceRouter
from adbtv.voice_input import SharedSessionStore, VoiceInputRegistry
from adbtv.voice_runtime import LibraryVoiceCatalog, VoiceDialogue


def build_series_library(root: Path):
    titles = ('星际穿越', '盗梦空间', '阿凡达', '泰坦尼克号')
    for title in titles:
        season = root / '剧集' / title / 'Season 01'
        season.mkdir(parents=True)
        for episode in (1, 2, 3):
            (season / f'{title} S01E00{episode}.mkv').write_bytes(b'v')
        (season.parent / 'tvshow.nfo').write_text('<tvshow><genre>剧情</genre></tvshow>')
    return root


class _Device:
    """Minimal device stub: observing it clears waiting_question (the real bug)."""

    def current_package(self):
        return 'com.mitv.mivideoplayer'

    def playback_status(self, include_ui=False):
        return {'state': 'playing', 'playing': True}


class PendingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_series_library(Path(self.tmp.name))
        self.index = MediaIndex([str(self.root)]).scan()
        cfg = copy.deepcopy(DEFAULTS)
        cfg['media'] = {'enabled': True, 'roots': [str(self.root)]}
        # observe_ttl_seconds=0 forces a device observation every turn, which is
        # what used to wipe the pending selection.
        cfg['voice']['intelligence'] = {'enabled': True, 'native_settle_seconds': 0,
                                        'observe_ttl_seconds': 0}
        cfg['voice']['assistant'] = {'enabled': True}
        self.cfg = cfg
        self.agent = Mock()
        self.agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '已播放'}
        self.responder = Mock()
        self.responder.respond.return_value = {'tts_ms': 0}

    def dialogue(self, *, device=True):
        return VoiceDialogue(self.cfg, agent=self.agent,
                             device=_Device() if device else None,
                             catalog=LibraryVoiceCatalog(self.cfg, index=self.index),
                             router=IntelligentVoiceRouter(self.cfg, llm=Mock(enabled=False)),
                             responder=self.responder)

    def test_bare_arabic_number_selects_third_candidate(self):
        d = self.dialogue()
        listed = d.handle('找一部短剧', tv_id='tv1', source='text')
        self.assertIn('请选择第几部', listed['reply'])
        picked = d.handle('3', tv_id='tv1', source='text')
        self.assertIn('已选择', picked['reply'])
        # The third item in the factual list is 盗梦空间.
        self.assertIn('盗梦空间', picked['reply'])
        self.assertNotIn('大模型', picked['reply'])
        self.assertEqual(picked['status'], 'NEEDS_CLARIFICATION')

    def test_bare_chinese_number_selects_candidate(self):
        d = self.dialogue()
        d.handle('找一部短剧', tv_id='tv1', source='text')
        picked = d.handle('第三', tv_id='tv1', source='text')
        self.assertIn('已选择', picked['reply'])
        self.assertIn('盗梦空间', picked['reply'])

    def test_di_number_form_still_selects(self):
        d = self.dialogue()
        d.handle('找一部短剧', tv_id='tv1', source='text')
        picked = d.handle('第3个', tv_id='tv1', source='text')
        self.assertIn('已选择', picked['reply'])

    def test_bare_episode_number_after_selection_confirms_play(self):
        d = self.dialogue()
        d.handle('找一部短剧', tv_id='tv1', source='text')
        d.handle('3', tv_id='tv1', source='text')
        episode = d.handle('2', tv_id='tv1', source='text')
        self.assertEqual(episode['status'], 'NEEDS_CONFIRMATION')
        self.assertIn('第2集', episode['reply'])
        self.agent.run.assert_not_called()
        confirmed = d.handle('确认', tv_id='tv1', source='text')
        self.assertEqual(confirmed['status'], 'SUCCESS')
        self.assertIn('盗梦空间', self.agent.run.call_args[0][0])
        self.assertIn('第2集', self.agent.run.call_args[0][0])

    def test_bare_number_without_pending_question_is_not_a_selection(self):
        d = self.dialogue()
        result = d.handle('3', tv_id='tv1', source='text')
        self.assertNotIn('已选择', result['reply'])

    def test_out_of_range_ordinal_asks_to_reselect(self):
        d = self.dialogue()
        d.handle('找一部短剧', tv_id='tv1', source='text')
        picked = d.handle('9', tv_id='tv1', source='text')
        self.assertNotIn('已选择', picked['reply'])
        self.assertEqual(picked['status'], 'NEEDS_CLARIFICATION')


class SharedStoreSelectionTests(unittest.TestCase):
    """The production web console hydrates a JSON snapshot every turn.

    ConversationManager.hydrate() used to unconditionally drop waiting_question,
    so the pending "请选择第几部" was gone before handle() even ran. This drives
    the real SharedSessionStore + VoiceInputRegistry path.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = build_series_library(Path(self.tmp.name))
        index = MediaIndex([str(root)]).scan()
        cfg = copy.deepcopy(DEFAULTS)
        cfg['media'] = {'enabled': True, 'roots': [str(root)]}
        cfg['voice']['intelligence'] = {'enabled': True, 'native_settle_seconds': 0,
                                        'observe_ttl_seconds': 0}
        cfg['voice']['assistant'] = {'enabled': True}
        agent = Mock()
        agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '已播放'}
        responder = Mock()
        responder.respond.return_value = {'tts_ms': 0}
        dialogue = VoiceDialogue(cfg, agent=agent, device=_Device(),
                                 catalog=LibraryVoiceCatalog(cfg, index=index),
                                 router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                                 responder=responder)
        shared = Path(self.tmp.name) / 'shared'
        shared.mkdir(parents=True, exist_ok=True)
        self.registry = VoiceInputRegistry(dialogue, store=SharedSessionStore(shared),
                                           tv_id='tv-shared')

    def test_bare_number_survives_shared_store_round_trip(self):
        listed = self.registry.submit_text('找一部短剧', source='web_microphone')
        self.assertIn('请选择第几部', listed['reply'])
        picked = self.registry.submit_text('3', source='web_microphone')
        self.assertIn('已选择', picked['reply'])
        self.assertIn('盗梦空间', picked['reply'])
        self.assertNotIn('大模型', picked['reply'])

    def test_episode_number_survives_shared_store_round_trip(self):
        self.registry.submit_text('找一部短剧', source='web_microphone')
        self.registry.submit_text('3', source='web_microphone')
        episode = self.registry.submit_text('2', source='web_microphone')
        self.assertEqual(episode['status'], 'NEEDS_CONFIRMATION')
        self.assertIn('第2集', episode['reply'])


if __name__ == '__main__':
    unittest.main()
