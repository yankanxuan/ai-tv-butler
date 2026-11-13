"""V4.4 media-index: real NFO metadata, evidence-only kinds, path-free views.

Every case runs against a temporary tree with hand-written NFO samples; no
network, no real /media mount, no production state. The fixtures mirror the
field mix found on the production NAS (``plot``/``tag``/``studio``/``uniqueid``
present, ``genre``/``runtime`` mostly absent), so the assertions describe real
behaviour rather than a convenient ideal.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from adbtv.library_tools import KINDS, LibraryTools
from adbtv.media_index import MediaEntry, MediaIndex, classify_kind, empty_evidence, parse_nfo

LONG_PLOT = '这是一个用于测试截断的超长简介。' * 40  # 480 chars


def write_nfo(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')


def episode_files(directory, title, count, season=1, suffix='.mp4'):
    directory.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        (directory / f'{title} S{season:02d}E{number:03d}{suffix}').write_bytes(b'v')


def build_tree(root):
    """14 works: 4 movies, 6 series, 4 short dramas, 0 unknown."""
    # short drama #1: plot + studio 短剧 + hongguo uniqueid + tag, under /短剧/
    show = root / '短剧' / '爆款短剧 (2025)'
    episode_files(show / 'Season 01', '爆款短剧', 3)
    write_nfo(show / 'tvshow.nfo',
              '<tvshow><plot>落魄少年逆袭。</plot><studio>红果短剧</studio>'
              '<tag>都市</tag><uniqueid type="hongguo">7151</uniqueid>'
              '<year>2025</year></tvshow>')
    # short drama #2: only <studio> proves it (no 短剧 path segment)
    show = root / '剧集' / '短剧工作室剧 (2024)'
    episode_files(show / 'Season 01', '短剧工作室剧', 1, suffix='.mkv')
    write_nfo(show / 'tvshow.nfo',
              '<tvshow><studio>某某短剧工作室</studio><genre>悬疑</genre></tvshow>')
    # short drama #3: only <uniqueid type="hongguo"> proves it
    show = root / '剧集' / '红果授权剧 (2024)'
    episode_files(show / 'Season 01', '红果授权剧', 1, suffix='.mkv')
    write_nfo(show / 'tvshow.nfo',
              '<tvshow><uniqueid type="hongguo">9001</uniqueid><genre>古装</genre></tvshow>')
    # short drama #4: only the /短剧/ path proves it, no NFO at all
    episode_files(root / '短剧' / '无NFO短剧 (2023)' / 'Season 01', '无NFO短剧', 1, suffix='.mkv')
    # movie #1: movie.nfo with real metadata
    movie = root / '电影' / '单片电影 (2019)'
    movie.mkdir(parents=True, exist_ok=True)
    (movie / '单片电影.mp4').write_bytes(b'v')
    write_nfo(movie / 'movie.nfo',
              '<movie><plot>一个男人的十年。</plot><studio>某影业</studio>'
              '<genre>科幻</genre><year>2019</year></movie>')
    # movie #2: <type>movie</type> inside tvshow.nfo, only a year as metadata
    movie = root / '电影' / '类型电影 (2018)'
    movie.mkdir(parents=True, exist_ok=True)
    (movie / '类型电影.mkv').write_bytes(b'v')
    write_nfo(movie / 'tvshow.nfo', '<tvshow><type>movie</type><year>2018</year></tvshow>')
    # movie #3: single unmarked file, no NFO
    movie = root / '电影' / '无NFO电影 (2017)'
    movie.mkdir(parents=True, exist_ok=True)
    (movie / '无NFO电影.mkv').write_bytes(b'v')
    # movie #4: 480-char plot, must be projected truncated
    movie = root / '电影' / '长简介电影 (2014)'
    movie.mkdir(parents=True, exist_ok=True)
    (movie / '长简介电影.mp4').write_bytes(b'v')
    write_nfo(movie / 'movie.nfo', f'<movie><plot>{LONG_PLOT}</plot></movie>')
    # series #1: real episode structure, genre only as <tag>
    show = root / '剧集' / '长篇剧 (2023)'
    episode_files(show / 'Season 01', '长篇剧', 5, suffix='.mkv')
    write_nfo(show / 'tvshow.nfo', '<tvshow><tag>悬疑</tag></tvshow>')
    # series #2: two seasons, no NFO
    show = root / '剧集' / '无NFO多季剧 (2022)'
    episode_files(show / 'Season 01', '无NFO多季剧', 1, season=1, suffix='.mkv')
    episode_files(show / 'Season 02', '无NFO多季剧', 1, season=2, suffix='.mkv')
    # series #3: malformed NFO must not break the scan
    show = root / '剧集' / '坏XML (2021)'
    episode_files(show / 'Season 01', '坏XML', 1, suffix='.mkv')
    write_nfo(show / 'tvshow.nfo', '<tvshow><plot>没有闭合的标签')
    # series #4: oversized NFO is ignored, never parsed
    show = root / '剧集' / '超大NFO (2020)'
    episode_files(show / 'Season 01', '超大NFO', 1, suffix='.mkv')
    write_nfo(show / 'tvshow.nfo',
              '<tvshow><plot>' + 'x' * (1024 * 1024 + 64) + '</plot></tvshow>')
    # series #5: year is the only metadata field
    show = root / '剧集' / '只有年份 (2016)'
    episode_files(show / 'Season 01', '只有年份', 1, suffix='.mkv')
    write_nfo(show / 'tvshow.nfo', '<tvshow><year>2016</year></tvshow>')
    # series #6: nothing at all
    episode_files(root / '剧集' / '无元数据 (2015)' / 'Season 01', '无元数据', 1, suffix='.mkv')
    return root


def tools_for(root, **kwargs):
    cfg = {'media': {'enabled': True, 'roots': [str(root)]}}
    return LibraryTools(cfg, index=MediaIndex([str(root)]).scan(), **kwargs)


class Fixture:
    """Temp tree + one LibraryTools built over it."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_tree(self.root)
        self.tools = tools_for(self.root)

    def items(self):
        return {item['title']: item for item in self.tools.browse('any', 20)['items']}

    def cleanup(self):
        self._tmp.cleanup()


