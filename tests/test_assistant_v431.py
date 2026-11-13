"""V4.3.1: fact-claim grounding is bound to fields, not to bare numbers.

These are the adversarial reproductions that motivated the release:

* a correct total must not stand in for a wrong movie count;
* a correct movie count must not stand in for a wrong series count;
* Chinese numerals and unbracketed titles must not bypass grounding;
* a successful tool call must not launder a wrong-field answer;
* a failed tool must never be reported as a successful query;
* a real playback-progress record must be accepted, a missing one stated honestly;
* the same number appearing in two fields, and two tools in one turn, must not
  let facts leak from one field/tool into another.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.assistant_loop import AssistantLoop
from adbtv.config import DEFAULTS
from adbtv.library_tools import LibraryTools
from adbtv.media_index import MediaIndex


def build_library(root: Path):
    """1 movie + 1 series with 3 episodes: total=2, movies=1, series=1, episodes=4."""
    movie = root / '电影' / '星际穿越 (2014)'
    movie.mkdir(parents=True)
    (movie / '星际穿越.mp4').write_bytes(b'v')
    (movie / 'movie.nfo').write_text('<movie><genre>科幻</genre></movie>')
    series = root / '剧集' / '三体 (2023)' / 'Season 01'
    series.mkdir(parents=True)
    for episode in (1, 2, 3):
        (series / f'三体 S01E00{episode}.mkv').write_bytes(b'v')
    (series.parent / 'tvshow.nfo').write_text('<tvshow><genre>科幻</genre></tvshow>')
    return root


_MANY_TITLES = ('星际穿越', '盗梦空间', '阿凡达', '泰坦尼克号', '狮子王',
                '千与千寻', '让子弹飞', '霸王别姬')


def build_many_movies(root: Path, count=5):
    for title in _MANY_TITLES[:count]:
        movie = root / '电影' / title
        movie.mkdir(parents=True)
        (movie / f'{title}.mp4').write_bytes(b'v')
        (movie / 'movie.nfo').write_text('<movie><genre>剧情</genre></movie>')
    return root


def cfg_for(root):
    cfg = copy.deepcopy(DEFAULTS)
    cfg['media'] = {'enabled': True, 'roots': [str(root)]}
    cfg['voice']['assistant'] = {'enabled': True}
    return cfg


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.enabled = True
        self.model = 'fake-model'

    def chat(self, messages, system=None, timeout=None):
        if not self.replies:
            raise RuntimeError('no scripted reply')
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_library(Path(self.tmp.name))
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=MediaIndex([str(self.root)]).scan())

    def loop(self, replies):
        return AssistantLoop(self.cfg, self.tools, FakeLLM(replies))

    def summary_obs(self):
        return [{'tool': 'catalog_summary', 'arguments': {},
                 'result': self.tools.summary()}]

    def list_obs(self, kind='any', limit=20):
        return [{'tool': 'catalog_list', 'arguments': {'kind': kind, 'limit': limit},
                 'result': self.tools.browse(kind, limit)}]


class FieldBindingTests(_Base):
    """P0: every count is bound to the field it names."""

    def setUp(self):
        super().setUp()
        self.summary = self.tools.summary()
        # Guard the fixture itself: the reproduction depends on these numbers.
        self.assertEqual((self.summary['total'], self.summary['movies'],
                          self.summary['series'], self.summary['episodes']),
                         (2, 1, 1, 4))

    def test_total_correct_but_movie_count_wrong_is_rejected(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        self.assertFalse(loop.grounded('本地共有 2 部电影，都能播放。', self.summary_obs()))

    def test_movie_correct_but_series_count_wrong_is_rejected(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        self.assertFalse(loop.grounded('本地共 2 部作品，电影 1 部、剧集 2 部。',
                                       self.summary_obs()))

    def test_episode_count_is_not_a_work_count(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        # 4 is the episode count; claiming it as a work count must fail.
        self.assertFalse(loop.grounded('本地共有 4 部作品。', self.summary_obs()))
        # The same 4 is fine when it is actually the episode count.
        self.assertTrue(loop.grounded('本地共有 2 部作品，一共 4 集。', self.summary_obs()))

    def test_same_number_in_two_fields_does_not_leak(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        # 4 exists as episodes but not as movies; the claim names movies.
        self.assertFalse(loop.grounded('本地有 4 部电影。', self.summary_obs()))
        # 2 exists as total but not as movies.
        self.assertFalse(loop.grounded('本地有 2 部电影。', self.summary_obs()))

    def test_correct_field_bound_answer_is_accepted(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        self.assertTrue(loop.grounded('本地共 2 部作品，电影 1 部、剧集 1 部。',
                                      self.summary_obs()))

    def test_chinese_numerals_bind_the_same_way(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        self.assertTrue(loop.grounded('本地共两部作品，电影一部、剧集一部。',
                                      self.summary_obs()))
        self.assertFalse(loop.grounded('本地共两部作品，电影两部。', self.summary_obs()))

    def test_list_page_size_is_not_the_library_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_many_movies(Path(tmp), 5)
            cfg = cfg_for(root)
            tools = LibraryTools(cfg, index=MediaIndex([str(root)]).scan())
            loop = AssistantLoop(cfg, tools, None)
            page = tools.browse('any', 2)
            self.assertEqual((page['total'], len(page['items'])), (5, 2))
            obs = [{'tool': 'catalog_list', 'arguments': {'kind': 'any', 'limit': 2},
                    'result': page}]
            # The two rows on this page must not be spoken as "2 works in total".
            self.assertFalse(loop.grounded('本地共 2 部作品。', obs))


class ChineseAndTitleBypassTests(_Base):
    """P0: free-text fabrication with no evidence is refused."""

    def test_chinese_numeral_fabrication_without_tool_is_rejected(self):
        loop = self.loop(['{"action":"answer","text":"我查到十二部电影。"}'])
        result = loop.run('本地有多少电影', state={})
        self.assertNotIn('十二', result['reply'])
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')

    def test_arabic_numeral_without_tool_is_rejected(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        self.assertFalse(loop.grounded('一共有 12 部电影。', []))
        self.assertTrue(loop.grounded('你好，我可以帮你查本地片库。', []))

    def test_unbracketed_fabricated_title_without_tool_is_rejected(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        self.assertFalse(loop.grounded('推荐你看不存在的神剧', []))
        self.assertFalse(loop.grounded('推荐一部不存在的剧吧', []))

    def test_unbracketed_fabricated_title_after_real_tool_is_rejected(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        # Both titles are real in the library, but only these were observed.
        obs = self.list_obs('any', 20)
        self.assertTrue(loop.grounded('推荐三体和星际穿越', obs))
        self.assertFalse(loop.grounded('推荐三体和不存在的神剧', obs))

    def test_bracketed_fabricated_title_is_rejected(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        obs = self.list_obs('any', 20)
        self.assertFalse(loop.grounded('推荐《三体》和《不存在的神剧》。', obs))


class WrongFieldAndFailureTests(_Base):
    """P0: a real tool call cannot launder a wrong-field answer, nor a failure."""

    def test_tool_success_but_wrong_field_answer_is_replaced(self):
        loop = self.loop([
            '{"action":"tool","tool":"catalog_summary","arguments":{}}',
            '{"action":"answer","text":"本地共有2部电影，都能播放。"}',
        ])
        result = loop.run('现在资源库里有什么', state={})
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')
        self.assertNotIn('2 部电影', result['reply'])
        self.assertIn('电影 1 部', result['reply'])
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['answer_source'], 'local_catalog')

    def test_tool_failure_is_not_reported_as_success_by_the_model(self):
        loop = self.loop(['{"action":"tool","tool":"catalog_summary","arguments":{}}',
                          '{"action":"answer","text":"查询成功，本地有 5 部电影。"}'])
        loop.tools.summary = Mock(side_effect=RuntimeError('index exploded'))
        result = loop.run('本地有多少电影', state={})
        self.assertEqual(result['diagnostics']['fallback_reason'], 'tool_error')
        self.assertEqual(result['diagnostics']['failed_stage'], 'tool')
        self.assertNotIn('查询成功', result['reply'])

    def test_failed_tool_observation_never_earns_local_catalog_source(self):
        # Direct provenance contract: an error trace entry is not evidence.
        self.assertEqual(
            AssistantLoop.answer_source([], [{'tool': 'catalog_summary',
                                              'status': 'error',
                                              'error': 'RuntimeError'}]),
            'unverified')
        self.assertEqual(
            AssistantLoop.answer_source([{'tool': 'catalog_summary', 'result': {}}], []),
            'local_catalog')

    def test_two_tools_in_one_turn_do_not_cross_contaminate(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        summary = self.tools.summary()  # total=2, movies=1, series=1, episodes=4
        progress = {'source': 'device_status', 'available': True,
                    'evidence': 'device_observation', 'title': '三体',
                    'season': 1, 'episode': 2, 'state': 'PLAYING',
                    'playing': True, 'updated_at': None}
        obs = [{'tool': 'catalog_summary', 'arguments': {}, 'result': summary},
               {'tool': 'catalog_get_playback_progress', 'arguments': {},
                'result': progress}]
        # Correct on both tools' facts.
        self.assertTrue(loop.grounded('本地共 2 部作品，上次看到《三体》第2集。', obs))
        # The summary's episode total (4) must not become a movie count, and the
        # playback episode number (2) must not become the movie count either.
        self.assertFalse(loop.grounded('本地共 4 部电影，上次看到第2集。', obs))
        self.assertFalse(loop.grounded('本地有 2 部电影，上次看到《三体》第2集。', obs))
        # A playback episode that was never observed must be refused.
        self.assertFalse(loop.grounded('本地共 2 部作品，上次看到《三体》第4集。', obs))


class PlaybackProgressTests(_Base):
    """P1: real progress is accepted; absence is stated honestly."""

    def test_real_playback_progress_is_accepted(self):
        state = {'tv': {'title': '三体', 'season': 1, 'episode': 2,
                        'state': 'PLAYING', 'playing': True}}
        loop = self.loop([
            '{"action":"tool","tool":"catalog_get_playback_progress","arguments":{}}',
            '{"action":"answer","text":"你上次看的是《三体》第2集。"}',
        ])
        result = loop.run('我上次看到哪一集', state=state)
        self.assertEqual(result['diagnostics']['decision_source'], 'llm')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['answer_source'], 'device_status')
        self.assertIn('三体', result['reply'])
        self.assertIn('第2集', result['reply'])

    def test_progress_tool_reports_source_and_schema(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        live, _ = loop._run_tool('catalog_get_playback_progress', {},
                                 {'tv': {'title': '三体', 'season': 1, 'episode': 2,
                                         'state': 'PLAYING', 'playing': True}})
        self.assertEqual(live['source'], 'device_status')
        self.assertEqual((live['title'], live['season'], live['episode']),
                         ('三体', 1, 2))
        recent, _ = loop._run_tool('catalog_get_playback_progress', {},
                                   {'played_history': [{'title': '三体', 'season': 1,
                                                        'episode': 3}]})
        self.assertEqual(recent['source'], 'recent_task')
        self.assertEqual(recent['episode'], 3)
        empty, _ = loop._run_tool('catalog_get_playback_progress', {}, {})
        self.assertFalse(empty['available'])

    def test_missing_playback_progress_is_reported_honestly(self):
        loop = self.loop([
            '{"action":"tool","tool":"catalog_get_playback_progress","arguments":{}}',
            '{"action":"answer","text":"你上次看的是《三体》第2集。"}',
        ])
        result = loop.run('我上次看到哪一集', state={})
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')
        self.assertIn('还没有记录', result['reply'])
        self.assertNotIn('第2集', result['reply'])

    def test_progress_record_is_not_a_precise_timestamp(self):
        loop = AssistantLoop(self.cfg, self.tools, None)
        progress = {'source': 'recent_task', 'available': True,
                    'evidence': 'recent_play_record', 'title': '三体',
                    'season': 1, 'episode': 2, 'state': '', 'playing': False,
                    'updated_at': None}
        obs = [{'tool': 'catalog_get_playback_progress', 'arguments': {},
                'result': progress}]
        # A bare "第2集" record cannot be dressed up as a wall-clock time.
        self.assertFalse(loop.grounded('你上次在 21:15 看了《三体》第2集。', obs))
        self.assertTrue(loop.grounded('你上次记录到的是《三体》第2集。', obs))


if __name__ == '__main__':
    unittest.main()
