"""Work-level title resolution: grouping, season/episode, expiry, confirmation."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from adbtv.media_index import MediaEntry, MediaIndex
from poc.title_session import TitleSessionStore


def entry(base, year=None, season_hint=None, seasons=None, title=None):
    return MediaEntry(base=base, title=title or base, seasons=seasons or {}, year=year,
                      season_hint=season_hint)


class TitleSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = [1000.0]
        self.index = MediaIndex([])
        self.add('穿成野神我靠香火斩诸神', year='2026', season_hint=1, episodes=range(1, 5))
        self.add('穿成野神我靠香火斩诸神', year='2026', season_hint=2, episodes=range(1, 4))
        self.add('暗河谜案', year='2025', season_hint=1, episodes=range(1, 3))
        self.add('量子企鹅', year='2024', season_hint=1, episodes=range(1, 2))
        self.store = TitleSessionStore(self.index, now=lambda: self.clock[0])

    def add(self, base, *, year=None, season_hint=None, episodes=(), season_key=1):
        folder = self.root / f'{base}-{year or "none"}-{season_hint or 0}'
        folder.mkdir(parents=True, exist_ok=True)
        mapping = {}
        for episode in episodes:
            path = folder / f'{base} S{season_hint or 1:02d}E{episode:03d}.mp4'
            path.write_bytes(b'video')
            mapping[str(episode)] = str(path)
        self.index.entries.append(entry(base, year, season_hint, {str(season_key): mapping}))

    def suggest(self, text, current=None):
        return self.store.suggest('s1', text, current=current)

    def test_work_level_grouping_does_not_let_seasons_fill_candidates(self):
        result = self.suggest('搜索穿成野神我靠香火斩诸神')
        self.assertEqual(result['status'], 'suggested')
        self.assertEqual(len(result['works']), 1)
        self.assertEqual(result['works'][0]['season_count'], 2)
        self.assertEqual(result['works'][0]['episode_count'], 7)
        self.assertTrue(result['works'][0]['requires_confirmation'])
        self.assertNotIn('path', json.dumps(result))

    def test_approximate_title_finds_real_work(self):
        result = self.suggest('搜索穿成一神我靠香火斩诸神')
        self.assertEqual(result['works'][0]['title'], '穿成野神我靠香火斩诸神')
        self.assertGreaterEqual(result['works'][0]['score'], 70)
        self.assertEqual(result['hypothesis'], '搜索穿成一神我靠香火斩诸神')

    def test_seasons_and_episodes_are_secondary_selection(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        seasons = self.store.seasons('s1', work_id)
        self.assertEqual([s['season'] for s in seasons['seasons']], [1, 2])
        episodes = self.store.episodes('s1', work_id, 2)
        self.assertEqual(episodes['episodes'], [1, 2, 3])
        with self.assertRaisesRegex(ValueError, 'season does not exist'):
            self.store.episodes('s1', work_id, 99)

    def test_select_confirm_and_one_time_confirmation(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        selected = self.store.select('s1', work_id, 2, 3)
        self.assertEqual(selected['status'], 'needs_confirmation')
        self.assertEqual(selected['command'], '播放《穿成野神我靠香火斩诸神》第2季第3集')
        with self.assertRaisesRegex(ValueError, 'confirm:true'):
            self.store.confirm('s1', confirm='true')
        confirmed = self.store.confirm('s1', confirm=True)
        self.assertEqual(confirmed['status'], 'confirmed')
        with self.assertRaisesRegex(ValueError, 'already confirmed'):
            self.store.confirm('s1', confirm=True)
        self.store.cancel('s1')
        with self.assertRaisesRegex(ValueError, 'unknown session'):
            self.store.confirm('s1', confirm=True)

    def test_cancel_invalidates_pending_selection(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        self.store.select('s1', work_id, 1, 1)
        self.assertTrue(self.store.cancel('s1')['had_pending'])
        with self.assertRaisesRegex(ValueError, 'unknown session'):
            self.store.select('s1', work_id, 1, 1)

    def test_candidate_expiry_blocks_every_later_action(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        self.clock[0] += 601
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.store.seasons('s1', work_id)
        self.store.suggest('s1', '搜索穿成野神我靠香火斩诸神')
        self.clock[0] += 601
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.store.select('s1', work_id, 1, 1)

    def test_selection_must_belong_to_snapshot_and_exist(self):
        other = TitleSessionStore(self.index, now=lambda: self.clock[0])
        other_work = other.suggest('other', '搜索暗河谜案')['works'][0]['work_id']
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        with self.assertRaisesRegex(ValueError, 'not part'):
            self.store.select('s1', other_work, 1, 1)
        with self.assertRaisesRegex(ValueError, 'episode does not exist'):
            self.store.select('s1', work_id, 1, 99)

    def test_control_and_compound_inputs_are_rejected(self):
        for text in ('暂停播放', '确认', '先搜索然后播放', '搜索穿成野神; rm -rf /', '下一个'):
            with self.subTest(text=text):
                result = self.suggest(text)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['works'], [])

    def test_empty_query_is_clarification(self):
        result = self.suggest('搜索   ')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['reason'], 'empty_input')

    def test_library_miss_has_no_candidates(self):
        result = self.suggest('搜索完全不存在的量子企鹅宇宙旅行')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['reason'], 'no_indexed_match')
        self.assertEqual(result['works'], [])

    def test_season_hint_prevents_folder_season_collisions(self):
        result = self.suggest('搜索穿成野神我靠香火斩诸神')
        seasons = result['works'][0]['seasons']
        self.assertEqual([s['season'] for s in seasons], [1, 2])
        self.assertEqual([s['episode_count'] for s in seasons], [4, 3])

    def test_remakes_require_clarification(self):
        self.add('同名剧', year='2020', season_hint=1, episodes=[1])
        self.add('同名剧', year='2024', season_hint=1, episodes=[1])
        self.store = TitleSessionStore(self.index, now=lambda: self.clock[0])
        result = self.suggest('搜索同名剧')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['reason'], 'ambiguous_remakes')
        self.assertEqual(len(result['works']), 2)

    def test_small_margin_requires_clarification(self):
        self.index.entries.clear()
        for title in ('穿成野神我靠香火斩诸神', '穿成野神我靠香火斩诸神二'):
            self.add(title, year='2026', season_hint=1, episodes=[1])
        self.store = TitleSessionStore(self.index, now=lambda: self.clock[0])
        result = self.suggest('搜索穿成野神我靠香火斩诸神')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['reason'], 'small_score_margin')

    def test_low_score_requires_clarification(self):
        result = self.suggest('搜索穿成完全不同的剧名')
        self.assertIn(result['reason'], ('low_score', 'no_indexed_match'))
        self.assertEqual(result['status'], 'needs_clarification')

    def test_current_playback_context_boosts_matching_work(self):
        self.index.entries.clear()
        for title in ('穿成野神我靠香火斩诸神', '穿成野神外传'):
            self.add(title, year='2026', season_hint=1, episodes=[1])
        self.store = TitleSessionStore(self.index, now=lambda: self.clock[0])
        without = self.suggest('搜索穿成野神')
        with_context = self.suggest('搜索穿成野神', current={'title': '穿成野神外传', 'season': 1, 'episode': 1})
        self.assertEqual(with_context['works'][0]['title'], '穿成野神外传')
        self.assertGreaterEqual(with_context['works'][0]['score'], without['works'][0]['score'])

    def test_reviewed_aliases_are_validated_and_reported(self):
        store = TitleSessionStore(self.index, aliases={'野神': '穿成野神我靠香火斩诸神'},
                                  now=lambda: self.clock[0])
        result = store.suggest('s1', '搜索野神')
        self.assertEqual(result['works'][0]['title'], '穿成野神我靠香火斩诸神')
        self.assertEqual(result['works'][0]['alias_used'], '野神')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            TitleSessionStore(self.index, aliases={'坏别名': '不存在作品'})
        with self.assertRaisesRegex(ValueError, 'shadows'):
            TitleSessionStore(self.index, aliases={'暗河谜案': '穿成野神我靠香火斩诸神'})

    def test_alias_does_not_change_original_hypothesis(self):
        store = TitleSessionStore(self.index, aliases={'野神': '穿成野神我靠香火斩诸神'},
                                  now=lambda: self.clock[0])
        result = store.suggest('s1', '搜索野神', hypothesis='搜索野神原识别')
        self.assertEqual(result['hypothesis'], '搜索野神原识别')

    def test_candidate_output_has_no_media_paths_or_agent_calls(self):
        result = self.suggest('搜索穿成野神我靠香火斩诸神')
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('/media', serialized)
        self.assertNotIn('task_id', serialized)
        self.assertNotIn('execute', serialized)

    def test_index_fingerprint_changes_when_alias_or_file_changes(self):
        before = self.store.index_fingerprint
        changed = TitleSessionStore(self.index, aliases={'野神': '穿成野神我靠香火斩诸神'},
                                    now=lambda: self.clock[0])
        self.assertNotEqual(before, changed.index_fingerprint)
        path = Path(self.index.entries[0].seasons['1']['1'])
        path.write_bytes(b'changed-video')
        refreshed = TitleSessionStore(self.index, now=lambda: self.clock[0])
        self.assertNotEqual(before, refreshed.index_fingerprint)

    def test_cache_path_map_is_read_only_and_maps_container_prefix(self):
        cache = self.root / 'media-index.json'
        self.index.save(str(cache))
        before = cache.read_bytes(), cache.stat().st_mtime_ns
        store = TitleSessionStore.from_cache(cache, path_map={'/media': str(self.root)})
        mapped = store.suggest('s1', '搜索穿成野神我靠香火斩诸神')
        self.assertEqual(mapped['status'], 'suggested')
        self.assertEqual((cache.read_bytes(), cache.stat().st_mtime_ns), before)

    def test_cache_rejects_oversized_or_wrong_version(self):
        cache = self.root / 'bad.json'
        cache.write_text(json.dumps({'version': 2, 'entries': []}))
        with self.assertRaisesRegex(ValueError, 'invalid MediaIndex cache'):
            TitleSessionStore.from_cache(cache)
        cache.write_bytes(b'x' * (16 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            TitleSessionStore.from_cache(cache)

    def test_cleanup_removes_expired_sessions(self):
        self.suggest('搜索穿成野神我靠香火斩诸神')
        self.clock[0] += 601
        self.store.cleanup()
        with self.assertRaisesRegex(ValueError, 'unknown session'):
            self.store.seasons('s1', 'work-any')


    def test_confirm_rechecks_file_exists_size_and_mtime(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        self.store.select('s1', work_id, 1, 1)
        media = Path(self.index.entries[0].seasons['1']['1'])
        media.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.store.confirm('s1', confirm=True)
        with self.assertRaisesRegex(ValueError, 'select a work'):
            self.store.confirm('s1', confirm=True)

        media.write_bytes(b'video')
        self.store.select('s1', work_id, 1, 1)
        media.write_bytes(b'changed-video-size')
        with self.assertRaisesRegex(ValueError, 'changed since selection'):
            self.store.confirm('s1', confirm=True)

        media.write_bytes(b'video')
        self.store.select('s1', work_id, 1, 1)
        os.utime(media, ns=(1_000_000_000, 1_000_000_000))
        with self.assertRaisesRegex(ValueError, 'changed since selection'):
            self.store.confirm('s1', confirm=True)

    def test_confirm_requires_current_candidate_snapshot(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        self.store.select('s1', work_id, 1, 1)
        self.suggest('搜索穿成野神我靠香火斩诸神')
        with self.assertRaisesRegex(ValueError, 'select a work'):
            self.store.confirm('s1', confirm=True)

    def test_periodic_cleanup_removes_expired_sessions(self):
        self.suggest('搜索穿成野神我靠香火斩诸神')
        self.clock[0] += 601
        thread = self.store.start_cleanup(interval=0.05)
        try:
            time.sleep(0.25)
            self.assertNotIn('s1', self.store._sessions)
        finally:
            self.store.stop_cleanup()
        self.assertFalse(thread.is_alive())

    def test_selection_does_not_expose_media_path(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        selected = self.store.select('s1', work_id, 1, 1)
        self.assertNotIn('media_path', selected['selection'])
        self.assertNotIn(str(self.root), json.dumps(selected, ensure_ascii=False))

    def test_store_confirm_is_atomic_for_two_threads(self):
        import concurrent.futures
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        self.store.select('s1', work_id, 1, 1)
        def attempt():
            try:
                return ('ok', self.store.confirm('s1', confirm=True)['status'])
            except ValueError as exc:
                return ('error', str(exc))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(attempt) for _ in range(2)]
            outcomes = [result.result(timeout=5) for result in results]
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == 'ok'), 1)
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == 'error'), 1)
        self.assertTrue(any('already confirmed' in value for kind, value in outcomes if kind == 'error'))

    def test_selection_does_not_execute(self):
        work_id = self.suggest('搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
        selected = self.store.select('s1', work_id, 1, 1)
        self.assertFalse(selected.get('executed', False))
        self.assertEqual(selected['command'], '播放《穿成野神我靠香火斩诸神》第1季第1集')


if __name__ == '__main__':
    unittest.main()