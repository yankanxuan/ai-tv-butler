"""V4.3.2: final media-fact gaps.

Three classes of failure, each reproduced then fixed:

* a recommendation page of N rows could prove "N 部电影" even when every row is
  a series (count without kind);
* quantifier/word-order variants ("100 个电影", "电影有 100 条") bypassed counts;
* attributes with no tool evidence (year, media type, genre, runtime, playback,
  recency/reason) could be filled in by the model.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from adbtv.assistant_loop import AssistantLoop
from adbtv.config import DEFAULTS
from adbtv.library_tools import LibraryTools
from adbtv.media_index import MediaIndex


def build_mixed(root: Path):
    """1 movie (2014) + 1 series (3 episodes): total 2, movies 1, series 1."""
    movie = root / '电影' / '星际穿越 (2014)'
    movie.mkdir(parents=True)
    (movie / '星际穿越.mp4').write_bytes(b'v')
    (movie / 'movie.nfo').write_text('<movie><genre>科幻</genre><year>2014</year></movie>')
    series = root / '剧集' / '三体 (2023)' / 'Season 01'
    series.mkdir(parents=True)
    for episode in (1, 2, 3):
        (series / f'三体 S01E00{episode}.mkv').write_bytes(b'v')
    (series.parent / 'tvshow.nfo').write_text('<tvshow><genre>科幻</genre></tvshow>')
    return root


def build_series_only(root: Path, count=3):
    titles = ('一身腱子肉', '万妖图录传', '从小兵开始', '从布衣开始成圣')[0:count]
    for title in titles:
        season = root / '剧集' / title / 'Season 01'
        season.mkdir(parents=True)
        for episode in (1, 2, 3):
            (season / f'{title} S01E00{episode}.mkv').write_bytes(b'v')
        (season.parent / 'tvshow.nfo').write_text('<tvshow><genre>剧情</genre></tvshow>')
    return root


def build_unknown(root: Path):
    folder = root / '其他' / '无名合集'
    folder.mkdir(parents=True)
    (folder / 'a.mp4').write_bytes(b'v')
    (folder / 'b.mp4').write_bytes(b'v')
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
        return self.replies.pop(0)


class _Base(unittest.TestCase):
    builder = staticmethod(build_mixed)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.builder(Path(self.tmp.name))
        self.cfg = cfg_for(self.root)
        self.tools = LibraryTools(self.cfg, index=MediaIndex([str(self.root)]).scan())
        self.loop = AssistantLoop(self.cfg, self.tools, None)

    def run_loop(self, replies, text):
        return AssistantLoop(self.cfg, self.tools, FakeLLM(replies)).run(text, state={})


class RecommendKindTests(_Base):
    """Problem 1: a returned row count must not stand in for a media type."""

    builder = staticmethod(build_series_only)

    def setUp(self):
        super().setUp()
        self.summary = self.tools.summary()
        self.assertEqual(self.summary['movies'], 0)
        self.assertEqual(self.summary['series'], 3)
        self.recommend = self.tools.recommend(limit=3)
        self.assertEqual(len(self.recommend['items']), 3)
        self.assertTrue(all(i['kind'] == 'series' for i in self.recommend['items']))
        self.obs = [{'tool': 'catalog_recommend', 'arguments': {'limit': 3},
                     'result': self.recommend}]
        self.titles = [i['title'] for i in self.recommend['items']]

    def test_zero_movies_three_series_cannot_be_called_three_movies(self):
        text = '我给你推荐了三部电影：' + '、'.join(f'《{t}》' for t in self.titles) + '。'
        self.assertFalse(self.loop.grounded(text, self.obs))

    def test_three_series_may_be_called_three_series(self):
        text = '我给你推荐了三部剧集：' + '、'.join(f'《{t}》' for t in self.titles) + '。'
        self.assertTrue(self.loop.grounded(text, self.obs))

    def test_three_series_may_be_called_three_works(self):
        text = '给你推荐这三部作品：' + '、'.join(f'《{t}》' for t in self.titles) + '。'
        self.assertTrue(self.loop.grounded(text, self.obs))

    def test_full_loop_replaces_mislabelled_recommendation(self):
        answer = '我给你推荐了三部电影：' + '、'.join(f'《{t}》' for t in self.titles) + '。'
        result = self.run_loop([
            '{"action":"tool","tool":"catalog_recommend","arguments":{"limit":3}}',
            '{"action":"answer","text":"' + answer + '"}',
        ], '给我推荐三部')
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')
        self.assertNotIn('三部电影', result['reply'])
        self.assertIn(self.titles[0], result['reply'])

    def test_row_count_and_real_classification_mismatch(self):
        # 3 rows, 0 movies: "这三部电影" must fail while "这三部剧集" passes.
        self.assertFalse(self.loop.grounded('这三部电影都挺好看。', self.obs))
        self.assertTrue(self.loop.grounded('这三部剧集都挺好看。', self.obs))


class MixedCountKindTests(_Base):
    """1 movie + 1 series: two rows are not two movies."""

    def setUp(self):
        super().setUp()
        self.summary = self.tools.summary()
        self.assertEqual((self.summary['movies'], self.summary['series']), (1, 1))
        self.obs_summary = [{'tool': 'catalog_summary', 'arguments': {},
                             'result': self.summary}]
        self.list_all = self.tools.browse('any', 10)
        self.obs_list = [{'tool': 'catalog_list', 'arguments': {'kind': 'any'},
                          'result': self.list_all}]

    def test_one_movie_one_series_not_two_movies(self):
        self.assertFalse(self.loop.grounded('本地共有 2 部电影。', self.obs_summary))
        self.assertFalse(self.loop.grounded('这两部电影都不错。', self.obs_list))

    def test_one_movie_one_series_not_two_series(self):
        self.assertFalse(self.loop.grounded('本地共有 2 部剧集。', self.obs_summary))


class QuantifierTests(_Base):
    """Problem 2: 个/条/部片子 and word order must be parsed and checked."""

    def setUp(self):
        super().setUp()
        self.summary = self.tools.summary()  # movies=1
        self.obs = [{'tool': 'catalog_summary', 'arguments': {}, 'result': self.summary}]

    def test_hundred_ge_movies_rejected(self):
        self.assertFalse(self.loop.grounded('我查到有 100 个电影。', self.obs))

    def test_hundred_tiao_movies_rejected(self):
        self.assertFalse(self.loop.grounded('我查到有 100 条电影。', self.obs))

    def test_reversed_order_with_quantifier_rejected(self):
        self.assertFalse(self.loop.grounded('电影有 100 个。', self.obs))

    def test_real_count_with_variant_quantifier_accepted(self):
        self.assertTrue(self.loop.grounded('本地有 1 个电影。', self.obs))

    def test_full_loop_keeps_real_movie_count(self):
        result = self.run_loop([
            '{"action":"tool","tool":"catalog_summary","arguments":{}}',
            '{"action":"answer","text":"我查到有100个电影。"}',
        ], '本地有多少电影')
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')
        self.assertNotIn('100', result['reply'])


class AttributeTests(_Base):
    """Problem 3: year / type / genre / runtime / playback need real evidence."""

    def setUp(self):
        super().setUp()
        self.summary = self.tools.summary()
        self.obs_summary = [{'tool': 'catalog_summary', 'arguments': {},
                             'result': self.summary}]
        self.items = self.tools.browse('any', 10)['items']
        self.obs_list = [{'tool': 'catalog_list', 'arguments': {'kind': 'any'},
                          'result': self.tools.browse('any', 10)}]
        self.by_title = {i['title']: i for i in self.items}

    def test_fabricated_year_with_real_title_is_rejected(self):
        self.assertIn('星际穿越', self.by_title)
        self.assertFalse(self.loop.grounded('《星际穿越》拍摄于 2099 年。', self.obs_list))

    def test_real_year_with_real_title_is_accepted(self):
        year = self.by_title['星际穿越']['year']
        self.assertEqual(year, 2014)
        self.assertTrue(self.loop.grounded('《星际穿越》是 2014 年的电影。', self.obs_list))

    def test_year_without_metadata_is_rejected(self):
        self.assertFalse(self.loop.grounded('电影拍摄于 2099 年。', self.obs_summary))

    def test_unknown_kind_cannot_be_called_movie(self):
        # A work whose kind could not be determined must be stated as unknown,
        # never promoted to a movie/series by the model.
        listing = {'kind': 'any', 'total': 1, 'items': [
            {'id': 'u1', 'title': '无名合集', 'kind': 'unknown',
             'year': None, 'genres': [], 'episodes': 0, 'seasons': []}]}
        obs = [{'tool': 'catalog_list', 'arguments': {}, 'result': listing}]
        self.assertFalse(self.loop.grounded('《无名合集》是一部电影。', obs))
        self.assertFalse(self.loop.grounded('《无名合集》是一部剧集。', obs))
        self.assertTrue(self.loop.grounded('《无名合集》的类型未知。', obs))

    def test_genre_without_metadata_is_rejected(self):
        # summary only: no genre evidence at all.
        self.assertFalse(self.loop.grounded('这是一部科幻片。', self.obs_summary))

    def test_genre_absence_may_be_stated(self):
        self.assertTrue(self.loop.grounded('这些作品没有类型标签，无法判断题材。', self.obs_summary))

    def test_runtime_without_metadata_is_rejected(self):
        self.assertFalse(self.loop.grounded('这部电影时长 120 分钟。', self.obs_summary))

    def test_playback_state_without_observation_is_rejected(self):
        self.assertFalse(self.loop.grounded('正在播放《三体》第 2 集。', self.obs_list))

    def test_recommend_reason_fabrication_is_rejected(self):
        self.assertFalse(self.loop.grounded('推荐它是因为豆瓣评分很高。', self.obs_summary))


class LegitimateDialogueTests(_Base):
    """Must not become 'reject everything': real recommendations and ordinals stay."""

    builder = staticmethod(build_series_only)

    def setUp(self):
        super().setUp()
        self.recommend = self.tools.recommend(limit=3)
        self.obs = [{'tool': 'catalog_recommend', 'arguments': {'limit': 3},
                     'result': self.recommend}]
        self.titles = [i['title'] for i in self.recommend['items']]

    def test_legal_three_works_recommendation_passes(self):
        text = '从本地已入库内容里给你挑了这三部作品：' \
            + '、'.join(f'《{t}》' for t in self.titles) + '。'
        self.assertTrue(self.loop.grounded(text, self.obs))

    def test_legal_ordinal_selection_still_passes(self):
        self.assertTrue(self.loop.grounded('可以选第三部吗？', self.obs))
        self.assertTrue(self.loop.grounded('那就第二部吧。', self.obs))

    def test_real_progress_answer_still_passes(self):
        progress = {'source': 'device_status', 'available': True,
                    'evidence': 'device_observation', 'title': '三体', 'season': 1,
                    'episode': 2, 'state': 'PLAYING', 'playing': True,
                    'updated_at': None}
        obs = [{'tool': 'catalog_get_playback_progress', 'arguments': {},
                'result': progress}]
        self.assertTrue(self.loop.grounded('你正在观看《三体》第1季第2集。', obs))


if __name__ == '__main__':
    unittest.main()
