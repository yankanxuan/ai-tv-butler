"""V4.2 assistant: read-only library tools, tool-calling loop, acceptance utterances."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.assistant_loop import (AssistantLoop, ToolError, extract_json_object,
                                  validate_action, validate_arguments)
from adbtv.config import DEFAULTS
from adbtv.library_tools import LibraryTools
from adbtv.media_index import MediaIndex
from adbtv.voice_intelligence import IntelligentVoiceRouter
from adbtv.voice_runtime import LibraryVoiceCatalog, VoiceDialogue


def build_library(root: Path, *, nfo=True):
    """movie (single unmarked file), series (SxxExx), ambiguous folder."""
    movie = root / '电影' / '星际穿越 (2014)'
    movie.mkdir(parents=True)
    (movie / '星际穿越.mp4').write_bytes(b'v')
    if nfo:
        (movie / 'movie.nfo').write_text(
            '<movie><genre>科幻</genre><country>美国</country><year>2014</year></movie>')
    series = root / '剧集' / '三体 (2023)' / 'Season 01'
    series.mkdir(parents=True)
    for episode in (1, 2, 3):
        (series / f'三体 S01E00{episode}.mkv').write_bytes(b'v')
    if nfo:
        (series.parent / 'tvshow.nfo').write_text(
            '<tvshow><genre>科幻</genre><country>中国大陆</country></tvshow>')
    ambiguous = root / '其他' / '合集'
    ambiguous.mkdir(parents=True)
    (ambiguous / 'a.mp4').write_bytes(b'v')
    (ambiguous / 'b.mp4').write_bytes(b'v')
    return root


def cfg_for(root, *, assistant=True, nfo=True):
    cfg = copy.deepcopy(DEFAULTS)
    cfg['media'] = {'enabled': True, 'roots': [str(root)]}
    cfg['voice']['intelligence'] = {'enabled': True, 'native_settle_seconds': 0}
    cfg['voice']['assistant'] = {'enabled': assistant}
    cfg['voice']['conversation'] = {'ttl_seconds': 180, 'max_turns': 8}
    return cfg


class LibraryToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        build_library(self.root)
        self.index = MediaIndex([str(self.root)]).scan()
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=self.index)

    def test_kind_is_derived_from_structure_not_directory_name(self):
        kinds = {item['title']: item['kind'] for item in self.tools.browse('any', 20)['items']}
        self.assertEqual(kinds.get('星际穿越'), 'movie')
        self.assertEqual(kinds.get('三体'), 'series')
        # A folder with several unmarked videos is ambiguous, not "series".
        self.assertNotIn('合集', kinds)
        summary = self.tools.summary()
        self.assertEqual((summary['movies'], summary['series']), (1, 1))
        self.assertEqual(summary['ambiguous_folders'], 1)
        self.assertEqual(summary['ambiguous_videos'], 2)

    def test_without_nfo_kind_still_follows_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_library(root, nfo=False)
            index = MediaIndex([tmp]).scan()
            tools = LibraryTools(cfg_for(root), index=index)
            kinds = {i['title']: i['kind'] for i in tools.browse('any', 20)['items']}
            self.assertEqual(kinds['星际穿越'], 'movie')
            self.assertEqual(kinds['三体'], 'series')
            self.assertEqual(tools.summary()['metadata_missing'], 2)

    def test_metadata_is_reported_as_missing_not_invented(self):
        summary = self.tools.summary()
        self.assertEqual(summary['with_metadata'], 2)
        self.assertEqual(summary['metadata_missing'], 0)
        result = self.tools.search(genre='悬疑')
        self.assertEqual(result['items'], [])
        self.assertEqual(result['total'], 0)

    def test_search_by_query_genre_and_year(self):
        self.assertEqual([i['title'] for i in self.tools.search(query='三体')['items']], ['三体'])
        self.assertEqual({i['title'] for i in self.tools.search(genre='科幻')['items']},
                         {'三体', '星际穿越'})
        self.assertEqual([i['title'] for i in self.tools.search(year=2014)['items']], ['星际穿越'])
        self.assertEqual(self.tools.search(kind='movie')['total'], 1)

    def test_recommend_is_deterministic_and_honours_exclusions(self):
        first = self.tools.recommend(limit=2)
        second = self.tools.recommend(limit=2)
        self.assertEqual([i['id'] for i in first['items']], [i['id'] for i in second['items']])
        excluded = first['items'][0]['id']
        remaining = self.tools.recommend(limit=5, exclusions=[excluded])
        self.assertNotIn(excluded, [i['id'] for i in remaining['items']])
        self.assertTrue(all('reasons' in item for item in first['items']))

    def test_recommend_max_episodes_filters_real_episode_counts(self):
        result = self.tools.recommend(limit=5, max_episodes=1)
        self.assertEqual([i['title'] for i in result['items']], ['星际穿越'])

    def test_recent_is_ordered_by_real_mtime(self):
        recent = self.tools.recent(limit=5)
        self.assertTrue(recent['items'])
        self.assertGreaterEqual(recent['items'][0]['updated_at'], recent['items'][-1]['updated_at'])

    def test_health_reports_roots_and_read_only(self):
        health = self.tools.health()
        self.assertTrue(health['available'])
        self.assertTrue(health['read_only'])
        self.assertEqual(health['roots'][0]['root'], str(self.root))
        self.assertTrue(health['roots'][0]['readable'])

    def test_missing_root_is_unavailable_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'nope'
            tools = LibraryTools({'media': {'enabled': True, 'roots': [str(missing)]}})
            health = tools.health()
            self.assertFalse(health['available'])
            self.assertFalse(health['roots'][0]['exists'])

    def test_projected_items_never_expose_filesystem_paths(self):
        payload = json.dumps(self.tools.browse('any', 20), ensure_ascii=False)
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn('/media', payload)
        self.assertNotIn('.mkv', payload)

    def test_find_requires_exact_unique_title(self):
        self.assertEqual(self.tools.find('三体')['title'], '三体')
        self.assertIsNone(self.tools.find('不存在的剧名'))


class SchemaValidationTests(unittest.TestCase):
    def test_unknown_tool_and_arguments_rejected(self):
        with self.assertRaises(ToolError):
            validate_arguments('shell_exec', {})
        with self.assertRaises(ToolError):
            validate_arguments('library_browse', {'path': '/etc/passwd'})

    def test_types_enums_and_ranges_enforced(self):
        for tool, arguments in (
                ('library_browse', {'kind': 'cartoon'}),
                ('library_browse', {'limit': 999}),
                ('library_browse', {'limit': '10'}),
                ('library_search', {'year': 'yesterday'}),
                ('library_recommend', {'max_episodes': 0}),
                ('library_recommend', {'preferences': 'action'})):
            with self.subTest(tool=tool, arguments=arguments), self.assertRaises(ToolError):
                validate_arguments(tool, arguments)

    def test_shell_path_and_url_content_rejected(self):
        for arguments in ({'query': 'adb shell rm -rf /'},
                          {'query': '/media/短剧/x.mp4'},
                          {'query': 'http://evil.example'},
                          {'query': 'select * from tasks'}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                validate_arguments('library_search', arguments)

    def test_action_envelope_is_closed(self):
        with self.assertRaises(ToolError):
            validate_action({'action': 'exec', 'command': 'adb shell'})
        with self.assertRaises(ToolError):
            validate_action({'action': 'tool', 'tool': 'library_summary',
                             'arguments': {}, 'extra': 1})
        self.assertEqual(validate_action({'action': 'tool', 'tool': 'library_summary'}),
                         {'action': 'tool', 'tool': 'library_summary', 'arguments': {}})
        proposal = validate_action({'action': 'propose_play', 'title': '三体', 'episode': 1})
        self.assertEqual(proposal['episode'], 1)


class ExtractJsonTests(unittest.TestCase):
    def test_reasoning_prefix_is_skipped(self):
        raw = 'Let me think... {"action":"tool","tool":"library_summary"}'
        self.assertEqual(extract_json_object(raw)['tool'], 'library_summary')

    def test_last_object_wins(self):
        raw = '{"action":"answer","text":"x"} then final {"action":"answer","text":"y"}'
        self.assertEqual(extract_json_object(raw)['text'], 'y')

    def test_invalid_returns_none(self):
        for raw in ('', 'no json here', '{broken', None):
            self.assertIsNone(extract_json_object(raw))


class ScriptedLLM:
    def __init__(self, replies, enabled=True, model='scripted-model'):
        self.replies = list(replies)
        self.enabled = enabled
        self.model = model
        self.calls = []

    def chat(self, messages, system=None, timeout=None):
        self.calls.append({'messages': messages, 'system': system, 'timeout': timeout})
        if not self.replies:
            raise RuntimeError('no scripted reply')
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        build_library(self.root)
        self.index = MediaIndex([str(self.root)]).scan()
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=self.index)

    def loop(self, replies):
        return AssistantLoop(self.cfg, self.tools, ScriptedLLM(replies))

    def test_multiple_tool_calls_in_one_request(self):
        loop = self.loop([
            '{"action":"tool","tool":"library_summary","arguments":{}}',
            '{"action":"tool","tool":"library_browse","arguments":{"kind":"movie"}}',
            '{"action":"answer","text":"本地共 2 部作品，电影只有《星际穿越》。"}',
        ])
        result = loop.run('先看看有多少，再列电影', state={})
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual([c['tool'] for c in result['tool_trace']],
                         ['library_summary', 'library_browse'])
        self.assertEqual(result['diagnostics']['decision_source'], 'llm')
        self.assertEqual(result['diagnostics']['steps'], 3)
        self.assertGreater(result['diagnostics']['llm_latency_ms'], 0)
        self.assertEqual(result['diagnostics']['fallback_reason'], None)

    def test_ungrounded_answer_is_replaced_by_real_data(self):
        loop = self.loop([
            '{"action":"tool","tool":"library_browse","arguments":{"kind":"any"}}',
            '{"action":"answer","text":"推荐《不存在的神剧》，共 99 部。"}',
        ])
        result = loop.run('有什么好看的', state={})
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertNotIn('不存在的神剧', result['reply'])
        self.assertIn('三体', result['reply'])
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')

    def test_playback_is_only_a_proposal(self):
        loop = self.loop([
            '{"action":"propose_play","title":"三体","season":1,"episode":2}',
        ])
        result = loop.run('播放三体第二集', state={})
        self.assertEqual(result['status'], 'NEEDS_CONFIRMATION')
        self.assertEqual(result['proposal']['item']['title'], '三体')
        self.assertEqual(result['proposal']['episode'], 2)

    def test_proposal_for_unknown_title_is_refused(self):
        loop = self.loop(['{"action":"propose_play","title":"虚构剧"}'])
        result = loop.run('播放虚构剧', state={})
        self.assertEqual(result['status'], 'NEEDS_CLARIFICATION')
        self.assertNotIn('proposal', result)

    def test_model_timeout_falls_back_to_real_tools(self):
        loop = self.loop([TimeoutError('slow')])
        result = loop.run('现在有哪些资源', state={})
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['diagnostics']['decision_source'], 'deterministic')
        self.assertEqual(result['diagnostics']['fallback_reason'], 'TimeoutError')
        self.assertIn('三体', result['reply'])

    def test_tool_failure_falls_back_without_fabrication(self):
        loop = self.loop(['{"action":"tool","tool":"library_browse","arguments":{}}'])
        loop.tools.browse = Mock(side_effect=ValueError('boom'))
        result = loop.run('随便列几部', state={})
        self.assertEqual(result['diagnostics']['fallback_reason'], 'tool_error')
        # Fallback answers from real data only; no invented titles or counts.
        self.assertIn('三体', result['reply'])
        self.assertNotIn('《不存在的', result['reply'])

    def test_invalid_json_falls_back(self):
        loop = self.loop(['这不是 JSON'])
        result = loop.run('最近新加了什么', state={})
        self.assertEqual(result['diagnostics']['fallback_reason'], 'invalid_json')
        self.assertEqual(result['status'], 'SUCCESS')

    def test_loop_is_bounded(self):
        loop = self.loop(['{"action":"tool","tool":"library_summary","arguments":{}}'] * 6)
        result = loop.run('一直查', state={})
        self.assertLessEqual(result['diagnostics']['steps'], loop.max_steps)


class DialogueAcceptanceTests(unittest.TestCase):
    """The 15 required utterances, through the real dialogue entry point."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        build_library(self.root)
        self.index = MediaIndex([str(self.root)]).scan()
        self.cfg = cfg_for(self.root)
        self.agent = Mock()
        self.agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '正在播放：三体'}
        self.responder = Mock()
        self.responder.respond.return_value = {'tts_ms': 0}
        self.catalog = LibraryVoiceCatalog(self.cfg, index=self.index)
        self.router = IntelligentVoiceRouter(self.cfg, llm=Mock(enabled=False))
        self.dialogue = VoiceDialogue(self.cfg, agent=self.agent, catalog=self.catalog,
                                      router=self.router, responder=self.responder)

    def say(self, text, tv='tv1'):
        return self.dialogue.handle(text, tv_id=tv, source='text')

    def test_01_resources_overview(self):
        result = self.say('现在有哪些资源？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('三体', result['reply'])
        self.agent.run.assert_not_called()

    def test_02_library_contents(self):
        result = self.say('资源库里边有什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('星际穿越', result['reply'])
        self.agent.run.assert_not_called()

    def test_03_movie_count(self):
        result = self.say('一共有多少部电影？')
        self.assertIn('1 部电影', result['reply'])
        self.agent.run.assert_not_called()

    def test_04_recent_additions(self):
        result = self.say('最近新加了什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('最近', result['reply'])

    def test_05_recommend_a_movie(self):
        result = self.say('随便推荐一部电影。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('星际穿越', result['reply'])
        self.agent.run.assert_not_called()

    def test_06_genre_request_explains_missing_metadata(self):
        result = self.say('找一部悬疑的。')
        self.assertIn('悬疑', result['reply'])
        self.assertIn('没有', result['reply'])
        self.agent.run.assert_not_called()

    def test_07_short_preference_inherits_context(self):
        self.say('随便推荐一部。')
        result = self.say('不要太长的。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('集', result['reply'])

    def test_08_second_candidate_is_described(self):
        self.say('现在有哪些资源？')
        result = self.say('第二个是什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('第2个', result['reply'])
        self.agent.run.assert_not_called()

    def test_09_similar_request_uses_real_items(self):
        result = self.say('换一部类似的。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('三体', result['reply'] + json.dumps(result['candidates'], ensure_ascii=False))

    def test_10_recall_position_without_history_is_honest(self):
        result = self.say('我上次看到了哪里？')
        self.assertIn('还没有记录', result['reply'])
        self.agent.run.assert_not_called()

    def test_11_play_first_episode_requires_confirmation(self):
        self.say('现在有哪些资源？')
        result = self.say('播放三体第一集。')
        self.assertEqual(result['status'], 'NEEDS_CONFIRMATION')
        self.agent.run.assert_not_called()
        self.assertIn('三体', result['reply'])

    def test_12_confirm_executes_once(self):
        self.say('现在有哪些资源？')
        self.say('播放三体第一集。')
        result = self.say('确认播放。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(self.agent.run.call_count, 1)
        self.assertIn('三体', self.agent.run.call_args[0][0])
        # A second confirmation must not execute again.
        self.say('确认播放。')
        self.assertEqual(self.agent.run.call_count, 1)

    def test_13_cancel_clears_pending_without_execution(self):
        self.say('现在有哪些资源？')
        self.say('播放三体第一集。')
        result = self.say('取消刚才的操作。')
        self.assertEqual(result['status'], 'NEEDS_CLARIFICATION')
        self.assertIn('没有执行', result['reply'])
        self.agent.run.assert_not_called()

    def test_14_current_playback_is_a_factual_query(self):
        result = self.say('电视现在正在放什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('正在播放', result['reply'])
        self.assertEqual(self.agent.run.call_count, 1)

    def test_15_unavailable_library_reports_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfg_for(Path(tmp))
            cfg['media'] = {'enabled': True, 'roots': [str(Path(tmp) / 'missing')]}
            dialogue = VoiceDialogue(cfg, agent=self.agent,
                                     catalog=LibraryVoiceCatalog(cfg),
                                     router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                                     responder=self.responder)
            result = dialogue.handle('资源库不可用时还能做什么？', tv_id='tv2', source='text')
            self.assertIn('不可用', result['reply'])
            self.assertIn('查询本地影视库', result['reply'])
            self.agent.run.assert_not_called()


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def responder():
        mock = Mock()
        mock.respond.return_value = {'tts_ms': 0}
        return mock

    def tools(self, *, nfo=True):
        build_library(self.root, nfo=nfo)
        index = MediaIndex([str(self.root)]).scan()
        return LibraryTools(cfg_for(self.root), index=index)

    def test_empty_library_is_reported_as_empty_not_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfg_for(Path(tmp))
            tools = LibraryTools(cfg, index=MediaIndex([tmp]).scan())
            loop = AssistantLoop(cfg, tools, None)
            result = loop.run('现在有哪些资源', state={})
            self.assertIn('0 部', result['reply'])

    def test_no_metadata_genre_request_names_the_gap(self):
        tools = self.tools(nfo=False)
        loop = AssistantLoop(cfg_for(self.root), tools, None)
        result = loop.run('找一部科幻的', state={})
        self.assertIn('元数据', result['reply'])

    def test_session_state_tracks_query_and_tools(self):
        build_library(self.root)
        index = MediaIndex([str(self.root)]).scan()
        cfg = cfg_for(self.root)
        dialogue = VoiceDialogue(cfg, agent=Mock(), catalog=LibraryVoiceCatalog(cfg, index=index),
                                 router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                                 responder=self.responder())
        dialogue.handle('随便推荐一部。', tv_id='tv1', source='text')
        state = dialogue.sessions.assistant_state('tv1')
        self.assertEqual(state.get('last_intent'), 'assistant')
        self.assertTrue(state.get('last_tools'))
        self.assertEqual(state.get('library_scope'), 'local')

    def test_two_users_do_not_share_candidates(self):
        build_library(self.root)
        index = MediaIndex([str(self.root)]).scan()
        cfg = cfg_for(self.root)
        dialogue = VoiceDialogue(cfg, agent=Mock(), catalog=LibraryVoiceCatalog(cfg, index=index),
                                 router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                                 responder=self.responder())
        dialogue.handle('现在有哪些资源？', tv_id='a', source='text')
        dialogue.handle('现在有哪些资源？', tv_id='b', source='text')
        self.assertNotEqual(dialogue.sessions.get('a').session_id,
                            dialogue.sessions.get('b').session_id)
        self.assertEqual(dialogue.sessions.get('a').recent_results,
                         dialogue.sessions.get('b').recent_results)

    def test_expired_session_loses_candidates(self):
        build_library(self.root)
        index = MediaIndex([str(self.root)]).scan()
        cfg = cfg_for(self.root)
        cfg['voice']['conversation'] = {'ttl_seconds': 0.05, 'max_turns': 8}
        clock = [1000.0]
        dialogue = VoiceDialogue(cfg, agent=Mock(),
                                 catalog=LibraryVoiceCatalog(cfg, index=index),
                                 router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                                 responder=self.responder(), clock=lambda: clock[0])
        dialogue.handle('现在有哪些资源？', tv_id='tv1', source='text')
        self.assertTrue(dialogue.sessions.get('tv1').recent_results)
        clock[0] += 1
        self.assertEqual(dialogue.sessions.get('tv1').recent_results, [])

    def test_preferences_persist_only_when_enabled(self):
        build_library(self.root)
        index = MediaIndex([str(self.root)]).scan()
        path = self.root / 'prefs.json'
        cfg = cfg_for(self.root)
        cfg['voice']['assistant'] = {'enabled': True, 'remember_preferences': True,
                                     'preferences_path': str(path)}
        dialogue = VoiceDialogue(cfg, agent=Mock(), catalog=LibraryVoiceCatalog(cfg, index=index),
                                 router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                                 responder=self.responder())
        dialogue._remember_preferences('tv1', {'genres': ['科幻'], 'max_episodes': 120})
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())['tv1']['genres'], ['科幻'])

        off = cfg_for(self.root)
        off['voice']['assistant'] = {'enabled': True}
        quiet = VoiceDialogue(off, agent=Mock(), catalog=LibraryVoiceCatalog(off, index=index),
                              router=IntelligentVoiceRouter(off, llm=Mock(enabled=False)),
                              responder=self.responder())
        quiet._remember_preferences('tv1', {'genres': ['科幻']})
        self.assertIsNone(quiet.preferences_path)


if __name__ == '__main__':
    unittest.main()