class KindDetectionTests(unittest.TestCase):
    """Five priority levels, each proven by its own evidence only."""

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.items = self.fixture.items()

    def test_movie_from_movie_nfo(self):
        self.assertEqual(self.items['单片电影']['kind'], 'movie')
        self.assertEqual(self.items['单片电影']['kind_label'], '电影')

    def test_movie_from_declared_type(self):
        self.assertEqual(self.items['类型电影']['kind'], 'movie')
        self.assertEqual(self.items['类型电影']['kind_label'], '电影')

    def test_short_drama_from_studio_without_short_path(self):
        # Path says 剧集; only <studio> mentions 短剧.
        self.assertEqual(self.items['短剧工作室剧']['kind'], 'short_drama')
        self.assertEqual(self.items['短剧工作室剧']['kind_label'], '短剧')

    def test_short_drama_from_hongguo_uniqueid(self):
        self.assertEqual(self.items['红果授权剧']['kind'], 'short_drama')

    def test_short_drama_from_path_segment(self):
        self.assertEqual(self.items['无NFO短剧']['kind'], 'short_drama')

    def test_series_from_real_episode_structure(self):
        self.assertEqual(self.items['长篇剧']['kind'], 'series')
        self.assertEqual(self.items['长篇剧']['kind_label'], '电视剧')
        self.assertEqual(self.items['无NFO多季剧']['kind'], 'series')

    def test_movie_from_single_unmarked_file_without_nfo(self):
        self.assertEqual(self.items['无NFO电影']['kind'], 'movie')

    def test_unknown_when_no_evidence_at_all(self):
        entry = MediaEntry(base='无名合集', title='无名合集')
        two_files = ['/media/其他/无名合集/a.mp4', '/media/其他/无名合集/b.mp4']
        self.assertEqual(classify_kind(entry, two_files, empty_evidence()), 'unknown')
        # ... and the single-file / marked branches are reachable from the same call
        self.assertEqual(classify_kind(entry, two_files[:1], empty_evidence()), 'movie')
        marked = ['/media/其他/无名合集/合集 S01E001.mp4']
        self.assertEqual(classify_kind(entry, marked, empty_evidence()), 'series')
        self.assertIn('unknown', KINDS)

    def test_title_never_decides_the_kind(self):
        # "短剧" in the *title* is not evidence: the folder path is 电影 and the
        # NFO declares a movie.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / '电影' / '短剧之王 (2013)'
            movie.mkdir(parents=True)
            (movie / '短剧之王.mp4').write_bytes(b'v')
            write_nfo(movie / 'movie.nfo', '<movie><plot>喜剧。</plot></movie>')
            item = tools_for(root).browse('any', 10)['items'][0]
            self.assertEqual(item['kind'], 'movie')


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.tools = self.fixture.tools
        self.items = self.fixture.items()

    def test_plot_studio_and_genres_come_from_real_fields(self):
        item = self.items['爆款短剧']
        self.assertEqual(item['plot'], '落魄少年逆袭。')
        self.assertEqual(item['studio'], '红果短剧')
        self.assertEqual(item['genres'], ['都市'])  # <tag> counts as a genre
        self.assertEqual(item['year'], 2025)

    def test_genre_and_tag_are_merged_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / '剧集' / '合并标签 (2024)'
            episode_files(show / 'Season 01', '合并标签', 1)
            write_nfo(show / 'tvshow.nfo',
                      '<tvshow><genre>悬疑</genre><genre>都市</genre>'
                      '<tag>悬疑</tag><tag>甜宠,都市</tag></tvshow>')
            item = tools_for(root).browse('any', 10)['items'][0]
            self.assertEqual(item['genres'], ['悬疑', '都市', '甜宠'])

    def test_plot_is_truncated_to_300_characters(self):
        item = self.items['长简介电影']
        self.assertIsNotNone(item['plot'])
        self.assertEqual(len(item['plot']), 300)
        self.assertTrue(LONG_PLOT.startswith(item['plot']))

    def test_broken_and_oversized_nfo_are_ignored_not_raised(self):
        broken = self.items['坏XML']
        oversized = self.items['超大NFO']
        self.assertEqual(broken['kind'], 'series')  # structure still wins
        self.assertIsNone(broken['plot'])
        self.assertEqual(broken['genres'], [])
        self.assertIsNone(oversized['plot'])
        self.assertEqual(oversized['genres'], [])
        self.assertIsNone(parse_nfo(self.fixture.root / '剧集' / '坏XML (2021)' / 'tvshow.nfo'))
        self.assertIsNone(parse_nfo(self.fixture.root / '剧集' / '超大NFO (2020)' / 'tvshow.nfo'))

    def test_metadata_missing_is_reported_not_invented(self):
        bare = self.items['无元数据']
        self.assertIsNone(bare['plot'])
        self.assertIsNone(bare['studio'])
        self.assertEqual(bare['genres'], [])
        self.assertIsNone(bare['runtime_minutes'])

    def test_with_metadata_new_definition(self):
        summary = self.tools.summary()
        # plot / genres / studio / NFO year each count on their own
        self.assertIsNotNone(self.items['单片电影']['plot'])      # plot
        self.assertEqual(self.items['长篇剧']['genres'], ['悬疑'])  # tag only
        self.assertEqual(self.items['短剧工作室剧']['studio'], '某某短剧工作室')
        self.assertEqual(self.items['只有年份']['year'], 2016)     # year only
        self.assertEqual(summary['with_metadata'], 8)
        self.assertEqual(summary['metadata_missing'], 6)
        self.assertEqual(summary['with_plot'], 3)

    def test_no_nfo_still_reports_title_year_and_kind(self):
        bare = self.items['无NFO多季剧']
        self.assertEqual(bare['title'], '无NFO多季剧')
        self.assertEqual(bare['year'], 2022)  # from the folder name, structure only
        self.assertEqual(bare['kind'], 'series')
        self.assertEqual(bare['episodes'], 2)
        self.assertEqual(bare['seasons'], [1, 2])

    def test_runtime_is_passed_through_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / '电影' / '有片长 (2012)'
            movie.mkdir(parents=True)
            (movie / '有片长.mp4').write_bytes(b'v')
            write_nfo(movie / 'movie.nfo', '<movie><runtime>128 min</runtime></movie>')
            item = tools_for(root).browse('any', 10)['items'][0]
            self.assertEqual(item['runtime_minutes'], '128 min')


class SummaryAndOverviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.tools = self.fixture.tools

    def test_kind_counts_and_invariant(self):
        summary = self.tools.summary()
        self.assertEqual(summary['total'], 14)
        self.assertEqual(summary['movies'], 4)
        self.assertEqual(summary['series'], 6)
        self.assertEqual(summary['short_dramas'], 4)
        self.assertEqual(summary['unknown_kind'], 0)
        self.assertEqual(summary['episodes'], 21)
        self.assertEqual(summary['seasons'], 15)
        self.assertEqual(summary['total'],
                         summary['movies'] + summary['series']
                         + summary['short_dramas'] + summary['unknown_kind'])

    def test_overview_keys_and_numeric_consistency(self):
        overview = self.tools.overview()
        expected = {'state', 'source', 'read_only', 'available', 'total', 'movies',
                    'series', 'short_dramas', 'unknown_kind', 'episodes', 'seasons',
                    'with_metadata', 'metadata_missing', 'with_plot',
                    'recent_added_7d', 'recent_added_30d', 'index_age_seconds',
                    'roots_available', 'recent', 'errors'}
        self.assertTrue(expected.issubset(overview))
        summary = self.tools.summary()
        for key in ('total', 'movies', 'series', 'short_dramas', 'unknown_kind',
                    'episodes', 'seasons', 'with_metadata', 'metadata_missing', 'with_plot'):
            self.assertEqual(overview[key], summary[key], key)
        self.assertEqual(overview['state'], 'ok')
        self.assertEqual(overview['total'],
                         overview['movies'] + overview['series']
                         + overview['short_dramas'] + overview['unknown_kind'])
        self.assertEqual(len(overview['recent']), 5)
        self.assertTrue(overview['roots_available'] >= 1)

    def test_status_keeps_old_keys_and_adds_short_dramas(self):
        status = self.tools.status()
        for key in ('state', 'available', 'total', 'movies', 'series', 'unknown_kind',
                    'episodes', 'metadata_missing', 'ambiguous_folders', 'roots', 'errors'):
            self.assertIn(key, status)
        self.assertEqual(status['short_dramas'], 4)

    def test_empty_library_is_empty_not_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = tools_for(Path(tmp))
            summary = tools.summary()
            self.assertEqual(summary['total'], 0)
            self.assertEqual(summary['available'], True)
            self.assertEqual(tools.status()['state'], 'empty')
            self.assertEqual(tools.overview()['total'], 0)


