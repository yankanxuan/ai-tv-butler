"""V4.4 phase 1+2: catalog overview / recent / detail tools and their grounding.

The loop may only *say* what a tool observation proves. V4.4 adds three
read-only catalog tools and a short-drama kind, so the fact ledger gains
``short_dramas``, observed synopses (``plot_text``) and recency evidence.

Every test here uses an in-memory catalog stub that mirrors the documented
``LibraryTools`` contract (docs/V44_CONTRACT.md), so these assertions do not
depend on the media scanner and cannot silently pass on a different dataset.
"""
import unittest

from adbtv.assistant_loop import AssistantLoop, TOOL_SCHEMAS, ACTION_SCHEMA, ToolError


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.enabled = True
        self.model = 'fake-model'

    def chat(self, messages, system=None, timeout=None):
        return self.replies.pop(0)


def _item(item_id, title, kind='series', episodes=12, **extra):
    row = {'id': item_id, 'title': title, 'kind': kind, 'year': 2026,
           'genres': [], 'episodes': episodes, 'seasons': [1],
           'runtime_minutes': None, 'updated_at': 0.0, 'season': None,
           'episode': None, 'region': None, 'plot': None, 'studio': None,
           'kind_label': {'movie': '电影', 'series': '电视剧',
                          'short_drama': '短剧'}.get(kind, '未知'),
           'added_days_ago': None}
    row.update(extra)
    return row


SHORT_DRAMAS = [
    _item('sd1', '宝星行', 'short_drama', 60,
          plot='大四穷学生张通求职受挫返乡，意外在老宅地下发现连通异星宝地的神秘通道。',
          studio='红果短剧', added_days_ago=2.0),
    _item('sd2', '异界争雄录', 'short_drama', 80,
          plot='少年觉醒血脉之力，在异界大陆一路争雄。',
          studio='红果短剧', added_days_ago=9.0),
]
SERIES = [
    _item('s1', '师兄太稳健', 'series', 40, added_days_ago=1.0),
    _item('s2', '我不是大师', 'series', 36, added_days_ago=20.0),
]


class FakeCatalog:
    """Documented LibraryTools contract over a fixed, explicit dataset."""

    def __init__(self, items=None, available=True):
        self.items = list(SHORT_DRAMAS + SERIES if items is None else items)
        self.available = available

    # ---- counters ----
    def summary(self):
        kinds = {'movie': 0, 'series': 0, 'short_drama': 0, 'unknown': 0}
        for item in self.items:
            kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
        with_meta = sum(1 for i in self.items
                        if i.get('plot') or i.get('genres') or i.get('studio') or i.get('year'))
        return {'available': self.available, 'total': len(self.items),
                'movies': kinds['movie'], 'series': kinds['series'],
                'short_dramas': kinds['short_drama'], 'unknown_kind': kinds['unknown'],
                'episodes': sum(i['episodes'] for i in self.items),
                'seasons': sum(len(i['seasons']) for i in self.items),
                'with_metadata': with_meta,
                'metadata_missing': len(self.items) - with_meta,
                'ambiguous_folders': 0, 'ambiguous_videos': 0,
                'roots': ['/media'], 'index_age_seconds': 60.0, 'errors': [],
                'recent_added_7d': sum(1 for i in self.items
                                       if (i.get('added_days_ago') or 999) <= 7),
                'recent_added_30d': sum(1 for i in self.items
                                        if (i.get('added_days_ago') or 999) <= 30)}

    def overview(self):
        return dict(self.summary())

    def status(self):
        data = self.summary()
        data.update(state='ok' if self.available else 'unavailable',
                    source='local_catalog', read_only=True)
        return data

    # ---- lists ----
    def _match(self, item, kind):
        return kind in (None, 'any') or item['kind'] == kind

    def browse(self, kind='any', limit=10, sort='title'):
        rows = [i for i in self.items if self._match(i, kind)]
        if sort == 'recent':
            rows.sort(key=lambda i: i.get('added_days_ago') or 999)
        else:
            rows.sort(key=lambda i: i['title'])
        return {'kind': kind, 'sort': sort, 'total': len(rows), 'items': rows[:limit]}

    def search(self, query=None, genre=None, year=None, kind='any', limit=10):
        rows = [i for i in self.items if self._match(i, kind)]
        if genre:
            rows = [i for i in rows if genre in (i.get('genres') or [])]
        if query:
            rows = [i for i in rows if query in i['title']]
        return {'query': query, 'genre': genre, 'year': year, 'kind': kind,
                'matched_by': 'title', 'total': len(rows), 'items': rows[:limit]}

    def recommend(self, preferences=None, exclusions=None, limit=5, kind='any',
                  max_episodes=None, exclude_genres=None, include_seen=False):
        rows = [i for i in self.items if self._match(i, kind)]
        if exclusions:
            rows = [i for i in rows if i['id'] not in exclusions]
        if max_episodes:
            rows = [i for i in rows if i['episodes'] <= max_episodes]
        return {'kind': kind, 'total': len(rows), 'items': rows[:limit]}

    def recent(self, limit=5, kind='any'):
        rows = sorted((i for i in self.items if self._match(i, kind)),
                      key=lambda i: i.get('added_days_ago') or 999)
        return {'kind': kind, 'total': len(rows), 'items': rows[:limit]}

    def item(self, item_id):
        for item in self.items:
            if item['id'] == item_id:
                return item
        return None

    def episodes(self, item_id, season=None):
        item = self.item(item_id)
        if item is None:
            raise ValueError('unknown catalog item')
        return {'item': item, 'seasons': item['seasons'],
                'episodes': [{'season': 1, 'episode_count': item['episodes']}]}

    def find(self, title):
        for item in self.items:
            if item['title'] == title:
                return item
        return None


