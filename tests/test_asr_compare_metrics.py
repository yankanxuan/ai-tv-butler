"""CER correctness; generated strings only, no audio or downloaded models."""
import importlib.util
import itertools
import unittest

from poc import asr_compare as compare
from poc import sherpa_asr


@unittest.skipUnless(importlib.util.find_spec('jiwer'), 'isolated evaluation dependency: poc/requirements-eval.txt')
class CharacterMetricsTests(unittest.TestCase):
    def assert_counts(self, ref, hyp, counts):
        result = compare.character_metrics(ref, hyp)
        self.assertEqual(tuple(result[k] for k in ('substitutions', 'deletions', 'insertions')), counts)
        self.assertEqual(result['errors'], sum(counts))
        self.assertEqual(len(result['edits']), sum(counts))
        self.assertEqual(result['cer'], sum(counts) / result['characters'])
        return result

    def test_substitution(self):
        result = self.assert_counts('暂停播放', '代停播放', (1, 0, 0))
        self.assertEqual(result['edits'], [['暂', '代']])

    def test_insertion(self):
        result = self.assert_counts('打开飞牛', '请打开飞牛吧', (0, 0, 2))
        self.assertEqual(result['edits'], [[None, '请'], [None, '吧']])

    def test_deletion(self):
        result = self.assert_counts('请打开飞牛吧', '打开飞牛', (0, 2, 0))
        self.assertEqual(result['edits'], [['请', None], ['吧', None]])

    def test_repeated_characters_use_minimum_distance(self):
        # SequenceMatcher aligns the repeated middle and counts 4 instead of 2.
        self.assert_counts('ababab', 'aaaaab', (2, 0, 0))
        self.assert_counts('哈哈哈哈', '哈哈哈', (0, 1, 0))
        self.assert_counts('哈哈哈', '哈哈哈哈哈', (0, 0, 2))

    def test_long_repetition_does_not_use_autojunk(self):
        result = compare.character_metrics('甲' * 220 + '乙', '丙' + '甲' * 220 + '乙')
        self.assertEqual((result['errors'], result['insertions']), (1, 1))

    def test_normalization_is_shared_and_case_is_preserved(self):
        self.assert_counts('Ａ， 飞 牛！', 'A飞牛', (0, 0, 0))
        self.assert_counts('Ａ', 'a', (1, 0, 0))
        self.assertTrue(compare.contains_reference('飞 牛！', '请打开飞牛。'))

    def test_empty_hypothesis_and_cer_above_one(self):
        self.assert_counts('飞牛', '', (0, 2, 0))
        self.assert_counts('牛', '牛牛牛牛', (0, 0, 3))

    def test_empty_normalized_reference_rejected(self):
        for ref in ('', ' ，！\n'):
            with self.subTest(ref=ref), self.assertRaises(sherpa_asr.ValidationError):
                compare.cer(ref, '飞牛')

    def test_exhaustive_short_strings_match_independent_distance(self):
        # Compare against the existing DP oracle, including duplicate characters.
        refs = [''.join(p) for n in range(1, 5) for p in itertools.product('甲乙', repeat=n)]
        hyps = ['', *refs]
        for ref, hyp in itertools.product(refs, hyps):
            result = compare.character_metrics(ref, hyp)
            self.assertEqual(result['errors'], sherpa_asr.cer(ref, hyp)['errors'], (ref, hyp))
            self.assertEqual(len(result['edits']), result['errors'])

    def test_complete_edits_are_not_truncated(self):
        result = compare.character_metrics('飞牛', '甲' * 20 + '飞牛')
        self.assertEqual((result['errors'], len(result['edits'])), (20, 20))

    def test_corpus_uses_integer_edit_totals_and_weighted_cer(self):
        rows = []
        for ref, hyp in [('甲', '乙'), ('甲' * 9, '甲' * 9)]:
            rows.append({**compare.character_metrics(ref, hyp), 'audio_seconds': 2,
                         'wall_seconds': .5, 'proper_nouns': {'甲': False}, 'command_recalled': False})
        result = compare.aggregate(rows)
        self.assertEqual(result['cer'], .1)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['substitutions'], 1)
        self.assertEqual(result['rtf'], .25)
        self.assertEqual(result['proper_noun_total'], 2)

    def test_empty_condition_is_unmeasured(self):
        self.assertIsNone(compare.aggregate([])['cer'])
        self.assertIsNone(compare.aggregate([])['rtf'])


if __name__ == '__main__':
    unittest.main()
