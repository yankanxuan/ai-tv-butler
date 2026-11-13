"""Isolated ASR PoC gates and metrics; no user audio or model downloads."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from poc import sherpa_asr as asr


class AsrPocTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def wav(self, rate=16000, channels=1, width=2):
        path = self.root / 'synthetic.wav'
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(channels)
            out.setsampwidth(width)
            out.setframerate(rate)
            out.writeframes(b'\x00' * rate * channels * width)
        return path

    def model(self):
        files = {}
        pins = {}
        for role in ('encoder', 'decoder', 'joiner', 'tokens'):
            path = self.root / role
            path.write_bytes(role.encode())
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            pins[role] = (role, sha, len(role))
            files[role] = {'path': role, 'sha256': sha, 'size_bytes': len(role),
                           'source_url': f'https://huggingface.co/{asr.MODEL_REPO}/resolve/{asr.MODEL_REVISION}/{role}'}
        patcher = patch.object(asr, 'PINNED_FILES', pins)
        patcher.start()
        self.addCleanup(patcher.stop)
        return {'schema_version': 1, 'model': {'repo': asr.MODEL_REPO,
                'revision': asr.MODEL_REVISION, 'license': 'apache-2.0',
                'license_evidence': asr.LICENSE_EVIDENCE,
                'files': files}}

    def test_no_consent_rejected_before_any_file_read(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(asr.ValidationError, 'consent'):
                asr.run('absent-model.json', 'absent-samples.json', consent=False)

    def test_wrong_model_license_and_hash_rejected(self):
        manifest = self.model()
        manifest['model']['license'] = 'unknown'
        with self.assertRaisesRegex(asr.ValidationError, 'license'):
            asr.validate_model(manifest, self.root)
        manifest['model']['license'] = 'apache-2.0'
        manifest['model']['files']['encoder']['sha256'] = '0' * 64
        with self.assertRaisesRegex(asr.ValidationError, 'sha256'):
            asr.validate_model(manifest, self.root)

    def test_correct_declared_hash_still_rejects_corrupt_local_artifact(self):
        manifest = self.model()
        (self.root / 'encoder').write_bytes(b'corrupt')
        with self.assertRaisesRegex(asr.ValidationError, 'sha256'):
            asr.validate_model(manifest, self.root)

    def test_compact_example_is_pinned_and_source_license_is_required(self):
        example, root = asr.load_manifest(Path(__file__).resolve().parents[1] / 'poc' / 'model_manifest.example.json')
        self.assertEqual(example['model']['revision'], asr.CTC_REVISION)
        self.assertEqual(sum(v['size_bytes'] for v in example['model']['files'].values()), 26355706)
        example['model']['license_evidence'] = example['model']['provenance_evidence']
        with self.assertRaisesRegex(asr.ValidationError, 'license_evidence'):
            asr.validate_model(example, root)

    def test_revision_and_license_evidence_must_be_verified_candidate(self):
        manifest = self.model()
        manifest['model']['revision'] = 'main'
        with self.assertRaisesRegex(asr.ValidationError, 'revision'):
            asr.validate_model(manifest, self.root)
        manifest = self.model()
        manifest['model']['license_evidence'] = 'https://example.invalid'
        with self.assertRaisesRegex(asr.ValidationError, 'license_evidence'):
            asr.validate_model(manifest, self.root)

    def test_wav_pcm_mono_16k(self):
        samples, duration = asr.read_wav(self.wav())
        self.assertEqual(len(samples), 16000)
        self.assertEqual(samples.typecode, 'f')
        self.assertEqual(duration, 1.0)
        for args in ({'rate': 8000}, {'channels': 2}, {'width': 1}):
            with self.subTest(args=args), self.assertRaises(asr.ValidationError):
                asr.read_wav(self.wav(**args))

    def test_chinese_cer_uses_characters_with_explicit_normalization(self):
        self.assertEqual(asr.cer('你好， 世界！', '你好世界')['cer'], 0)
        self.assertEqual(asr.cer('你好世界', '你号世界')['cer'], 0.25)
        self.assertEqual(asr.cer('你好', '你')['deletions'], 1)
        self.assertEqual(asr.cer('你', '你好')['insertions'], 1)
        self.assertEqual(asr.cer('你好', '你号')['substitutions'], 1)
        with self.assertRaisesRegex(asr.ValidationError, 'reference'):
            asr.cer('，', '')

    def test_samples_need_authorization_origin_license_reference_hash(self):
        path = self.wav()
        sample = {'id': 'synthetic', 'path': path.name, 'origin': 'test-generated silence',
                  'license': 'CC0-1.0', 'authorized': True, 'reference': '你好',
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.assertEqual(len(asr.validate_samples({'schema_version': 1, 'samples': [sample]}, self.root)), 1)
        for field, value in [('authorized', False), ('origin', ''), ('license', 'unknown'),
                             ('reference', ''), ('sha256', '0' * 64)]:
            with self.subTest(field=field), self.assertRaises(asr.ValidationError):
                asr.validate_samples({'schema_version': 1, 'samples': [{**sample, field: value}]}, self.root)

    def test_all_sample_metadata_is_checked_before_audio_is_read(self):
        valid = {'id': 'one', 'path': 'never-open.wav', 'origin': 'temporary synthetic unit test',
                 'license': 'CC0-1.0', 'authorized': True, 'reference': '你好', 'sha256': '0' * 64}
        unauthorized = {**valid, 'id': 'two', 'authorized': False}
        with patch.object(asr, '_hash_file', side_effect=AssertionError('no audio reads')):
            with self.assertRaisesRegex(asr.ValidationError, 'authorized'):
                asr.validate_samples({'schema_version': 1, 'samples': [valid, unauthorized]}, self.root)

    def test_permission_license_requires_actual_evidence_field(self):
        sample = {'id': 'one', 'path': 'never-open.wav', 'origin': 'synthetic',
                  'license': 'LicenseRef-Explicit-Permission', 'authorized': True,
                  'reference': '你好', 'sha256': '0' * 64}
        with self.assertRaisesRegex(asr.ValidationError, 'permission_evidence'):
            asr.validate_samples({'schema_version': 1, 'samples': [sample]}, self.root)

    def test_pcm_normalization_and_nonempty_frames(self):
        path = self.root / 'levels.wav'
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b'\x00\x80\x00\x00\xff\x7f')
        data, duration = asr.read_wav(path)
        self.assertEqual(list(data), [-1.0, 0.0, 32767 / 32768])
        self.assertEqual(duration, 3 / 16000)
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
        with self.assertRaisesRegex(asr.ValidationError, 'audio frames'):
            asr.read_wav(path)

    def test_measurements_are_labeled_and_cold_init_is_separate(self):
        result = asr.measurements(2, 0.5, 0.3, 1024)
        self.assertEqual(result['audio_duration_seconds'], 2)
        self.assertEqual(result['wall_inference_seconds'], 0.5)
        self.assertEqual(result['rtf'], 0.25)
        self.assertEqual(result['process_time_cpu_seconds'], 0.3)
        self.assertEqual(result['max_rss_bytes'], 1024)
        self.assertEqual(result['max_rss_scope'], 'peak_process_lifetime_not_delta')
        self.assertNotIn('cold_init_wall_seconds', result)


if __name__ == '__main__':
    unittest.main()
