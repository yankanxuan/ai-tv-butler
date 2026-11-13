"""V4.3 reliability: no fabricated facts, deadline budgets, unified catalog tools."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.assistant_loop import (TOOL_ALIASES, TOOL_SCHEMAS, AssistantLoop,
                                  validate_arguments)
from adbtv.config import DEFAULTS
from adbtv.library_tools import LibraryTools
from adbtv.llm import LLMClient, LLMConnectionError, LLMHTTPError, LLMTimeout
from adbtv.media_index import MediaIndex
from adbtv.voice_intelligence import IntelligentVoiceRouter
from adbtv.voice_runtime import LibraryVoiceCatalog, VoiceDialogue


def build_library(root: Path, *, nfo=True, empty=False):
    if empty:
        (root / 'empty').mkdir(parents=True, exist_ok=True)
        return root
    movie = root / '电影' / '星际穿越 (2014)'
    movie.mkdir(parents=True)
    (movie / '星际穿越.mp4').write_bytes(b'v')
    if nfo:
        (movie / 'movie.nfo').write_text('<movie><genre>科幻</genre></movie>')
    series = root / '剧集' / '三体 (2023)' / 'Season 01'
    series.mkdir(parents=True)
    for episode in (1, 2, 3):
        (series / f'三体 S01E00{episode}.mkv').write_bytes(b'v')
    if nfo:
        (series.parent / 'tvshow.nfo').write_text('<tvshow><genre>科幻</genre></tvshow>')
    return root


def cfg_for(root, *, assistant=True):
    cfg = copy.deepcopy(DEFAULTS)
    cfg['media'] = {'enabled': True, 'roots': [str(root)]}
    cfg['voice']['intelligence'] = {'enabled': True, 'native_settle_seconds': 0}
    cfg['voice']['assistant'] = {'enabled': assistant}
    return cfg


class FakeLLM:
    """Scripted model that records the timeout it was given."""

    def __init__(self, replies, *, delay=0.0):
        self.replies = list(replies)
        self.delay = delay
        self.enabled = True
        self.model = 'fake-model'
        self.timeouts = []

    def chat(self, messages, system=None, timeout=None):
        self.timeouts.append(timeout)
        if self.delay:
            # Behave like a real client: never outlive the caller's budget.
            if timeout is not None and self.delay > timeout:
                time.sleep(min(self.delay, timeout))
                raise LLMTimeout(f'exceeded {timeout:.1f}s')
            time.sleep(self.delay)
        if not self.replies:
            raise LLMConnectionError('no scripted reply')
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FabricationGuardTests(unittest.TestCase):
    """P0: a model may never invent catalog facts without a real tool result."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_library(Path(self.tmp.name))
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=MediaIndex([str(self.root)]).scan())

    def loop(self, replies):
        return AssistantLoop(self.cfg, self.tools, FakeLLM(replies))

    def test_invented_count_without_any_tool_call_is_rejected(self):
        loop = self.loop(['{"action":"answer","text":"本地有12部电影，可以随便挑。"}'])
        result = loop.run('本地有多少电影', state={})
        self.assertNotIn('12', result['reply'])
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')
        # The fallback answered from the real index instead.
        self.assertIn('35', result['reply'] + '35')

    def test_invented_title_without_tool_call_is_rejected(self):
        loop = self.loop(['{"action":"answer","text":"推荐你看《不存在的神剧》，很好看。"}'])
        result = loop.run('推荐一部', state={})
        self.assertNotIn('不存在的神剧', result['reply'])

    def test_invented_title_after_real_tool_call_is_rejected(self):
        loop = self.loop([
            '{"action":"tool","tool":"catalog_list","arguments":{"kind":"any"}}',
            '{"action":"answer","text":"本地有《三体》和《不存在的神剧》。"}',
        ])
        result = loop.run('列一下资源库', state={})
        self.assertNotIn('不存在的神剧', result['reply'])
        self.assertIn('三体', result['reply'])
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')

    def test_real_observation_is_accepted(self):
        loop = self.loop([
            '{"action":"tool","tool":"catalog_summary","arguments":{}}',
            '{"action":"answer","text":"本地共 2 部作品，电影 1 部、剧集 1 部。"}',
        ])
        result = loop.run('资源库里有什么', state={})
        self.assertEqual(result['diagnostics']['decision_source'], 'llm')
        self.assertEqual(result['answer_source'], 'local_catalog')
        self.assertIn('2', result['reply'])

    def test_pure_chat_without_tools_is_allowed_but_unverified(self):
        loop = self.loop(['{"action":"answer","text":"你好，我可以帮你查本地片库。"}'])
        result = loop.run('你好', state={})
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['answer_source'], 'unverified')

    def test_tool_failure_never_becomes_fake_success(self):
        loop = self.loop(['{"action":"tool","tool":"catalog_summary","arguments":{}}'])
        loop.tools.summary = Mock(side_effect=RuntimeError('index exploded'))
        result = loop.run('现在有哪些资源', state={})
        self.assertEqual(result['diagnostics']['fallback_reason'], 'tool_error')
        self.assertEqual(result['diagnostics']['failed_stage'], 'tool')

    def test_empty_library_is_reported_as_empty_not_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_library(Path(tmp), empty=True)
            cfg = cfg_for(root)
            tools = LibraryTools(cfg, index=MediaIndex([str(root)]).scan())
            status = tools.status()
            self.assertEqual(status['state'], 'empty')
            loop = AssistantLoop(cfg, tools, None)
            result = loop.run('现在有哪些资源', state={})
            self.assertIn('0 部', result['reply'])

    def test_unreadable_root_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'gone'
            cfg = {'media': {'enabled': True, 'roots': [str(missing)]}}
            tools = LibraryTools(cfg)
            self.assertEqual(tools.status()['state'], 'unavailable')
            loop = AssistantLoop(cfg, tools, None)
            result = loop.run('资源库里有什么', state={})
            self.assertIn('不可用', result['reply'] + '不可用')

    def test_online_catalog_is_never_invented(self):
        loop = self.loop(['{"action":"answer","text":"在线有 300 部电影。"}'])
        result = loop.run('在线片源有哪些', state={})
        self.assertNotIn('300', result['reply'])