def make_loop(tools=None, replies=None):
    tools = tools or FakeCatalog()
    llm = FakeLLM(replies) if replies else None
    return AssistantLoop({}, tools, llm), tools


class ToolSurfaceTests(unittest.TestCase):
    def test_new_tools_are_advertised(self):
        for name in ('catalog_overview', 'catalog_recent', 'catalog_detail'):
            self.assertIn(name, TOOL_SCHEMAS)
            self.assertIn(name, ACTION_SCHEMA['properties']['tool']['enum'])

    def test_kind_enum_includes_short_drama(self):
        enum = TOOL_SCHEMAS['catalog_recent']['parameters']['properties']['kind']['enum']
        self.assertIn('short_drama', enum)
        self.assertIn('short_drama',
                      TOOL_SCHEMAS['library_recommend']['parameters']['properties']['kind']['enum'])

    def test_detail_requires_item_id(self):
        params = TOOL_SCHEMAS['catalog_detail']['parameters']
        self.assertEqual(params['required'], ['item_id'])

    def test_unknown_tool_is_rejected(self):
        loop, _ = make_loop()
        with self.assertRaises(ToolError):
            loop._run_tool('catalog_delete_everything', {})


class OverviewTests(unittest.TestCase):
    def test_overview_counts_short_dramas_separately(self):
        loop, _ = make_loop()
        data, _ms = loop._run_tool('catalog_overview', {})
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['series'], 2)
        self.assertEqual(data['short_dramas'], 2)
        self.assertEqual(data['total'],
                         data['movies'] + data['series'] + data['short_dramas']
                         + data['unknown_kind'])

    def test_resource_question_answers_from_real_counts(self):
        loop, _ = make_loop()
        result = loop.run('现在资源库有什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['answer_source'], 'local_catalog')
        self.assertIn('4 部作品', result['reply'])
        self.assertIn('短剧 2 部', result['reply'])
        self.assertIn('电影 0 部', result['reply'])
        # a real item is listed, and nothing outside the catalog is claimed
        self.assertIn('宝星行', result['reply'])
        self.assertNotIn('5 部', result['reply'])

    def test_unavailable_mount_is_reported_honestly(self):
        loop, _ = make_loop(FakeCatalog(available=False))
        result = loop.run('现在资源库有什么？')
        self.assertIn('读不到', result['reply'])

    def test_empty_library_is_not_reported_as_broken(self):
        loop, _ = make_loop(FakeCatalog(items=[]))
        result = loop.run('现在资源库有什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('0 部作品', result['reply'])


class RecentTests(unittest.TestCase):
    def test_recent_uses_new_tool_and_reports_days(self):
        loop, _ = make_loop()
        data, _ms = loop._run_tool('catalog_recent', {'limit': 3})
        self.assertEqual([i['title'] for i in data['items']][:2],
                         ['师兄太稳健', '宝星行'])
        result = loop.run('最近新增了哪些资源？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('师兄太稳健', result['reply'])
        self.assertIn('宝星行', result['reply'])


class DetailAndSynopsisTests(unittest.TestCase):
    def test_second_candidate_question_resolves_to_that_candidate(self):
        loop, tools = make_loop()
        state = {'candidates': [{'id': 'sd1', 'title': '宝星行'},
                                {'id': 'sd2', 'title': '异界争雄录'}]}
        result = loop.run('第二个讲什么？', state=state)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn('异界争雄录', result['reply'])
        self.assertIn('少年觉醒血脉之力', result['reply'])
        self.assertNotIn('张通', result['reply'])
        self.assertEqual(result['tool_trace'][0]['tool'], 'catalog_detail')

    def test_detail_without_plot_states_absence_instead_of_inventing(self):
        loop, tools = make_loop()
        result = loop.run('第一个讲什么？',
                          state={'candidates': [{'id': 's1', 'title': '师兄太稳健'}]})
        self.assertIn('师兄太稳健', result['reply'])
        self.assertIn('没有可用的剧情简介', result['reply'])

    def test_out_of_range_ordinal_does_not_guess(self):
        loop, _ = make_loop()
        result = loop.run('第五个讲什么？',
                          state={'candidates': [{'id': 'sd1', 'title': '宝星行'}]})
        self.assertNotEqual(result['status'], 'SUCCESS')
        self.assertNotIn('宝星行', result['reply'])


class ShortDramaGroundingTests(unittest.TestCase):
    """The ledger must bind "短剧 N 部" to short_dramas, not to series."""

    def setUp(self):
        self.loop, self.tools = make_loop()
        self.overview, _ = self.loop._run_tool('catalog_overview', {})
        self.obs = [{'tool': 'catalog_overview', 'arguments': {},
                     'result': self.overview}]

    def test_real_short_drama_count_is_accepted(self):
        self.assertTrue(self.loop.grounded('本地有 2 部短剧。', self.obs))

    def test_wrong_short_drama_count_is_rejected(self):
        self.assertFalse(self.loop.grounded('本地有 3 部短剧。', self.obs))

    def test_series_count_cannot_be_reused_for_short_dramas(self):
        # series is 2 as well; the field binding still has to be explicit
        self.assertFalse(self.loop.grounded('本地有 5 部短剧。', self.obs))
        self.assertTrue(self.loop.grounded('本地有 2 部剧集。', self.obs))

    def test_short_drama_never_counts_as_series(self):
        data = dict(self.overview)
        data['short_dramas'] = 3
        data['series'] = 0
        obs = [{'tool': 'catalog_overview', 'arguments': {}, 'result': data}]
        self.assertFalse(self.loop.grounded('本地有 3 部剧集。', obs))
        self.assertTrue(self.loop.grounded('本地有 3 部短剧。', obs))


class SynopsisGroundingTests(unittest.TestCase):
    """A synopsis is tool data: quoting it is fine, inventing one is not."""

    def setUp(self):
        self.loop, _ = make_loop()
        self.detail, _ = self.loop._run_tool('catalog_detail', {'item_id': 'sd1'})
        self.obs = [{'tool': 'catalog_detail', 'arguments': {'item_id': 'sd1'},
                     'result': self.detail}]

    def test_paraphrase_of_observed_synopsis_is_accepted(self):
        text = ('《宝星行》讲的是大四穷学生张通求职受挫返乡，'
                '意外在老宅地下发现连通异星宝地的神秘通道。')
        self.assertTrue(self.loop.grounded(text, self.obs))

    def test_genre_word_inside_synopsis_is_grounded(self):
        detail = dict(self.detail)
        detail['plot'] = '这是一部悬疑向的作品，主角追查旧案。'
        obs = [{'tool': 'catalog_detail', 'arguments': {'item_id': 'sd1'},
                'result': detail}]
        self.assertTrue(self.loop.grounded('《宝星行》是悬疑剧，主角追查旧案。', obs))

    def test_genre_word_without_evidence_is_rejected(self):
        self.assertFalse(self.loop.grounded('《宝星行》是悬疑剧。', self.obs))

    def test_invented_synopsis_without_observation_is_rejected(self):
        self.assertFalse(self.loop.grounded('《宝星行》讲的是太空歌剧与星际战争。', []))

    def test_unsupported_attributes_are_still_rejected(self):
        self.assertFalse(self.loop.grounded('《宝星行》豆瓣评分 8.9。', self.obs))


class ModelDrivenTests(unittest.TestCase):
    def test_model_can_chain_two_read_only_tools(self):
        loop, _ = make_loop(replies=[
            '{"action":"tool","tool":"catalog_overview","arguments":{}}',
            '{"action":"tool","tool":"catalog_recent","arguments":{"limit":2}}',
            '{"action":"answer","text":"本地共有 4 部作品，最近新增的是《师兄太稳健》。"}',
        ])
        result = loop.run('资源库现在都有什么，最近又加了哪些？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(len(result['tool_trace']), 2)
        self.assertEqual(result['diagnostics']['steps'], 3)
        self.assertEqual(result['answer_source'], 'local_catalog')

    def test_model_answer_with_unproven_count_falls_back_to_tools(self):
        loop, _ = make_loop(replies=[
            '{"action":"tool","tool":"catalog_overview","arguments":{}}',
            '{"action":"answer","text":"本地共有 99 部作品。"}',
        ])
        result = loop.run('资源库有多少？')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['diagnostics']['decision_source'], 'deterministic')
        self.assertEqual(result['diagnostics']['fallback_reason'], 'ungrounded_answer')
        self.assertNotIn('99', result['reply'])

    def test_model_plot_invention_is_rejected_and_replaced(self):
        loop, _ = make_loop(replies=[
            '{"action":"tool","tool":"catalog_detail","arguments":{"item_id":"sd1"}}',
            '{"action":"answer","text":"《宝星行》讲的是外星舰队入侵地球。"}',
        ])
        result = loop.run('第一个讲什么？',
                          state={'candidates': [{'id': 'sd1', 'title': '宝星行'}]})
        self.assertNotIn('外星舰队', result['reply'])
        self.assertIn('宝星行', result['reply'])


if __name__ == '__main__':
    unittest.main()
