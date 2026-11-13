"""Isolated laboratory boundary tests; no microphone, models, Docker or TV used."""
import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

from poc import asr_lab_server as service
from poc import push_to_talk as ptt

TOKEN = 'asr-lab-test-fixture-' + 'x' * 32


def wav(path, frames=1600, rate=16000, channels=1):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b'\0' * frames * channels * 2)


class LabHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audio = self.root / 'audio'
        self.audio.mkdir()
        wav(self.audio / 'sample.wav')
        self.engine = Mock()
        self.engine.transcribe.return_value = {'hypothesis': '打开飞牛', 'wall_seconds': .1,
                                                'cpu_seconds': .08, 'first_partial_seconds': .03}
        self.factory = Mock(return_value=self.engine)
        self.suggester = Mock()
        self.suggester.suggest.return_value = {'candidates': [], 'requires_confirmation': True}
        self.server = service.create_server('fixed-manifest.json', self.audio, token=str(TOKEN),
                                            engine_factory=self.factory, suggester=self.suggester)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method='POST', path='/transcribe', body=None, token=str(TOKEN), raw=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        payload = raw if raw is not None else json.dumps(body if body is not None else {'path': 'sample.wav', 'consent': True})
        options = {'Content-Type': 'application/json'}
        if token is not None:
            options['Authorization'] = 'Bearer ' + token
        options.update(headers or {})
        try:
            client.request(method, path, body=payload if method == 'POST' else None, headers=options)
            result = client.getresponse()
            return result.status, json.loads(result.read())
        finally:
            client.close()

    def wav_bytes(self, frames=1600, rate=16000, channels=1):
        target = self.root / 'upload.wav'
        wav(target, frames=frames, rate=rate, channels=channels)
        return target.read_bytes()

    def upload(self, payload=None, *, token=str(TOKEN), headers=None):
        options = {'Content-Type': 'audio/wav', 'X-ASR-Consent': 'true',
                   'X-Capture-Kind': 'push_to_talk_microphone_then_offline'}
        options.update(headers or {})
        return self.request('POST', '/transcribe-upload', token=token,
                            raw=payload if payload is not None else self.wav_bytes(),
                            headers=options)

    def test_authenticated_wav_upload_is_recognized_with_capture_label(self):
        status, value = self.upload()
        self.assertEqual(status, 200)
        self.assertEqual(value['capture_kind'], 'push_to_talk_microphone_then_offline')
        self.assertEqual(value['asr']['hypothesis'], '打开飞牛')
        self.assertTrue(value['suggestions']['requires_confirmation'])
        self.engine.transcribe.assert_called_once()
        self.assertEqual(self.engine.transcribe.call_args.args[0].name, 'audio.wav')
        self.assertFalse(self.engine.transcribe.call_args.args[0].exists())

    def test_upload_requires_consent_token_content_type_and_valid_label(self):
        self.assertEqual(self.upload(headers={'X-ASR-Consent': 'false'})[0], 403)
        self.assertEqual(self.upload(token=None)[0], 401)
        self.assertEqual(self.upload(headers={'Content-Type': 'application/octet-stream'})[0], 415)
        self.assertEqual(self.upload(headers={'X-Capture-Kind': 'live_microphone'})[0], 400)
        self.assertEqual(self.upload(headers={'Transfer-Encoding': 'chunked'})[0], 400)
        self.engine.transcribe.assert_not_called()

    def test_upload_rejects_bad_size_and_malformed_wav(self):
        self.assertEqual(self.upload(b'RIFF' + b'\0' * 20)[0], 413)
        self.assertEqual(self.upload(b'x' * 2_000_001)[0], 413)
        self.assertEqual(self.upload(b'not a wav' * 20)[0], 400)
        self.assertEqual(self.upload(self.wav_bytes(rate=8000))[0], 400)
        self.assertEqual(self.upload(self.wav_bytes(channels=2))[0], 400)
        self.assertEqual(self.upload(self.wav_bytes(frames=960_001))[0], 400)
        self.engine.transcribe.assert_not_called()

    def test_client_early_close_does_not_kill_upload_server(self):
        import socket
        sock = socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=3)
        sock.sendall(b'POST /transcribe-upload HTTP/1.1\r\nHost: localhost\r\n'
                     b'Authorization: Bearer ' + TOKEN.encode() + b'\r\n'
                     b'Content-Type: audio/wav\r\nX-ASR-Consent: true\r\n'
                     b'Content-Length: 200000\r\n\r\nshort')
        sock.close()
        self.assertEqual(self.request('GET', '/health', token=None)[0], 200)

    def test_ready_only_after_fixed_engine_constructed(self):
        self.factory.assert_called_once_with('fixed-manifest.json', num_threads=2, vad=False)
        status, value = self.request('GET', '/health', token=None)
        self.assertEqual(status, 200)
        self.assertEqual(value, {'service': 'asr-lab', 'ready': True, 'capture_kind': 'offline_file_transcription'})
        self.assertTrue(self.server.ready)

    def test_resource_limits_cannot_exceed_engine_bound(self):
        for options in ({'num_threads': 5}, {'num_threads': True}, {'max_frames': 960001},
                        {'max_audio_bytes': 2000001}, {'max_frames': 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                service.create_server('fixed', self.audio, token=str(TOKEN),
                                      engine_factory=self.factory, **options)
        self.factory.assert_called_once()

    def test_model_load_failure_never_binds_or_reports_ready(self):
        with patch.object(service, 'ThreadingHTTPServer') as bind:
            with self.assertRaises(RuntimeError):
                service.create_server('unverified', self.audio, token=str(TOKEN),
                                      engine_factory=Mock(side_effect=RuntimeError('hash failed')))
        bind.assert_not_called()

    def test_success_uses_snapshot_and_returns_suggestions_without_execution(self):
        observed = []
        def transcribe(path):
            observed.append(Path(path))
            self.assertNotEqual(path, self.audio / 'sample.wav')
            self.assertEqual(Path(path).read_bytes(), (self.audio / 'sample.wav').read_bytes())
            return {'hypothesis': '打开飞牛', 'wall_seconds': .1}
        self.engine.transcribe.side_effect = transcribe
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(result['asr']['hypothesis'], '打开飞牛')
        self.assertEqual(result['capture_kind'], 'offline_file_transcription')
        self.suggester.suggest.assert_called_once_with('打开飞牛')
        self.assertFalse(observed[0].exists())
        self.assertNotIn('execution', result)
        self.assertNotIn('production', result)

    def test_completed_requests_allow_immediate_next_request(self):
        for _ in range(20):
            self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.engine.transcribe.call_count, 20)

    def test_missing_wrong_and_non_ascii_credentials_rejected(self):
        for token in (None, 'wrong-token', 'é' * 32):
            with self.subTest(token=token):
                self.assertEqual(self.request(token=token)[0], 401)
        self.engine.transcribe.assert_not_called()

    def test_explicit_boolean_consent_before_audio_access(self):
        with patch.object(service, 'snapshot_audio') as snapshot:
            for consent in (None, False, 1, 'true'):
                self.assertEqual(self.request(body={'path': 'sample.wav', 'consent': consent})[0], 403)
        snapshot.assert_not_called()

    def test_no_per_request_model_or_production_configuration(self):
        for key in ('manifest', 'model', 'num_threads', 'token_file', 'execute'):
            self.assertEqual(self.request(body={'path': 'sample.wav', 'consent': True, key: 'arbitrary'})[0], 400)
        self.engine.transcribe.assert_not_called()
        self.factory.assert_called_once()

    def test_absolute_traversal_and_ambiguous_paths_rejected(self):
        for path in ('/etc/passwd.wav', '../sample.wav', 'sub/../../sample.wav',
                     './sample.wav', 'sub//file.wav', 'sub\\file.wav', 'sample.mp3', '', None):
            with self.subTest(path=path):
                self.assertEqual(self.request(body={'path': path, 'consent': True})[0], 400)
        self.engine.transcribe.assert_not_called()

    def test_file_and_parent_symlinks_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        wav(outside / 'private.wav')
        (self.audio / 'linked.wav').symlink_to(outside / 'private.wav')
        (self.audio / 'linked').symlink_to(outside, target_is_directory=True)
        for path in ('linked.wav', 'linked/private.wav'):
            self.assertEqual(self.request(body={'path': path, 'consent': True})[0], 400)
        self.engine.transcribe.assert_not_called()

    def test_fifo_and_directory_not_audio(self):
        (self.audio / 'dir.wav').mkdir()
        os.mkfifo(self.audio / 'pipe.wav')
        for path in ('dir.wav', 'pipe.wav'):
            self.assertEqual(self.request(body={'path': path, 'consent': True})[0], 400)
        self.engine.transcribe.assert_not_called()

    def test_wrong_format_empty_and_truncated_audio(self):
        wav(self.audio / 'stereo.wav', channels=2)
        wav(self.audio / 'rate.wav', rate=8000)
        wav(self.audio / 'empty.wav', frames=0)
        (self.audio / 'truncated.wav').write_bytes((self.audio / 'sample.wav').read_bytes()[:-50])
        (self.audio / 'garbage.wav').write_bytes(b'not wave' * 20)
        for path in ('stereo.wav', 'rate.wav', 'empty.wav', 'truncated.wav', 'garbage.wav'):
            with self.subTest(path=path):
                self.assertEqual(self.request(body={'path': path, 'consent': True})[0], 400)
        self.engine.transcribe.assert_not_called()

    def test_byte_and_frame_limits_before_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            for byte_limit, frame_limit in ((100, 1600), (4000, 100)):
                with self.subTest(byte_limit=byte_limit), self.assertRaises(service.RequestRejected):
                    service.snapshot_audio(self.audio, 'sample.wav', Path(temporary) / 'snapshot.wav', byte_limit, frame_limit)

    def test_invalid_json_body_limits_and_unknown_endpoints(self):
        self.assertEqual(self.request(raw='{')[0], 400)
        self.assertEqual(self.request(raw='[]')[0], 403)
        self.assertEqual(self.request(raw='x' * 9000)[0], 413)
        self.assertEqual(self.request(headers={'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertEqual(self.request('GET', '/production')[0], 404)
        self.assertEqual(self.request(path='/play')[0], 404)

    def test_busy_is_immediately_rejected_and_released_after_failure(self):
        entered, release = threading.Event(), threading.Event()
        def block(path):
            entered.set()
            release.wait(timeout=3)
            raise RuntimeError('private error details')
        self.engine.transcribe.side_effect = block
        first = []
        thread = threading.Thread(target=lambda: first.append(self.request()))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(self.request()[0], 409)
        finally:
            release.set()
            thread.join(timeout=3)
        self.assertEqual(first[0], (500, {'error': 'recognition_failed'}))
        self.engine.transcribe.side_effect = None
        self.assertEqual(self.request()[0], 200)

    def test_response_does_not_echo_exception_paths(self):
        self.engine.transcribe.side_effect = RuntimeError('private token path /secret')
        status, result = self.request()
        self.assertEqual(status, 500)
        self.assertNotIn('secret', json.dumps(result))


class TokenAndCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_only_independent_token_environment_and_file(self):
        with patch.dict(os.environ, {'AI_TV_TOKEN': TOKEN}, clear=True):
            with self.assertRaises(ValueError):
                service.load_token()
        with patch.dict(os.environ, {'ASR_LAB_TOKEN': TOKEN}, clear=True):
            self.assertEqual(service.load_token(), TOKEN)
        token = self.root / 'experiment-token'
        token.write_text(TOKEN)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(service.load_token(token), TOKEN)
            with self.assertRaises(ValueError):
                service.load_token(self.root / 'console.token')
        with patch.dict(os.environ, {'ASR_LAB_TOKEN': TOKEN}, clear=True):
            with self.assertRaises(ValueError):
                service.load_token(token)

    def test_token_file_symlink_and_short_token_fail(self):
        token = self.root / 'experiment-token'
        token.write_text(TOKEN)
        linked = self.root / 'link'
        linked.symlink_to(token)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(OSError):
            service.load_token(linked)
        with patch.dict(os.environ, {'ASR_LAB_TOKEN': TOKEN[:5]}, clear=True), self.assertRaises(ValueError):
            service.load_token()

    def test_no_consent_rejects_before_capture_or_input_read(self):
        with patch.object(ptt, 'record_microphone') as capture, patch.object(ptt, 'convert_wav') as convert:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                ptt.main(['--reference', '暂停', '--output', str(self.root / 'out.wav')])
        self.assertEqual(error.exception.code, 2)
        capture.assert_not_called()
        convert.assert_not_called()

    def test_offline_wav_has_honest_manifest_and_private_permissions(self):
        source, target = self.root / 'source.wav', self.root / 'out.wav'
        wav(source)
        with patch.object(ptt, 'convert_wav', side_effect=lambda a, b: shutil.copyfile(a, b)), \
                patch.object(ptt, 'record_microphone') as capture, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ptt.main(['--consent', '--reference', '打开飞牛', '--input-wav', str(source),
                                      '--output', str(target)]), 0)
        capture.assert_not_called()
        manifest = target.with_suffix('.samples.json')
        record = json.loads(manifest.read_text())
        self.assertEqual(record['capture_kind'], 'offline_wav_input')
        self.assertEqual(record['samples'][0]['capture_kind'], 'offline_wav_input')
        self.assertTrue(record['samples'][0]['authorized'])
        self.assertEqual(record['samples'][0]['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        from poc.sherpa_asr import validate_samples
        self.assertEqual(len(validate_samples(record, manifest.parent)), 1)

    def test_microphone_capture_then_offline_label_is_explicit(self):
        target = self.root / 'mic.wav'
        with patch.object(ptt, 'record_microphone', side_effect=lambda path, **kw: wav(path)) as capture, \
                patch.object(ptt, 'convert_wav', side_effect=lambda a, b: shutil.copyfile(a, b)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ptt.main(['--consent', '--reference', '暂停', '--seconds', '1', '--output', str(target)]), 0)
        self.assertEqual(capture.call_args.kwargs['seconds'], 1)
        record = json.loads(target.with_suffix('.samples.json').read_text())
        self.assertEqual(record['capture_kind'], 'push_to_talk_microphone_then_offline')

    def test_no_recorder_reports_not_accepted(self):
        with patch.object(ptt.shutil, 'which', return_value=None), self.assertRaisesRegex(RuntimeError, 'MICROPHONE_NOT_ACCEPTED'):
            ptt.record_microphone(self.root / 'mic.wav', seconds=1)

    def test_fixed_capture_reuses_arecord_without_shell(self):
        target = self.root / 'mic.wav'
        def recorded(command, **kwargs):
            wav(Path(command[-1]))
        with patch.object(ptt.shutil, 'which', return_value='/usr/bin/arecord'), \
                patch.object(ptt.subprocess, 'run', side_effect=recorded) as run:
            ptt.record_microphone(target, recorder='arecord', device='hw:1', seconds=2)
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'arecord')
        self.assertIn('hw:1', command)
        self.assertIn('-d', command)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_enter_capture_is_bounded_and_stops_child(self):
        target = self.root / 'mic.wav'
        child = Mock(returncode=0)
        child.poll.return_value = None
        child.communicate.return_value = (None, b'')
        def start(command, **kwargs):
            wav(Path(command[-1]))
            return child
        inputs = Mock(return_value='')
        with patch.object(ptt.shutil, 'which', return_value='/usr/bin/ffmpeg'), \
                patch.object(ptt.subprocess, 'Popen', side_effect=start) as popen:
            ptt.record_microphone(target, recorder='ffmpeg', input_fn=inputs)
        self.assertEqual(inputs.call_count, 2)
        self.assertIn('60', popen.call_args.args[0])
        child.send_signal.assert_called_once()
        child.communicate.assert_called_once()

    def test_conversion_uses_ffmpeg_pcm_arguments(self):
        source, target = self.root / 'source.wav', self.root / 'target.wav'
        wav(source)
        with patch.object(ptt.shutil, 'which', return_value='/usr/bin/ffmpeg'), \
                patch.object(ptt.subprocess, 'run', side_effect=lambda command, **kw: wav(Path(command[-1]))) as run:
            ptt.convert_wav(source, target)
        command = run.call_args.args[0]
        self.assertIn('pcm_s16le', command)
        self.assertIn('16000', command)
        self.assertIn('-n', command)

    def test_capture_failure_never_claims_acceptance_or_writes_manifest(self):
        target = self.root / 'failed.wav'
        stderr = io.StringIO()
        with patch.object(ptt, 'record_microphone', side_effect=RuntimeError('no microphone')), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            ptt.main(['--consent', '--reference', '暂停', '--output', str(target)])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('no microphone/service acceptance claimed', stderr.getvalue())
        self.assertFalse(target.with_suffix('.samples.json').exists())

    def test_existing_destinations_reference_and_duration_are_validated(self):
        target = self.root / 'existing.wav'
        wav(target)
        for extra in ([], ['--reference', '，！'], ['--seconds', 'nan'], ['--seconds', '121']):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ptt.main(['--consent', '--reference', '暂停', '--output', str(target), *extra])

    def test_client_refuses_nonlocal_servers(self):
        for url in ('https://localhost', 'http://example.com', 'http://user@localhost', 'http://localhost?token=x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ptt.submit(url, 'sample.wav', TOKEN)

    def test_experiment_compose_and_docker_are_isolated(self):
        root = Path(__file__).resolve().parents[1]
        import yaml
        config = yaml.safe_load((root / 'docker-compose.asr-lab.yml').read_text())
        entry = config['services']['asr-lab']
        self.assertEqual(entry['profiles'], ['asr-lab'])
        self.assertTrue(entry['read_only'])
        self.assertTrue(str(entry['user']).startswith('${ASR_LAB_UID'))
        self.assertNotIn('network_mode', entry)
        self.assertNotIn('privileged', entry)
        self.assertEqual(entry['cpus'], '2.0')
        self.assertTrue(all(item['read_only'] for item in entry['volumes']))
        self.assertTrue(entry['ports'][0].startswith('127.0.0.1:'))
        # Docker refuses max-file: 1 with the local driver's default compression,
        # and an internal network cannot serve a published loopback port.
        self.assertGreaterEqual(int(str(entry['logging']['options']['max-file'])), 2)
        self.assertEqual(config['networks']['asr-lab']['driver'], 'bridge')
        self.assertNotEqual(config['networks']['asr-lab'].get('internal'), True)
        dockerfile = (root / 'deploy/asr-lab/Dockerfile').read_text()
        self.assertIn('--require-hashes', dockerfile)
        self.assertIn('requirements-v41.lock', dockerfile)
        self.assertIn('USER 10001:10001', dockerfile)
        self.assertNotIn(':latest', dockerfile)
        self.assertIn('chmod -R a+rX /app', dockerfile)
        self.assertIn('COPY adbtv/__init__.py adbtv/media_index.py /app/adbtv/', dockerfile)
        self.assertNotIn('COPY adbtv/ /app/adbtv/', dockerfile)
        overlay = yaml.safe_load((root / 'deploy/asr-lab/title-index.override.example.yml').read_text())
        optional = overlay['services']['asr-lab']
        self.assertEqual({v['target'] for v in optional['volumes']}, {'/index/media-index.json', '/media'})
        self.assertTrue(all(v['read_only'] and not v['bind']['create_host_path'] for v in optional['volumes']))
        self.assertIn('--title-cache', optional['command'])
        self.assertNotIn('environment', optional)


if __name__ == '__main__':
    unittest.main()