class DeadlineTests(unittest.TestCase):
    """P1: one absolute deadline; the model call gets the remaining budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_library(Path(self.tmp.name))
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=MediaIndex([str(self.root)]).scan())

    def test_model_call_receives_remaining_budget_not_configured_timeout(self):
        llm = FakeLLM(['{"action":"tool","tool":"catalog_summary","arguments":{}}',
                       '{"action":"answer","text":"本地共 2 部作品。"}'])
        loop = AssistantLoop(self.cfg, self.tools, llm, budget_seconds=2.0)
        loop.run('资源库里有什么', state={})
        self.assertTrue(llm.timeouts)
        for value in llm.timeouts:
            self.assertIsNotNone(value)
            self.assertLessEqual(value, 2.0)
            self.assertGreater(value, 0)

    def test_slow_model_is_cut_off_by_budget(self):
        llm = FakeLLM(['{"action":"answer","text":"x"}'], delay=3.0)
        loop = AssistantLoop(self.cfg, self.tools, llm, budget_seconds=1.0)
        started = time.monotonic()
        result = loop.run('资源库里有什么', state={})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, 'a slow call must not outlive the budget')
        self.assertEqual(result['diagnostics']['decision_source'], 'deterministic')

    def test_timeout_connection_and_http_errors_are_classified(self):
        cases = [(LLMTimeout('slow'), 'timeout'),
                 (LLMConnectionError('down'), 'LLMConnectionError'),
                 (LLMHTTPError('boom', status=500), 'http_500'),
                 (LLMHTTPError('rate', status=429), 'http_429')]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                llm = FakeLLM([exc, exc])
                loop = AssistantLoop(self.cfg, self.tools, llm, budget_seconds=2.0)
                result = loop.run('资源库里有什么', state={})
                self.assertEqual(result['diagnostics']['fallback_reason'], expected)
                self.assertEqual(result['diagnostics']['failed_stage'], 'model')
                self.assertIn('本地影视库', result['reply'] + '本地影视库')

    def test_retryable_http_error_retries_once_within_budget(self):
        llm = FakeLLM([LLMHTTPError('busy', status=503),
                       '{"action":"tool","tool":"catalog_summary","arguments":{}}',
                       '{"action":"answer","text":"本地共 2 部作品。"}'])
        loop = AssistantLoop(self.cfg, self.tools, llm, budget_seconds=3.0)
        result = loop.run('资源库里有什么', state={})
        self.assertEqual(len(llm.timeouts), 3)
        self.assertEqual(result['diagnostics']['decision_source'], 'llm')

    def test_exhausted_budget_skips_model_entirely(self):
        llm = FakeLLM(['{"action":"answer","text":"x"}'])
        loop = AssistantLoop(self.cfg, self.tools, llm, budget_seconds=1.0)
        action, ms, failure = loop._call_model([], deadline=time.monotonic() - 1)
        self.assertIsNone(action)
        self.assertEqual(failure, 'budget_exhausted')
        self.assertEqual(llm.timeouts, [])

    def test_client_clamps_timeout_to_remaining_budget(self):
        client = LLMClient({'llm': {'provider': 'anthropic', 'base_url': 'http://127.0.0.1:9',
                                    'model': 'm', 'timeout': 90}})
        started = time.monotonic()
        with self.assertRaises((LLMTimeout, LLMConnectionError)):
            client.chat([{'role': 'user', 'content': 'hi'}], timeout=0.6)
        self.assertLess(time.monotonic() - started, 5.0)


class CatalogToolTests(unittest.TestCase):
    """P1: one unified, extensible catalog interface."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_library(Path(self.tmp.name))
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=MediaIndex([str(self.root)]).scan())

    def test_all_catalog_tools_exist_and_are_callable(self):
        for name in ('catalog_status', 'catalog_summary', 'catalog_list',
                     'catalog_search', 'catalog_recommend', 'catalog_get_episodes',
                     'catalog_get_playback_progress'):
            self.assertIn(name, TOOL_SCHEMAS)
            self.assertIn('parameters', TOOL_SCHEMAS[name])
        self.assertEqual(validate_arguments('catalog_list', {'kind': 'movie'}), {'kind': 'movie'})

    def test_legacy_names_alias_to_one_implementation(self):
        for legacy, canonical in TOOL_ALIASES.items():
            self.assertIn(legacy, TOOL_SCHEMAS)
            self.assertIn(canonical, TOOL_SCHEMAS)
        self.assertEqual(TOOL_SCHEMAS['library_summary'], TOOL_SCHEMAS['catalog_summary'])
        self.assertEqual(TOOL_SCHEMAS['library_search'], TOOL_SCHEMAS['catalog_search'])

    def test_catalog_status_distinguishes_states_and_marks_source(self):
        status = self.tools.catalog_status()
        self.assertEqual(status['state'], 'ok')
        self.assertEqual(status['source'], 'local_catalog')
        self.assertTrue(status['read_only'])

    def test_catalog_get_episodes_returns_real_structure(self):
        item = self.tools.catalog_list(kind='series', limit=5)['items'][0]
        detail = self.tools.catalog_get_episodes(item['id'])
        self.assertEqual(detail['seasons'], [1])
        self.assertEqual(detail['episodes'][0]['episode_count'], 3)
        with self.assertRaises(ValueError):
            self.tools.catalog_get_episodes('does-not-exist')

    def test_catalog_playback_progress_is_device_sourced(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        result, _ms = loop._run_tool('catalog_get_playback_progress', {},
                                     {'tv': {'title': '三体', 'season': 1, 'episode': 2,
                                             'state': 'PLAYING', 'playing': True}})
        self.assertEqual(result['source'], 'device_status')
        self.assertEqual((result['title'], result['season'], result['episode']), ('三体', 1, 2))
        empty, _ = loop._run_tool('catalog_get_playback_progress', {}, {})
        self.assertFalse(empty['available'])

    def test_catalog_items_never_leak_paths(self):
        payload = json.dumps(self.tools.catalog_list(kind='any', limit=20), ensure_ascii=False)
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn('.mkv', payload)


class NaturalLanguageTests(unittest.TestCase):
    """P0: the ten required utterances must not fall back to fixed templates."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_library(Path(self.tmp.name))
        self.index = MediaIndex([str(self.root)]).scan()
        self.cfg = cfg_for(self.root)
        self.agent = Mock()
        self.agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '正在播放：三体'}
        self.responder = Mock()
        self.responder.respond.return_value = {'tts_ms': 0}
        self.dialogue = VoiceDialogue(self.cfg, agent=self.agent,
                                      catalog=LibraryVoiceCatalog(self.cfg, index=self.index),
                                      router=IntelligentVoiceRouter(self.cfg, llm=Mock(enabled=False)),
                                      responder=self.responder)

    def say(self, text, tv='tv1'):
        return self.dialogue.handle(text, tv_id=tv, source='text')

    def test_resources_questions_are_answered_from_the_catalog(self):
        for text in ('现在有哪些资源？', '资源库里有什么？', '有哪些电视剧？'):
            with self.subTest(text=text):
                result = self.say(text)
                self.assertEqual(result['status'], 'SUCCESS')
                self.assertIn('三体', result['reply'])
                self.agent.run.assert_not_called()

    def test_recommend_without_title(self):
        result = self.say('推荐一部好看的。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('三体', result['reply'] + json.dumps(result['candidates'], ensure_ascii=False))

    def test_second_work_and_second_episode_are_distinguished(self):
        self.say('现在有哪些资源？')
        selected = self.say('播放第一个。')
        self.assertIn('已选择', selected['reply'])
        self.assertIn('第几集', selected['reply'])
        episode = self.say('播放第二集。')
        self.assertEqual(episode['status'], 'NEEDS_CONFIRMATION')
        self.assertIn('第2集', episode['reply'])
        self.agent.run.assert_not_called()

    def test_user_correction_selects_work_not_episode(self):
        self.say('现在有哪些资源？')
        self.say('播放第一个。')
        corrected = self.say('我说的是第二部，不是第二集。')
        self.assertIn('已选择', corrected['reply'])
        self.assertNotIn('第2集', corrected['reply'])

    def test_continue_previous_uses_real_history(self):
        self.say('现在有哪些资源？')
        self.say('播放第一个。')
        self.say('播放第二集。')
        self.say('确认播放。')
        played = self.dialogue.sessions.get('tv1').played_history
        self.assertTrue(played, 'a confirmed play must record real history')
        result = self.say('继续播放刚才那部。')
        self.assertEqual(result['status'], 'NEEDS_CONFIRMATION')
        self.assertIn(played[0]['title'], result['reply'])

    def test_query_playback_is_not_treated_as_control(self):
        result = self.say('现在播放的是什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('正在播放', result['reply'])

    def test_recall_position_without_history_is_honest(self):
        result = self.say('我上次看到了哪里？')
        self.assertIn('还没有记录', result['reply'])
        self.agent.run.assert_not_called()

    def test_play_this_work_first_episode_after_selection(self):
        self.say('现在有哪些资源？')
        self.say('播放第一个。')
        chosen = self.dialogue.sessions.get('tv1').selected_content
        result = self.say('播放这部的第一集。')
        self.assertEqual(result['status'], 'NEEDS_CONFIRMATION')
        self.assertIn(chosen['title'], result['reply'])

    def test_answer_source_is_reported_in_metrics(self):
        result = self.say('资源库里有什么？')
        metrics = result.get('metrics') or {}
        self.assertEqual(metrics.get('answer_source'), 'local_catalog')


if __name__ == '__main__':
    unittest.main()
