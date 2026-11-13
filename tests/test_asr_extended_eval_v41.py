"""Phase 2 metrics: separate intent, title, false-suggestion and latency denominators."""
import json
from pathlib import Path
import tempfile
import unittest

from adbtv.media_index import MediaEntry, MediaIndex
from poc.asr_extended_eval import _aggregate, evaluate
from poc.title_session import TitleSessionStore


def report(profile, rows):
    return {'status': 'offline_evaluation_completed', 'profile': profile,
            'model': {'repo': 'test'}, 'samples': rows}


class ExtendedMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        path = self.root / '剧.mp4'; path.write_bytes(b'video')
        index = MediaIndex([])
        index.entries.append(MediaEntry(base='穿成野神我靠香火斩诸神', title='穿成野神我靠香火斩诸神',
                                        seasons={'1': {'1': str(path)}}, year='2026', season_hint=1))
        self.store = TitleSessionStore(index)
        samples = {'samples': [
            {'id': 't1', 'condition': 'clean', 'expected_intent': 'search',
             'expected_title': '穿成野神我靠香火斩诸神', 'kind': 'title', 'context_required': False},
            {'id': 'c1', 'condition': 'clean', 'expected_intent': 'pause',
             'expected_title': None, 'kind': 'control', 'context_required': False},
            {'id': 'x1', 'condition': 'noise', 'expected_intent': 'select',
             'expected_title': None, 'kind': 'control', 'context_required': True},
        ], 'speakers': 1, 'conditions': ['clean', 'noise']}
        self.samples = self.root / 'samples.json'; self.samples.write_text(json.dumps(samples))
        self.rows = [
            {'id': 't1', 'condition': 'clean', 'hypothesis': '搜索穿成一神我靠香火斩诸神',
             'characters': 13, 'cer': 1/13, 'audio_seconds': 2, 'wall_seconds': .5, 'first_partial_seconds': .3},
            {'id': 'c1', 'condition': 'clean', 'hypothesis': '暂停播放',
             'characters': 4, 'cer': 0, 'audio_seconds': 1, 'wall_seconds': .4, 'first_partial_seconds': .2},
            {'id': 'x1', 'condition': 'noise', 'hypothesis': '选择第一个结果',
             'characters': 7, 'cer': 0, 'audio_seconds': 1, 'wall_seconds': .6, 'first_partial_seconds': .4},
        ]
        self.report = self.root / 'report.json'; self.report.write_text(json.dumps(report('ctc', self.rows)))

    def test_metrics_use_explicit_denominators(self):
        result = evaluate([self.report], self.samples, self.store, config_path='config.example.yaml')
        model = result['models'][0]; overall = model['overall']
        self.assertEqual(overall['samples'], 3)
        self.assertEqual(overall['intent_evaluated'], 2)
        self.assertEqual(overall['context_required'], 1)
        self.assertEqual(overall['title_samples'], 1)
        self.assertEqual(overall['title_top1'], 1.0)
        self.assertEqual(overall['title_top3'], 1.0)
        self.assertEqual(overall['false_suggestion_rate'], 0.0)
        self.assertEqual(overall['control_samples'], 2)
        self.assertEqual(overall['latency']['p50_wall_seconds'], .5)
        self.assertEqual(overall['latency']['p95_first_partial_seconds'], .4)

    def test_context_required_is_not_counted_as_intent_success_or_failure(self):
        result = evaluate([self.report], self.samples, self.store, config_path='config.example.yaml')
        row = next(r for r in result['models'][0]['samples'] if r['id'] == 'x1')
        self.assertIsNone(row['intent_correct'])
        self.assertTrue(row['context_required'])

    def test_library_miss_is_clarification_not_false_control_suggestion(self):
        self.rows[1]['hypothesis'] = '搜索完全不存在的片名'
        self.report.write_text(json.dumps(report('ctc', self.rows)))
        result = evaluate([self.report], self.samples, self.store, config_path='config.example.yaml')
        row = next(r for r in result['models'][0]['samples'] if r['id'] == 'c1')
        self.assertFalse(row['false_suggestion'])
        self.assertEqual(row['title_status'], 'needs_clarification')

    def test_reports_must_share_samples_and_be_complete(self):
        other = self.root / 'other.json'
        other.write_text(json.dumps(report('paraformer', self.rows[:2])))
        with self.assertRaisesRegex(ValueError, 'same samples'):
            evaluate([self.report, other], self.samples, self.store, config_path='config.example.yaml')
        bad = self.root / 'bad.json'
        bad.write_text(json.dumps({'status': 'paired_comparison_incomplete', 'samples': self.rows}))
        with self.assertRaisesRegex(ValueError, 'not a completed'):
            evaluate([bad], self.samples, self.store, config_path='config.example.yaml')

    def test_empty_denominators_are_none_not_zero(self):
        result = evaluate([self.report], self.samples, self.store, config_path='config.example.yaml')
        subset = [r for r in result['models'][0]['samples'] if r['id'] == 'x1']
        aggregate = _aggregate(subset)
        self.assertIsNone(aggregate['title_top1'])
        self.assertIsNone(aggregate['intent_accuracy'])

    def test_report_preserves_limitations_and_capture_kind(self):
        result = evaluate([self.report], self.samples, self.store, config_path='config.example.yaml')
        self.assertEqual(result['capture_kind'], 'offline_file_replay')
        self.assertFalse(result['realtime_microphone_tested'])
        self.assertEqual(result['corpus']['declared_speakers'], 1)
        self.assertTrue(any('microphone' in item for item in result['limitations']))


if __name__ == '__main__':
    unittest.main()