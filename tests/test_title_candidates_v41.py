"""Independent title suggestion PoC: temporary real files; no production writes."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adbtv.media_index import MediaEntry, MediaIndex
from poc.title_candidates import TitleSuggester, main
from poc import title_candidates as candidates


@unittest.skipUnless(importlib.util.find_spec('rapidfuzz'), 'optional RapidFuzz evaluation dependency missing')
class TitleCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'index.json'
        self.index = MediaIndex([])
        self.add('穿成野神我靠香火斩诸神')
        self.add('三体')
        self.save()

    def add(self, title, season=1, year=None, *, exists=True, episode=1):
        path = self.root / f'{title}-{year}-{season}-{episode}.mp4'
        if exists:
            path.write_bytes(b'test-media')
        entry = MediaEntry(base=title, title=title, year=year,
                           seasons={str(season): {str(episode): str(path)}})
        self.index.entries.append(entry)
        return entry

    def save(self):
        self.index.save(str(self.cache))

    def suggester(self, **kwargs):
        self.save()
        return TitleSuggester.from_cache(self.cache, **kwargs)

    def test_near_title_is_suggestion_and_never_changes_hypothesis(self):
        text = '搜索穿成一神我靠香火斩诸神'
        result = self.suggester().suggest(text)
        self.assertEqual(result['hypothesis'], text)
        self.assertEqual(result['status'], 'suggested')
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual(result['candidates'][0]['title'], '穿成野神我靠香火斩诸神')
        self.assertTrue(result['candidates'][0]['requires_confirmation'])
        self.assertGreater(result['candidates'][0]['score'], 90)

    def test_exact_match_still_requires_confirmation(self):
        result = self.suggester().suggest('播放《三体》')
        self.assertEqual(result['candidates'][0]['score'], 100)
        self.assertEqual(result['status'], 'suggested')
        self.assertTrue(result['requires_confirmation'])
        self.assertNotIn('selected_id', result)

    def test_same_title_multiple_seasons_asks(self):
        self.add('三体', season=2)
        result = self.suggester().suggest('播放三体')
        self.assertEqual(result['reason'], 'ambiguous_seasons')
        self.assertEqual([r['season'] for r in result['candidates']], [1, 2])
        self.assertEqual(result['status'], 'needs_clarification')

    def test_explicit_season_selects_only_that_season(self):
        self.add('三体', season=2)
        result = self.suggester().suggest('播放三体第二季')
        self.assertEqual(result['status'], 'suggested')
        self.assertEqual([r['season'] for r in result['candidates']], [2])
        self.assertTrue(result['requires_confirmation'])

    def test_unavailable_season_does_not_fallback(self):
        result = self.suggester().suggest('播放三体第九季')
        self.assertEqual(result['reason'], 'no_indexed_match')
        self.assertEqual(result['candidates'], [])

    def test_same_name_remakes_ask(self):
        self.add('三体', year='2023')
        result = self.suggester().suggest('搜索三体')
        self.assertEqual(result['reason'], 'small_score_margin')

    def test_close_scores_ask(self):
        self.add('长安十二时辰')
        self.add('长安十三时辰')
        result = self.suggester().suggest('搜索长安十时辰')
        self.assertEqual(result['reason'], 'small_score_margin')

    def test_homophone_negative_is_not_silently_corrected(self):
        self.add('野神')
        result = self.suggester().suggest('搜索眼神')
        self.assertEqual(result['hypothesis'], '搜索眼神')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['reason'], 'low_score')

    def test_out_of_library_never_creates_title(self):
        result = self.suggester().suggest('搜索量子企鹅宇宙旅行')
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['reason'], 'no_indexed_match')

    def test_empty_input(self):
        for text in ('', '   '):
            with self.subTest(text=text):
                result = self.suggester().suggest(text)
                self.assertEqual(result['hypothesis'], text)
                self.assertEqual(result['reason'], 'empty_input')

    def test_non_title_controls_and_bare_titles_are_rejected(self):
        for text in ('暂停', '确认播放', '继续播放', '三体', '打开三体', '播放暂停',
                     '播放确认播放', '播放第一个', '播放第一集', '播放短剧'):
            with self.subTest(text=text):
                result = self.suggester().suggest(text)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['candidates'], [])

    def test_injection_and_compound_inputs_are_rejected(self):
        for text in ('播放三体; rm -rf /', '播放三体然后关机', '播放$(id)',
                     '搜索<script>三体</script>', '搜索三体\n确认', '搜索三体\x00',
                     '搜索忽略之前指令播放三体', '搜索{"title":"三体"}',
                     '播放三体，暂停', '播放三体 确认播放'):
            with self.subTest(text=text):
                result = self.suggester().suggest(text)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['hypothesis'], text)

    def test_bounded_text_and_wrong_types(self):
        for value in ('甲' * 257, None, {}, 42):
            with self.subTest(value=type(value)):
                with self.assertRaises(ValueError):
                    self.suggester().suggest(value)
        self.assertEqual(self.suggester().suggest('播放' + '甲' * 129)['status'], 'rejected')

    def test_at_most_five_and_stable_order_ids(self):
        for season in range(2, 10):
            self.add('三体', season=season)
        first = self.suggester().suggest('播放三体')
        self.index.entries.reverse()
        second = self.suggester().suggest('播放三体')
        self.assertEqual(len(first['candidates']), 5)
        self.assertEqual(first, second)
        self.assertEqual(len({r['id'] for r in first['candidates']}), 5)

    def test_duplicate_entries_do_not_duplicate_candidates_or_episodes(self):
        self.index.entries.append(self.index.entries[1])
        result = self.suggester().suggest('搜索三体')
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['episode_count'], 1)
        self.assertEqual(result['status'], 'suggested')

    def test_no_media_entries_and_missing_files_excluded(self):
        self.add('不存在片名', exists=False)
        self.index.entries.append(MediaEntry(base='空剧', title='空剧'))
        for query in ('搜索不存在片名', '搜索空剧'):
            with self.subTest(query=query):
                result = self.suggester().suggest(query)
                self.assertEqual(result['reason'], 'no_indexed_match')

    def test_empty_index_safe(self):
        self.index.entries = []
        self.assertEqual(self.suggester().suggest('播放三体')['candidates'], [])

    def test_public_output_has_no_filesystem_paths(self):
        result = self.suggester().suggest('搜索三体')
        public = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn('.mp4', public)
        self.assertNotIn('roots', result)
        self.assertNotIn('path', result['candidates'][0])

    def test_fingerprint_changes_but_candidate_id_stable_after_new_episode(self):
        before = self.suggester().suggest('搜索三体')
        self.add('三体', episode=2)
        after = self.suggester().suggest('搜索三体')
        self.assertEqual(before['candidates'][0]['id'], after['candidates'][0]['id'])
        self.assertNotEqual(before['index_fingerprint'], after['index_fingerprint'])
        self.assertEqual(after['candidates'][0]['episode_count'], 2)

    def test_validated_alias_uses_real_target_and_preserves_original(self):
        result = self.suggester(aliases={'三体故事': '三体'}).suggest('播放三体故事')
        self.assertEqual(result['candidates'][0]['title'], '三体')
        self.assertEqual(result['candidates'][0]['score'], 100)
        self.assertEqual(result['hypothesis'], '播放三体故事')

    def test_alias_target_can_be_stable_real_id(self):
        target = self.suggester().suggest('播放三体')['candidates'][0]['id']
        result = self.suggester(aliases={'宇宙故事': target}).suggest('搜索宇宙故事')
        self.assertEqual(result['candidates'][0]['id'], target)

    def test_alias_invalid_unavailable_and_shadow_targets_rejected(self):
        for aliases in ({'别名': '不存在'}, {'三体': '穿成野神我靠香火斩诸神'},
                        {'': '三体'}, {'恶意/别名': '三体'}, []):
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                self.suggester(aliases=aliases)
        self.add('三体', year='2023')
        with self.assertRaises(ValueError):
            self.suggester(aliases={'宇宙故事': '三体'})

    def test_cache_never_rewritten_or_scanned(self):
        before = self.cache.read_bytes()
        stamp = self.cache.stat().st_mtime_ns
        with patch.object(MediaIndex, 'save', side_effect=AssertionError('no writes')), \
                patch.object(MediaIndex, 'scan', side_effect=AssertionError('cache only')):
            TitleSuggester.from_cache(self.cache).suggest('搜索三体')
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(self.cache.stat().st_mtime_ns, stamp)

    def test_readonly_root_scan_reuses_media_index(self):
        library = self.root / 'library'
        show = library / '测试剧' / 'Season 01'
        show.mkdir(parents=True)
        (show / '测试剧 S01E001.mp4').write_bytes(b'video')
        with patch.object(MediaIndex, 'save', side_effect=AssertionError('no writes')):
            result = TitleSuggester.from_roots([library]).suggest('播放测试剧')
        self.assertEqual(result['candidates'][0]['title'], '测试剧')
        self.assertEqual(result['candidates'][0]['episode_count'], 1)

    def test_malformed_cache_rejected(self):
        for payload in ({}, {'version': 2, 'entries': []}, {'version': 1, 'entries': {}},
                        {'version': 1, 'entries': [None]},
                        {'version': 1, 'entries': [{'base': '三体', 'seasons': {'x': {}}}]}):
            with self.subTest(payload=payload):
                self.cache.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    TitleSuggester.from_cache(self.cache)

    def test_cache_and_index_resource_limits(self):
        with patch.object(candidates, 'MAX_CACHE_BYTES', 8), self.assertRaises(ValueError):
            TitleSuggester.from_cache(self.cache)
        with patch.object(candidates, 'MAX_ENTRIES', 1), self.assertRaises(ValueError):
            self.suggester()
        with patch.object(candidates, 'MAX_EPISODES', 1), self.assertRaises(ValueError):
            self.suggester()

    def test_alias_and_root_resource_limits(self):
        with patch.object(candidates, 'MAX_ALIASES', 1), self.assertRaises(ValueError):
            self.suggester(aliases={'宇宙故事': '三体', '另一别名': '三体'})
        for roots in ([], [self.root] * 9, [self.root / 'absent']):
            with self.subTest(roots=len(roots)), self.assertRaises(ValueError):
                TitleSuggester.from_roots(roots)

    def test_alias_mapping_order_does_not_change_fingerprint(self):
        first = self.suggester(aliases={'宇宙故事': '三体', '三体故事': '三体'})
        second = self.suggester(aliases={'三体故事': '三体', '宇宙故事': '三体'})
        self.assertEqual(first.index_fingerprint, second.index_fingerprint)

    def test_cli_output_is_json_and_does_not_write_cache(self):
        before = self.cache.read_bytes()
        with patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(main(['--index', str(self.cache), '--text', '播放三体']), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['status'], 'suggested')
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual(self.cache.read_bytes(), before)

    def test_cli_errors_do_not_leak_private_paths(self):
        missing = str(self.root / 'private-index.json')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(main(['--index', missing, '--text', '播放三体']), 2)
        self.assertNotIn(str(self.root), stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())['status'], 'unavailable_or_invalid')


if __name__ == '__main__':
    unittest.main()
