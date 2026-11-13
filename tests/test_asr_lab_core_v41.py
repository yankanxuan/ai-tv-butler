"""V4.1 laboratory gates with tiny generated artifacts, WAVs and fake decoders.

No model/network downloads, user audio, television, production configuration or
production indexes are accessed. Test reports are temporary synthetic fixtures.
"""
from array import array
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import wave

from poc import asr_lab as lab
from poc import asr_compare_v41 as paired
from poc.sherpa_asr import ValidationError

ACTUAL_REGISTRY = lab.REGISTRY


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LabGatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / 'registry.json'
        self.models = self.root / 'models'
        self.models.mkdir()
        self.evidence = self.root / 'license.txt'
        self.evidence.write_text('Synthetic test evidence, not a real model license.\n')
        files = {}
        for role in ('model', 'tokens'):
            data = ('test-' + role).encode()
            path = self.models / role
            path.write_bytes(data)
            files[role] = {'name': role, 'size_bytes': len(data), 'sha256': sha(data),
                           'url': 'https://example.invalid/immutable/' + role}
        self.spec = {'kind': 'ctc', 'repo': 'synthetic/test', 'revision': 'a' * 40,
                     'license': 'LicenseRef-Synthetic-Fixture', 'files': files,
                     'evidence': [{'path': 'license.txt', 'sha256': sha(self.evidence.read_bytes())}]}
        self.data = {'schema_version': 1, 'profiles': {'test-profile': self.spec},
                     'vad': {'files': {'model': files['model']}, 'evidence': self.spec['evidence']}}
        self.registry.write_text(json.dumps(self.data))
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'schema_version': 1, 'profile': 'test-profile',
                                             'model_dir': 'models', 'vad_dir': 'models'}))
        registry_patch = patch.object(lab, 'REGISTRY', self.registry)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)

    def write_manifest(self, value):
        self.manifest.write_text(json.dumps(value))

    def wav(self, *, seconds=.02, channels=1, width=2, rate=16000, frames=None):
        path = self.root / 'synthetic.wav'
        count = int(seconds * rate) if frames is None else frames
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(channels)
            out.setsampwidth(width)
            out.setframerate(rate)
            out.writeframes(b'\x00' * count * channels * width)
        return path

    def bare_engine(self):
        engine = object.__new__(lab.Engine)
        engine.recognizer = object()
        engine.vad = None
        return engine

    def test_shipped_registry_pins_models_and_local_license_evidence(self):
        data = json.loads(ACTUAL_REGISTRY.read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], 1)
        self.assertTrue({'ctc', 'transducer', 'paraformer'} <= data['profiles'].keys())
        for spec in [*data['profiles'].values(), data['vad']]:
            self.assertTrue(spec['files'])
            self.assertTrue(spec['evidence'])
            for entry in spec['files'].values():
                self.assertRegex(entry['sha256'], r'^[0-9a-f]{64}$')
                self.assertGreater(entry['size_bytes'], 0)
                self.assertTrue(entry['url'].startswith('https://'))
            for evidence in spec['evidence']:
                local = ACTUAL_REGISTRY.parent / evidence['path']
                self.assertTrue(local.is_file())
                self.assertEqual(sha(local.read_bytes()), evidence['sha256'])

    def test_valid_manifest_uses_pinned_registry_artifacts_and_evidence(self):
        manifest, spec, files, vad_file = lab.validate_manifest(self.manifest)
        self.assertEqual(manifest['profile'], 'test-profile')
        self.assertEqual(spec, self.spec)
        self.assertEqual(set(files), {'model', 'tokens'})
        self.assertEqual(files['model'], str(self.models / 'model'))
        self.assertIsNone(vad_file)

    def test_manifest_cannot_supply_its_own_models_or_license_override(self):
        doc = json.loads(self.manifest.read_text())
        doc.update(kind='unreviewed', license='anything', files={'model': 'arbitrary-path'})
        self.write_manifest(doc)
        _, spec, files, _ = lab.validate_manifest(self.manifest)
        self.assertEqual(spec, self.spec)
        self.assertEqual(files['model'], str(self.models / 'model'))

    def test_unknown_profile_rejected_before_any_artifact_use(self):
        self.write_manifest({'schema_version': 1, 'profile': 'unreviewed', 'model_dir': 'models'})
        with patch.object(lab, 'checked_artifacts') as checked:
            with self.assertRaisesRegex(ValidationError, 'Unknown reviewed'):
                lab.validate_manifest(self.manifest)
        checked.assert_not_called()

    def test_manifest_schema_and_missing_model_directory_rejected(self):
        for doc in ([], {}, {'schema_version': 2},
                    {'schema_version': 1, 'profile': 'test-profile'},
                    {'schema_version': 1, 'profile': 'test-profile', 'model_dir': ''}):
            with self.subTest(doc=doc):
                self.write_manifest(doc)
                with self.assertRaises(ValidationError):
                    lab.validate_manifest(self.manifest)

    def test_missing_model_artifact_rejected(self):
        (self.models / 'model').unlink()
        with self.assertRaisesRegex(ValidationError, 'missing or hash/size'):
            lab.validate_manifest(self.manifest)

    def test_corrupt_same_size_and_wrong_size_artifacts_rejected(self):
        for raw in (b'X' * len(b'test-model'), b'too short'):
            with self.subTest(size=len(raw)):
                (self.models / 'model').write_bytes(raw)
                with self.assertRaisesRegex(ValidationError, 'hash/size'):
                    lab.validate_manifest(self.manifest)

    def test_symlink_artifact_is_rejected_even_if_content_matches(self):
        actual = self.root / 'outside-model'
        (self.models / 'model').rename(actual)
        (self.models / 'model').symlink_to(actual)
        with self.assertRaises(ValidationError):
            lab.validate_manifest(self.manifest)

    def test_missing_and_tampered_license_evidence_fail(self):
        self.evidence.unlink()
        with self.assertRaisesRegex(ValidationError, 'license/provenance evidence'):
            lab.validate_manifest(self.manifest)
        self.evidence.write_text('changed')
        with self.assertRaisesRegex(ValidationError, 'license/provenance evidence'):
            lab.validate_manifest(self.manifest)

    def test_vad_uses_its_pinned_file_and_license_evidence(self):
        _, _, _, path = lab.validate_manifest(self.manifest, vad=True)
        self.assertEqual(path, str(self.models / 'model'))
        doc = json.loads(self.manifest.read_text())
        doc.pop('vad_dir')
        self.write_manifest(doc)
        with self.assertRaisesRegex(ValidationError, 'vad_dir'):
            lab.validate_manifest(self.manifest, vad=True)

    def test_runtime_requires_exact_dependency_versions(self):
        with patch.object(lab, 'version', side_effect=lab.DEPENDENCIES.__getitem__):
            lab.check_runtime()
        for dependency in lab.DEPENDENCIES:
            versions = dict(lab.DEPENDENCIES)
            versions[dependency] = '0.0.0'
            with self.subTest(dependency=dependency), \
                    patch.object(lab, 'version', side_effect=versions.__getitem__), \
                    self.assertRaisesRegex(ValidationError, 'must be'):
                lab.check_runtime()

    def test_engine_runtime_rejection_precedes_recognizer_factory(self):
        factory = Mock()
        fake = SimpleNamespace(OnlineRecognizer=SimpleNamespace(from_zipformer2_ctc=factory))
        with patch.dict(sys.modules, {'sherpa_onnx': fake}), \
                patch.object(lab, 'check_runtime', side_effect=ValidationError('wrong runtime')):
            with self.assertRaisesRegex(ValidationError, 'runtime'):
                lab.Engine(self.manifest)
        factory.assert_not_called()

    def test_recognizer_factories_are_fixed_by_registry_kind(self):
        fake = SimpleNamespace(OnlineRecognizer=SimpleNamespace(
            from_zipformer2_ctc=Mock(), from_transducer=Mock(), from_paraformer=Mock()))
        for kind, method in [('ctc', 'from_zipformer2_ctc'), ('transducer', 'from_transducer'),
                             ('paraformer', 'from_paraformer')]:
            with self.subTest(kind=kind):
                for name in ('from_zipformer2_ctc', 'from_transducer', 'from_paraformer'):
                    getattr(fake.OnlineRecognizer, name).reset_mock()
                profile = {**self.spec, 'kind': kind}
                files = {'model': 'fixture-model', 'tokens': 'fixture-tokens'}
                with patch.object(lab, 'validate_manifest', return_value=({}, profile, files, None)), \
                        patch.object(lab, 'check_runtime'), patch.dict(sys.modules, {'sherpa_onnx': fake}):
                    engine = lab.Engine('unused', num_threads=3)
                selected = getattr(fake.OnlineRecognizer, method)
                selected.assert_called_once_with(**files, num_threads=3, sample_rate=16000,
                                                 feature_dim=80, decoding_method='greedy_search', provider='cpu')
                self.assertIs(engine.recognizer, selected.return_value)
                for name in ('from_zipformer2_ctc', 'from_transducer', 'from_paraformer'):
                    if name != method:
                        getattr(fake.OnlineRecognizer, name).assert_not_called()

    def test_unknown_kind_and_invalid_threads_rejected(self):
        for threads in (0, 5, True, 2.0, '2'):
            with self.subTest(threads=threads), patch.object(lab, 'validate_manifest') as check:
                with self.assertRaisesRegex(ValidationError, 'num_threads'):
                    lab.Engine('unused', num_threads=threads)
                check.assert_not_called()
        fake = SimpleNamespace()
        with patch.object(lab, 'validate_manifest', return_value=({}, {'kind': 'unknown'}, {}, None)), \
                patch.object(lab, 'check_runtime'), patch.dict(sys.modules, {'sherpa_onnx': fake}), \
                self.assertRaisesRegex(ValidationError, 'Unsupported reviewed'):
            lab.Engine('unused')

    def test_consent_gate_reads_no_manifests_samples_or_models(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read')), \
                patch.object(lab, 'Engine') as engine, patch.object(lab, 'validate_samples') as samples:
            with self.assertRaisesRegex(ValidationError, 'consent'):
                lab.evaluate('missing-model', 'missing-samples', consent=False)
        engine.assert_not_called()
        samples.assert_not_called()

    def test_non_boolean_consent_never_authorizes_sample_reads(self):
        for consent in ('false', 'true', 1, [], {}, object()):
            with self.subTest(consent_type=type(consent).__name__), \
                    patch.object(Path, 'read_text', side_effect=AssertionError('must not read')), \
                    patch.object(lab, 'Engine') as engine:
                with self.assertRaisesRegex(ValidationError, 'consent'):
                    lab.evaluate('missing-model', 'missing-samples', consent=consent)
                engine.assert_not_called()

    def test_unknown_sample_condition_rejected_before_model_load(self):
        path = self.root / 'samples.json'
        path.write_text('{}')
        with patch.object(lab, 'validate_samples', return_value=[({'condition': 'unknown'}, Path('unused'))]), \
                patch.object(lab, 'Engine') as engine:
            with self.assertRaisesRegex(ValidationError, 'condition'):
                lab.evaluate('unused', path, consent=True)
        engine.assert_not_called()

    def test_invalid_wav_formats_rejected_before_decoder(self):
        engine = self.bare_engine()
        cases = [{'channels': 2}, {'width': 1}, {'rate': 8000}, {'frames': 0}]
        for case in cases:
            with self.subTest(case=case), patch.object(lab, 'recognize') as decoder:
                with self.assertRaises(ValidationError):
                    engine.transcribe(self.wav(**case))
                decoder.assert_not_called()
        path = self.root / 'corrupt.wav'
        path.write_bytes(b'not a WAV')
        with self.assertRaises(ValidationError):
            engine.transcribe(path)

    def test_missing_and_overlong_wav_rejected(self):
        engine = self.bare_engine()
        with patch.object(lab, 'recognize') as decoder:
            with self.assertRaisesRegex(ValidationError, 'missing'):
                engine.transcribe(self.root / 'absent.wav')
            with self.assertRaisesRegex(ValidationError, '60 second'):
                engine.transcribe(self.wav(seconds=60.2))
            # Slightly over 60s is below the raw-byte+4096 guard: duration must reject too.
            with self.assertRaisesRegex(ValidationError, '60 seconds'):
                engine.transcribe(self.wav(frames=16000 * 60 + 1))
            decoder.assert_not_called()

    def test_exact_sixty_seconds_is_accepted(self):
        engine = self.bare_engine()
        with patch.object(lab, 'recognize', return_value=('合成测试结果', .1, .02)):
            result = engine.transcribe(self.wav(seconds=60))
        self.assertEqual(result['audio_seconds'], 60)
        self.assertEqual(result['hypothesis'], '合成测试结果')
        self.assertEqual(result['capture_kind'], 'offline_file_replay')
        self.assertFalse(result['realtime_microphone_tested'])

    def test_truncated_wav_rejected(self):
        path = self.wav()
        path.write_bytes(path.read_bytes()[:-2])
        with self.assertRaisesRegex(ValidationError, 'truncated'):
            self.bare_engine().transcribe(path)

    def test_private_json_permissions_and_exclusive_no_overwrite(self):
        path = self.root / 'private-report.json'
        previous = os.umask(0)
        try:
            lab.private_json(path, {'hypothesis': 'synthetic only'})
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        content = path.read_bytes()
        with self.assertRaises(FileExistsError):
            lab.private_json(path, {'replacement': True})
        self.assertEqual(path.read_bytes(), content)

    def test_private_json_rejects_symlink_destination(self):
        target = self.root / 'target'
        target.write_text('keep')
        path = self.root / 'link.json'
        path.symlink_to(target)
        with self.assertRaises(FileExistsError):
            lab.private_json(path, {'bad': 'overwrite'})
        self.assertEqual(target.read_text(), 'keep')

    def test_existing_valid_artifact_fetch_does_not_access_network(self):
        entry = self.spec['files']['model']
        with patch.object(lab.urllib.request, 'urlopen', side_effect=AssertionError('no network')):
            lab.fetch_artifact(entry, self.models / 'model')

    def test_existing_invalid_artifact_is_never_overwritten(self):
        path = self.models / 'model'
        path.write_bytes(b'keep corrupt file')
        with patch.object(lab.urllib.request, 'urlopen') as network:
            with self.assertRaises(ValidationError):
                lab.fetch_artifact(self.spec['files']['model'], path)
        self.assertEqual(path.read_bytes(), b'keep corrupt file')
        network.assert_not_called()

    def test_fetch_rejects_http_and_oversized_payload_without_valid_output(self):
        entry = dict(self.spec['files']['model'])
        target = self.root / 'download'
        entry['url'] = 'http://example.invalid/model'
        with patch.object(lab.urllib.request, 'urlopen') as network:
            with self.assertRaisesRegex(ValidationError, 'HTTPS'):
                lab.fetch_artifact(entry, target)
            network.assert_not_called()
        entry['url'] = 'https://example.invalid/model'
        response = io.BytesIO(b'X' * (entry['size_bytes'] + 1))
        response.geturl = lambda: entry['url']
        with patch.object(lab.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(ValidationError, 'exceeds pinned size'):
                lab.fetch_artifact(entry, target)
        self.assertFalse(target.exists())

    def test_main_missing_consent_has_no_success_report(self):
        report = self.root / 'report.json'
        with patch('sys.stderr', new_callable=io.StringIO), \
                patch.object(lab, 'Engine') as engine:
            code = lab.main(['evaluate', '--manifest', 'missing', '--samples', 'missing', '--report', str(report)])
        self.assertEqual(code, 2)
        self.assertFalse(report.exists())
        engine.assert_not_called()


class PairedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifests = [self.root / 'model-a.json', self.root / 'model-b.json']
        self.samples = self.root / 'samples.json'
        self.output = self.root / 'results'
        self.calls = []

    def report(self, profile):
        return {'status': 'offline_evaluation_completed', 'profile': profile,
                'model': {'kind': 'test'}, 'model_registry_sha256': 'c' * 64,
                'sample_manifest_sha256': 'd' * 64,
                'runtime': {'num_threads': 2}, 'initialization_seconds': .1,
                'initialization_cpu_seconds': .1, 'inference_cpu_seconds': .1,
                'peak_rss_bytes': 1, 'peak_rss_scope': 'single_model_process_lifetime',
                'overall': {'samples': 1}, 'by_condition': {},
                'samples': [{'id': 'synthetic-1', 'audio_sha256': 'a' * 64}]}

    def subprocess_result(self, command, **kwargs):
        self.calls.append((command, kwargs))
        report = self.report('profile-' + str(len(self.calls)))
        destination = Path(command[command.index('--report') + 1])
        lab.private_json(destination, report)
        return SimpleNamespace(returncode=0, stdout='private decoder output', stderr='private decoder output')

    def compare(self, **kwargs):
        return paired.compare(self.manifests, self.samples, self.output, consent=True, **kwargs)

    def test_fresh_sequential_process_per_model_with_private_unique_reports(self):
        with patch.object(paired.subprocess, 'run', side_effect=self.subprocess_result):
            result = self.compare(vad=True, title_index=self.root / 'index.json', timeout=9)
        self.assertEqual(result['status'], 'paired_comparison_completed')
        self.assertEqual(result['execution'], 'sequential_isolated_processes')
        self.assertEqual(len(self.calls), 2)
        reports = []
        for index, (command, kwargs) in enumerate(self.calls):
            self.assertEqual(command[:4], [sys.executable, '-m', 'poc.asr_lab', 'evaluate'])
            self.assertEqual(command[command.index('--manifest') + 1], str(self.manifests[index]))
            self.assertIn('--consent', command)
            self.assertIn('--vad', command)
            self.assertIn('--title-index', command)
            self.assertEqual(kwargs['timeout'], 9)
            reports.append(Path(command[command.index('--report') + 1]))
        self.assertNotEqual(reports[0], reports[1])
        for path in [*reports, self.output / 'comparison.json']:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertFalse(result['realtime_microphone_tested'])

    def test_no_consent_before_reads_subprocess_or_output_directory(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('no reads')), \
                patch.object(paired.subprocess, 'run') as process:
            with self.assertRaisesRegex(ValidationError, 'consent'):
                paired.compare(self.manifests, self.samples, self.output)
        self.assertFalse(self.output.exists())
        process.assert_not_called()

    def test_non_boolean_consent_never_starts_comparison(self):
        for consent in ('false', 'true', 1, [], {}, object()):
            with self.subTest(consent_type=type(consent).__name__), \
                    patch.object(Path, 'read_text', side_effect=AssertionError('no reads')), \
                    patch.object(paired.subprocess, 'run') as process:
                with self.assertRaisesRegex(ValidationError, 'consent'):
                    paired.compare(self.manifests, self.samples, self.output, consent=consent)
                self.assertFalse(self.output.exists())
                process.assert_not_called()

    def test_duplicate_manifest_and_one_model_rejected(self):
        for manifests in ([self.manifests[0]], [self.manifests[0], self.manifests[0]]):
            with self.subTest(manifests=len(manifests)), patch.object(paired.subprocess, 'run') as process:
                with self.assertRaises(ValidationError):
                    paired.compare(manifests, self.samples, self.output, consent=True)
                process.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_nonzero_child_exit_cannot_report_complete_or_leak_stderr(self):
        def child(command, **kwargs):
            if not self.calls:
                return self.subprocess_result(command, **kwargs)
            self.calls.append((command, kwargs))
            return SimpleNamespace(returncode=7, stdout='private transcript', stderr='private transcript')
        with patch.object(paired.subprocess, 'run', side_effect=child):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertEqual(len(result['models']), 1)
        self.assertNotIn('private transcript', json.dumps(result))
        self.assertEqual(result['failures'][0]['exit_code'], 7)

    def test_missing_child_report_cannot_report_complete(self):
        with patch.object(paired.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertEqual(len(result['models']), 0)

    def test_timeout_cannot_report_complete(self):
        with patch.object(paired.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fake', .01)):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertEqual(result['failures'][0]['reason'], 'evaluation_timeout')

    def test_changed_audio_identity_is_rejected(self):
        def child(command, **kwargs):
            self.subprocess_result(command, **kwargs)
            path = Path(command[command.index('--report') + 1])
            if len(self.calls) == 2:
                report = json.loads(path.read_text())
                report['samples'][0]['audio_sha256'] = 'b' * 64
                path.write_text(json.dumps(report))
            return SimpleNamespace(returncode=0)
        with patch.object(paired.subprocess, 'run', side_effect=child):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertIn('samples_changed_between_engines', [f['reason'] for f in result['failures']])

    def test_changed_sample_manifest_is_rejected(self):
        def child(command, **kwargs):
            self.subprocess_result(command, **kwargs)
            path = Path(command[command.index('--report') + 1])
            if len(self.calls) == 2:
                report = json.loads(path.read_text())
                report['sample_manifest_sha256'] = 'e' * 64
                path.write_text(json.dumps(report))
            return SimpleNamespace(returncode=0)
        with patch.object(paired.subprocess, 'run', side_effect=child):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertIn('samples_changed_between_engines', [f['reason'] for f in result['failures']])

    def test_duplicate_profile_is_rejected_even_with_distinct_manifests(self):
        def child(command, **kwargs):
            self.subprocess_result(command, **kwargs)
            path = Path(command[command.index('--report') + 1])
            report = json.loads(path.read_text())
            report['profile'] = 'same-profile'
            path.write_text(json.dumps(report))
            return SimpleNamespace(returncode=0)
        with patch.object(paired.subprocess, 'run', side_effect=child):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertIn('duplicate_profile', [f['reason'] for f in result['failures']])

    def test_incomplete_child_report_with_zero_exit_is_not_success(self):
        def child(command, **kwargs):
            self.subprocess_result(command, **kwargs)
            path = Path(command[command.index('--report') + 1])
            report = json.loads(path.read_text())
            report['status'] = 'rejected_or_unavailable'
            path.write_text(json.dumps(report))
            return SimpleNamespace(returncode=0)
        with patch.object(paired.subprocess, 'run', side_effect=child):
            result = self.compare()
        self.assertEqual(result['status'], 'paired_comparison_incomplete')
        self.assertTrue(result['failures'])

    def test_bad_json_missing_fields_empty_samples_and_duplicate_ids_are_incomplete(self):
        for case in ('bad_json', 'missing_field', 'empty_samples', 'duplicate_ids', 'wrong_count'):
            self.output = self.root / case
            self.calls = []
            def child(command, **kwargs):
                self.subprocess_result(command, **kwargs)
                path = Path(command[command.index('--report') + 1])
                if case == 'bad_json':
                    path.write_text('{broken')
                else:
                    report = json.loads(path.read_text())
                    if case == 'missing_field':
                        report.pop('runtime')
                    elif case == 'empty_samples':
                        report['samples'] = []
                        report['overall']['samples'] = 0
                    elif case == 'duplicate_ids':
                        report['samples'] *= 2
                        report['overall']['samples'] = 2
                    else:
                        report['overall']['samples'] = 2
                    path.write_text(json.dumps(report))
                return SimpleNamespace(returncode=0)
            with self.subTest(case=case), patch.object(paired.subprocess, 'run', side_effect=child):
                result = self.compare()
            self.assertEqual(result['status'], 'paired_comparison_incomplete')
            self.assertTrue(all(f['reason'] == 'evaluation_report_invalid' for f in result['failures']))

    def test_existing_output_directory_is_not_reused(self):
        self.output.mkdir()
        evidence = self.output / 'keep.txt'
        evidence.write_text('retained')
        with patch.object(paired.subprocess, 'run') as process, self.assertRaises(FileExistsError):
            self.compare()
        process.assert_not_called()
        self.assertEqual(evidence.read_text(), 'retained')


if __name__ == '__main__':
    unittest.main()