class RecentTests(unittest.TestCase):
    def test_recent_order_and_added_days_ago(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = time.time()
            ages = {'最旧作品': 40, '中间作品': 12, '最新作品': 2}
            for title, days in ages.items():
                movie = root / '电影' / title
                movie.mkdir(parents=True)
                video = movie / f'{title}.mp4'
                video.write_bytes(b'v')
                stamp = now - days * 86400
                os.utime(video, (stamp, stamp))
            tools = tools_for(root)
            rows = tools.recent(limit=5)['items']
            self.assertEqual([row['title'] for row in rows],
                             ['最新作品', '中间作品', '最旧作品'])
            for row in rows:
                self.assertAlmostEqual(row['added_days_ago'], ages[row['title']], delta=0.1)
            self.assertGreater(rows[0]['updated_at'], rows[-1]['updated_at'])
            summary = tools.summary()
            self.assertEqual(summary['recent_added_7d'], 1)
            self.assertEqual(summary['recent_added_30d'], 2)

    def test_browse_recent_carries_added_days_ago(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / '电影' / '新片 (2026)'
            movie.mkdir(parents=True)
            (movie / '新片.mp4').write_bytes(b'v')
            tools = tools_for(root)
            rows = tools.browse('any', 5, sort='recent')['items']
            self.assertEqual(rows[0]['added_days_ago'], 0.0)
            self.assertIsNotNone(rows[0]['added_days_ago'])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.tools = self.fixture.tools

    def test_genre_filter_matches_tag_and_genre_sources(self):
        titles = {row['title'] for row in self.tools.search(genre='悬疑')['items']}
        self.assertEqual(titles, {'长篇剧', '短剧工作室剧'})  # <tag> and <genre>
        self.assertEqual({row['title'] for row in self.tools.search(genre='都市')['items']},
                         {'爆款短剧'})  # <tag> only

    def test_genre_filter_without_match_returns_empty(self):
        result = self.tools.search(genre='根本不存在的类型')
        self.assertEqual(result['items'], [])
        self.assertEqual(result['total'], 0)

    def test_kind_filter_accepts_short_drama(self):
        result = self.tools.search(kind='short_drama')
        self.assertEqual(result['total'], 4)
        self.assertTrue(all(row['kind'] == 'short_drama' for row in result['items']))

    def test_query_still_matches_titles(self):
        self.assertEqual([row['title'] for row in self.tools.search(query='长篇剧')['items']],
                         ['长篇剧'])


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.tools = self.fixture.tools

    def test_max_episodes_only_returns_qualifying_works(self):
        rows = self.tools.recommend(limit=20, max_episodes=1)['items']
        self.assertTrue(rows)
        self.assertTrue(all(row['episodes'] <= 1 for row in rows))
        titles = {row['title'] for row in rows}
        self.assertNotIn('长篇剧', titles)     # 5 episodes
        self.assertNotIn('爆款短剧', titles)   # 3 episodes
        self.assertNotIn('无NFO多季剧', titles)  # 2 episodes

    def test_exclude_genres_is_honoured(self):
        rows = self.tools.recommend(limit=20, exclude_genres=['悬疑'])['items']
        titles = {row['title'] for row in rows}
        self.assertNotIn('长篇剧', titles)
        self.assertNotIn('短剧工作室剧', titles)
        self.assertIn('爆款短剧', titles)  # 都市, not excluded

    def test_missing_metadata_never_produces_a_genre_reason(self):
        rows = self.tools.recommend(limit=20)['items']
        by_title = {row['title']: row for row in rows}
        bare = by_title['无元数据']
        self.assertEqual(bare['genres'], [])
        self.assertTrue(bare['reasons'])
        for reason in bare['reasons']:
            self.assertFalse(reason.startswith('类型：'), reason)
            self.assertFalse(reason.startswith('出品：'), reason)
        # ... while a work with real metadata does cite it
        rich = by_title['爆款短剧']
        self.assertIn('类型：都市', rich['reasons'])

    def test_reasons_only_cite_observed_fields(self):
        rows = self.tools.recommend(limit=20)['items']
        by_title = {row['title']: row for row in rows}
        row = by_title['长篇剧']
        self.assertIn('类型：悬疑', row['reasons'])
        self.assertIn('共 5 集，集数较短', row['reasons'])
        self.assertIn('7 天内新增', row['reasons'])
        for reason in row['reasons']:
            self.assertNotIn('/', reason)  # no path-shaped reason ever

    def test_include_seen_controls_already_watched_rows(self):
        first = self.tools.recommend(limit=1)['items'][0]
        hidden = self.tools.recommend(limit=20, seen=[first['id']])['items']
        self.assertNotIn(first['id'], [row['id'] for row in hidden])
        shown = self.tools.recommend(limit=20, seen=[first['id']], include_seen=True)['items']
        self.assertIn(first['id'], [row['id'] for row in shown])
        seen_row = next(row for row in shown if row['id'] == first['id'])
        self.assertIn('已看过', seen_row['reasons'])


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.tools = self.fixture.tools

    def test_no_projection_leaks_a_filesystem_path(self):
        # Item projections (V4.4) never carry a path. ``summary()``/``status()``/
        # ``health()`` still expose the configured roots, a V4.3 contract key
        # asserted by tests/test_assistant_v42.py, so they are checked for item
        # rows only.
        item = self.tools.item(self.tools.browse('any', 1)['items'][0]['id'])
        payloads = [self.tools.overview(),
                    self.tools.browse('any', 20)['items'],
                    self.tools.search(query='剧')['items'],
                    self.tools.search(genre='悬疑')['items'],
                    self.tools.recent(limit=20)['items'],
                    self.tools.recommend(limit=20)['items'],
                    item,
                    self.tools.episodes(item['id'])]
        root = str(self.fixture.root)
        for payload in payloads:
            text = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(root, text)
            self.assertNotIn('/media', text)
            self.assertNotIn('.mp4', text)
            self.assertNotIn('.mkv', text)
        # The overview lists recent works but not the mount path.
        self.assertNotIn(root, json.dumps(self.tools.overview(), ensure_ascii=False))

    def test_item_detail_has_the_documented_fields(self):
        detail = self.tools.item(self.tools.browse('any', 20)['items'][0]['id'])
        for key in ('id', 'title', 'year', 'kind', 'kind_label', 'genres', 'studio',
                    'plot', 'region', 'episodes', 'seasons', 'runtime_minutes',
                    'updated_at', 'added_days_ago', 'season', 'episode',
                    'has_metadata', 'episode_count_by_season'):
            self.assertIn(key, detail)
        self.assertIsNone(self.tools.item('f' * 24))


class CacheTests(unittest.TestCase):
    def test_rebuild_reuses_unchanged_nfo_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / '剧集' / '缓存剧 (2024)'
            episode_files(show / 'Season 01', '缓存剧', 1)
            nfo = show / 'tvshow.nfo'
            write_nfo(nfo, '<tvshow><plot>第一版简介。</plot></tvshow>')
            tools = tools_for(root)
            self.assertEqual(tools.browse('any', 5)['items'][0]['plot'], '第一版简介。')
            parses = tools._nfo_cache.parses
            self.assertGreater(parses, 0)
            tools._built_at = -1000  # force the next call to rebuild
            tools.browse('any', 5)
            self.assertEqual(tools._nfo_cache.parses, parses)  # nothing re-read
            # A real change invalidates only that directory.
            write_nfo(nfo, '<tvshow><plot>第二版简介。</plot></tvshow>')
            os.utime(nfo, ns=(time.time_ns(), time.time_ns()))
            tools._built_at = -1000
            self.assertEqual(tools.browse('any', 5)['items'][0]['plot'], '第二版简介。')
            self.assertGreater(tools._nfo_cache.parses, parses)


if __name__ == '__main__':
    unittest.main()
