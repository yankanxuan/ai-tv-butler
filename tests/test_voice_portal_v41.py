"""Voice portal: authenticated upload, title selection, confirmation and cancellation."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from adbtv.media_index import MediaEntry, MediaIndex
from poc import voice_portal as portal_module
from poc.title_session import TitleSessionStore
from poc.voice_portal import Portal, create_server

CONSOLE_TOKEN = 'console-test-' + 'x' * 32
LAB_TOKEN = 'lab-test-' + 'x' * 32


def wav_bytes(frames=1600, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b'\0' * frames * channels * 2)
    return output.getvalue()


class FakeConsole:
    def __init__(self):
        self.token = CONSOLE_TOKEN
        self.commands = []
        self.cancelled = []
        self.state = 'SUCCESS'
        self.cancel_failure = False
        self.cancel_state = 'CANCELLED'
        self.submit_gate = None
        self.submit_started = threading.Event()
        self.submit_count = 0
        self.idempotency_keys = []
        self.records = []
        self.down = False
        self.server = None
        self.thread = None

    def start(self):
        outer = self
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def _json(self, code, value):
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                return self.headers.get('X-Auth-Token') == outer.token

            def do_GET(self):
                if outer.down: return self._json(503, {'error': 'down'})
                if not self._auth(): return self._json(401, {'error': 'unauthorized'})
                if self.path == '/api/config': return self._json(200, {'llm': {}, 'agent': {}})
                if self.path == '/api/runs/task-1':
                    return self._json(200, {'state': outer.state, 'result': {'ok': True}})
                if self.path == '/api/idempotency':
                    return self._json(200, {'records': list(outer.records)})
                if self.path.startswith('/api/idempotency/'):
                    key = self.path.rsplit('/', 1)[-1]
                    record = next((r for r in outer.records if r['key'] == key), None)
                    return self._json(200, {'record': record}) if record else self._json(404, {'error': 'not found'})
                return self._json(404, {'error': 'not_found'})

            def do_POST(self):
                if outer.down: return self._json(503, {'error': 'down'})
                if not self._auth(): return self._json(401, {'error': 'unauthorized'})
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                if self.path == '/api/tool':
                    return self._json(200, {'result': {'playing': False, 'state': ''}})
                if self.path == '/api/runs':
                    key = self.headers.get('X-Idempotency-Key')
                    existing = next((record for record in outer.records
                                     if key and record['key'] == key), None)
                    if existing is not None:
                        return self._json(202, {'task_id': existing['task_id'], 'idempotent': True})
                    outer.submit_count += 1
                    outer.idempotency_keys.append(key)
                    outer.submit_started.set()
                    if outer.submit_gate is not None:
                        outer.submit_gate.wait(5)
                    outer.commands.append(body.get('command'))
                    outer.state = 'RUNNING'
                    if key:
                        outer.records.append({'task_id': 'task-1', 'key': key,
                                              'state': 'committed', 'task_state': 'RUNNING',
                                              'created': time.time(), 'updated': time.time()})
                    return self._json(202, {'task_id': 'task-1', 'idempotent': False})
                if self.path == '/api/runs/task-1/cancel':
                    if outer.cancel_failure:
                        return self._json(503, {'error': 'cancel_unavailable'})
                    outer.cancelled.append('task-1')
                    outer.state = outer.cancel_state
                    return self._json(200, {'id': 'task-1', 'requested': 'cancel'})
                return self._json(404, {'error': 'not_found'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def stop(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


class FakeLab:
    def __init__(self):
        self.down = False
        self.busy = False
        self.capture = []
        self.hypothesis = '搜索穿成一神我靠香火斩诸神'
        self.server = None
        self.thread = None

    def start(self):
        outer = self
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def _json(self, code, value):
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                if outer.down: return self._json(503, {'error': 'down'})
                if outer.busy: return self._json(409, {'error': 'busy'})
                if self.headers.get('Authorization') != 'Bearer ' + LAB_TOKEN:
                    return self._json(401, {'error': 'unauthorized'})
                if self.headers.get('X-ASR-Consent') != 'true':
                    return self._json(403, {'error': 'consent'})
                length = int(self.headers.get('Content-Length', '0'))
                payload = self.rfile.read(length)
                if len(payload) <= 44:
                    return self._json(400, {'error': 'bad_audio'})
                outer.capture.append((len(payload), self.headers.get('X-Capture-Kind')))
                return self._json(200, {'asr': {'hypothesis': outer.hypothesis, 'wall_seconds': .2},
                                        'capture_kind': self.headers.get('X-Capture-Kind')})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def stop(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.console = FakeConsole(); console_url = self.console.start()
        self.lab = FakeLab(); lab_url = self.lab.start()
        self.addCleanup(self.console.stop); self.addCleanup(self.lab.stop)
        self.clock = [1000.0]
        index = MediaIndex([])
        for title, hint, count in (('穿成野神我靠香火斩诸神', 1, 3),
                                   ('穿成野神我靠香火斩诸神', 2, 2),
                                   ('暗河谜案', 1, 1)):
            folder = self.root / f'{title}-{hint}'
            folder.mkdir(parents=True, exist_ok=True)
            seasons = {}
            for episode in range(1, count + 1):
                path = folder / f'S{hint:02d}E{episode:03d}.mp4'
                path.write_bytes(b'video')
                seasons[str(episode)] = str(path)
            index.entries.append(MediaEntry(base=title, title=title, seasons={'1': seasons},
                                            year='2026', season_hint=hint))
        self.store = TitleSessionStore(index, now=lambda: self.clock[0])
        self.portal = Portal(store=self.store, lab_url=lab_url, lab_token=str(LAB_TOKEN),
                             console_url=console_url, now=lambda: self.clock[0])
        self.server = create_server(self.portal, host='127.0.0.1', port=0, secure_cookie=False)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.cookie = None; self.csrf = None

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None, cookie=None, csrf=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        options = dict(headers or {})
        if cookie: options['Cookie'] = 'vp_sid=' + cookie
        if csrf is not None: options['X-Portal-CSRF'] = csrf
        payload = body
        if isinstance(body, dict):
            payload = json.dumps(body)
            options['Content-Type'] = 'application/json'
        try:
            client.request(method, path, body=payload, headers=options)
            response = client.getresponse()
            raw = response.read()
            output = json.loads(raw) if raw else {}
            return response.status, output, response.getheader('Set-Cookie')
        finally:
            client.close()

    def login(self, token=CONSOLE_TOKEN):
        status, data, cookie = self.request('POST', '/api/login', {'token': token})
        self.assertEqual(status, 200, data)
        sid = cookie.split(';', 1)[0].split('=', 1)[1]
        self.cookie, self.csrf = sid, data['csrf']
        return sid, data['csrf']

    def upload(self, *, kind='push_to_talk_microphone_then_offline', payload=None, cookie=None, csrf=None):
        return self.request('POST', '/api/transcribe',
                            payload if payload is not None else wav_bytes(),
                            {'Content-Type': 'audio/wav', 'X-ASR-Consent': 'true',
                             'X-Capture-Kind': kind},
                            cookie if cookie is not None else self.cookie,
                            csrf if csrf is not None else self.csrf)

    def test_login_and_session_cookie_security(self):
        status, data, cookie = self.request('POST', '/api/login', {'token': CONSOLE_TOKEN})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', cookie); self.assertIn('SameSite=Strict', cookie)
        sid = cookie.split(';', 1)[0].split('=', 1)[1]
        status, data, _ = self.request('GET', '/api/session', cookie=sid)
        self.assertEqual(status, 200)
        self.assertEqual(data['csrf'], self.csrf or data['csrf'])
        self.assertEqual(self.request('GET', '/api/session')[0], 401)
        self.assertEqual(self.request('POST', '/api/login', {'token': 'x' * 32})[0], 401)

    def test_csrf_is_required_for_state_changes(self):
        self.login()
        status, data, _ = self.request('POST', '/api/seasons', {'work_id': 'x'}, cookie=self.cookie)
        self.assertEqual(status, 403)

    def test_upload_requires_consent_content_type_and_matching_token(self):
        self.login()
        self.assertEqual(self.request('POST', '/api/transcribe', wav_bytes(),
                                      {'Content-Type': 'audio/wav', 'X-ASR-Consent': 'true'})[0], 401)
        self.assertEqual(self.request('POST', '/api/transcribe', wav_bytes(),
                                      {'Content-Type': 'audio/wav'}, self.cookie, self.csrf)[0], 403)
        self.assertEqual(self.request('POST', '/api/transcribe', wav_bytes(),
                                      {'Content-Type': 'application/octet-stream',
                                       'X-ASR-Consent': 'true'}, self.cookie, self.csrf)[0], 415)
        self.assertEqual(self.upload(payload=b'bad')[0], 413)

    def test_authenticated_upload_returns_asr_and_work_level_candidates(self):
        self.login()
        status, data, _ = self.upload()
        self.assertEqual(status, 200, data)
        self.assertEqual(data['original_hypothesis'], '搜索穿成一神我靠香火斩诸神')
        self.assertEqual(data['capture_kind'], 'push_to_talk_microphone_then_offline')
        works = data['title']['works']
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]['title'], '穿成野神我靠香火斩诸神')
        self.assertEqual(works[0]['season_count'], 2)
        self.assertGreaterEqual(works[0]['episode_count'], 5)
        self.assertEqual(self.lab.capture[0][1], 'push_to_talk_microphone_then_offline')

    def test_file_upload_is_labelled_offline_not_microphone(self):
        self.login()
        status, data, _ = self.upload(kind='offline_wav_input')
        self.assertEqual(status, 200)
        self.assertEqual(data['capture_kind'], 'offline_wav_input')
        self.assertEqual(self.lab.capture[0][1], 'offline_wav_input')

    def test_manual_correction_preserves_original_hypothesis(self):
        self.login()
        self.upload()
        status, data, _ = self.request('POST', '/api/retitle', {'text': '搜索暗河谜案'},
                                       cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data['original_hypothesis'], '搜索穿成一神我靠香火斩诸神')
        self.assertEqual(data['title']['works'][0]['title'], '暗河谜案')

    def test_full_selection_confirm_and_one_task(self):
        self.login()
        _, asr, _ = self.upload()
        work_id = asr['title']['works'][0]['work_id']
        status, seasons, _ = self.request('POST', '/api/seasons', {'work_id': work_id},
                                          cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, seasons)
        self.assertEqual([s['season'] for s in seasons['seasons']], [1, 2])
        status, episodes, _ = self.request('POST', '/api/episodes',
                                           {'work_id': work_id, 'season': 2},
                                           cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        episode = episodes['episodes'][0]
        status, selected, _ = self.request('POST', '/api/select',
                                           {'work_id': work_id, 'season': 2, 'episode': episode},
                                           cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(selected['command'], '播放《穿成野神我靠香火斩诸神》第2季第1集')
        status, confirmed, _ = self.request('POST', '/api/confirm', {'confirm': True},
                                            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed['task_id'], 'task-1')
        self.assertEqual(self.console.commands, ['播放《穿成野神我靠香火斩诸神》第2季第1集'])
        status, duplicate, _ = self.request('POST', '/api/confirm', {'confirm': True},
                                            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate['task_id'], 'task-1')
        self.assertTrue(duplicate['idempotent'])
        self.assertEqual(self.console.submit_count, 1)

    def test_cancel_calls_console_and_invalidates_old_selection(self):
        self.login(); self.upload(); work_id = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work_id, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(self.console.cancelled, ['task-1'])
        self.assertEqual(self.request('POST', '/api/confirm', {'confirm': True},
                                      cookie=self.cookie, csrf=self.csrf)[0], 400)

    def test_expiry_blocks_confirmation(self):
        self.login(); self.upload(); work_id = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work_id, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.clock[0] += 601
        status, data, _ = self.request('POST', '/api/confirm', {'confirm': True},
                                       cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertIn('expired', data['error'])
        self.assertEqual(self.console.commands, [])

    def test_two_sessions_cannot_share_candidate_snapshot(self):
        sid_a, csrf_a = self.login()
        _, a, _ = self.upload(cookie=sid_a, csrf=csrf_a)
        work = a['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=sid_a, csrf=csrf_a)
        sid_b, csrf_b = self.login()
        status, data, _ = self.request('GET', '/api/session', cookie=sid_b)
        self.assertEqual(status, 200)
        status, data, _ = self.request('POST', '/api/confirm', {'confirm': True},
                                       cookie=sid_b, csrf=csrf_b)
        self.assertEqual(status, 400)

    def test_busy_and_downstream_errors_are_mapped(self):
        self.login()
        self.portal.asr_gate.acquire()
        try:
            self.assertEqual(self.upload()[0], 409)
        finally:
            self.portal.asr_gate.release()
        self.lab.busy = True
        self.assertEqual(self.upload()[0], 409)
        self.lab.busy = False; self.lab.down = True
        self.assertEqual(self.upload()[0], 502)
        self.lab.down = False
        self.console.down = True
        self.assertEqual(self.request('POST', '/api/login', {'token': CONSOLE_TOKEN})[0], 502)

    def test_task_endpoint_reports_state(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        status, data, _ = self.request('GET', '/api/task', cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data['state'], 'RUNNING')

    def test_static_assets_are_served_with_security_headers(self):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        client.request('GET', '/')
        response = client.getresponse(); body = response.read(); client.close()
        self.assertEqual(response.status, 200)
        self.assertIn('AI 电视管家', body.decode('utf-8'))
        self.assertIn("default-src 'self'", response.getheader('Content-Security-Policy'))
        self.assertEqual(response.getheader('X-Frame-Options'), 'DENY')
        self.assertIn("script-src 'self'", response.getheader('Content-Security-Policy'))

    def test_cancel_failure_keeps_task_and_supports_safe_retry(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        self.console.cancel_failure = True
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'cancel_failed')
        self.assertEqual(data['task_id'], 'task-1')
        self.assertTrue(data['retryable'])
        task = self.request('GET', '/api/task', cookie=self.cookie)[1]
        self.assertEqual(task['state'], 'RUNNING')
        self.console.cancel_failure = False
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'cancelled')
        self.assertEqual(data['final_state'], 'CANCELLED')
        self.assertEqual(self.request('GET', '/api/task', cookie=self.cookie)[1]['state'], 'NONE')

    def test_cancel_pending_and_not_effective_are_not_reported_cancelled(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        self.console.cancel_state = 'RUNNING'
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'cancel_pending')
        self.assertEqual(data['task_id'], 'task-1')
        self.console.cancel_state = 'CANCELLED'
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(data['status'], 'cancelled')
        # A completed task must not be reported as cancelled.
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.console.cancel_state = 'SUCCESS'
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(data['status'], 'cancel_not_effective')
        self.assertEqual(data['final_state'], 'SUCCESS')
        self.assertEqual(data['task_id'], 'task-1')

    def test_new_recognition_is_blocked_while_previous_task_runs(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        status, data, _ = self.upload()
        self.assertEqual(status, 409)
        self.console.state = 'SUCCESS'
        status, data, _ = self.upload()
        self.assertEqual(status, 200)

    def test_cancel_timeout_keeps_task_and_is_retryable(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        with patch.object(self.portal, '_console',
                          side_effect=portal_module.PortalError(504, '取消请求超时')):
            status, data, _ = self.request('POST', '/api/cancel', {},
                                           cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'cancel_failed')
        self.assertEqual(data['task_id'], 'task-1')
        self.assertTrue(data['retryable'])
        # The retained task id is usable after the network recovers.
        self.console.cancel_state = 'CANCELLED'
        status, data, _ = self.request('POST', '/api/cancel', {}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(data['status'], 'cancelled')

    def test_frontend_uses_server_logout_and_only_clears_after_verified_cancel(self):
        script = Path('poc/web/voice_portal/app.js').read_text()
        self.assertIn("'/api/logout'", script)
        self.assertIn("data.status === 'cancelled'", script)
        self.assertIn("cancel_not_effective", script)
        self.assertIn("state.taskId = data.task_id", script)

    def test_logout_revokes_cookie_and_csrf(self):
        sid, csrf = self.login()
        status, data, clear = self.request('POST', '/api/logout', {}, cookie=sid, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'logged_out')
        self.assertIn('Max-Age=0', clear)
        self.assertEqual(self.request('GET', '/api/session', cookie=sid)[0], 401)
        self.assertEqual(self.request('POST', '/api/seasons', {'work_id': 'x'},
                                      cookie=sid, csrf=csrf)[0], 401)
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)

    def test_over_limit_upload_returns_413_and_server_stays_usable(self):
        self.login()
        status, data, _ = self.request('POST', '/api/transcribe', b'x' * 2_000_001,
                                       {'Content-Type': 'audio/wav', 'X-ASR-Consent': 'true',
                                        'X-Capture-Kind': 'offline_wav_input'},
                                       self.cookie, self.csrf)
        self.assertEqual(status, 413)
        self.assertEqual(self.request('GET', '/api/health')[0], 200)
        self.assertEqual(self.request('GET', '/api/session', cookie=self.cookie)[0], 200)

    def test_early_client_close_does_not_kill_server(self):
        import socket
        sock = socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=3)
        sock.sendall(b'POST /api/transcribe HTTP/1.1\r\nHost: localhost\r\n'
                     b'Content-Type: audio/wav\r\nContent-Length: 200000\r\n'
                     b'X-ASR-Consent: true\r\n\r\nshort')
        sock.close()
        self.assertEqual(self.request('GET', '/api/health')[0], 200)

    def test_network_failure_is_not_reported_as_success(self):
        self.login(); self.lab.down = True
        status, data, _ = self.upload()
        self.assertEqual(status, 502)
        self.assertNotEqual(data.get('status'), 'cancelled')

    def test_two_simultaneous_confirms_submit_exactly_one_task(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.request, 'POST', '/api/confirm', {'confirm': True},
                                   None, self.cookie, self.csrf) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual([result[0] for result in results], [200, 200])
        bodies = [result[1] for result in results]
        self.assertTrue(all(body['status'] == 'submitted' for body in bodies))
        self.assertEqual({body['task_id'] for body in bodies}, {'task-1'})
        self.assertTrue(any((body.get('task_state') or {}).get('state') == 'RUNNING'
                            for body in bodies))
        self.assertEqual(self.console.submit_count, 1)
        self.assertEqual(len(self.console.idempotency_keys), 1)
        self.assertTrue(self.console.idempotency_keys[0])

    def test_confirm_and_cancel_concurrently_have_one_consistent_outcome(self):
        import concurrent.futures
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.console.submit_gate = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            confirm = pool.submit(self.request, 'POST', '/api/confirm', {'confirm': True},
                                  None, self.cookie, self.csrf)
            self.assertTrue(self.console.submit_started.wait(2))
            cancel = pool.submit(self.request, 'POST', '/api/cancel', {},
                                 None, self.cookie, self.csrf)
            self.console.submit_gate.set()
            confirm_status, confirm_body, _ = confirm.result(timeout=10)
            cancel_status, cancel_body, _ = cancel.result(timeout=10)
        self.assertEqual(confirm_status, 200)
        self.assertEqual(confirm_body['status'], 'submitted')
        self.assertEqual(cancel_status, 200)
        self.assertIn(cancel_body['status'], ('cancelled', 'cancel_not_effective'))
        self.assertEqual(self.console.submit_count, 1)

    def test_confirm_and_expiry_cleanup_do_not_double_submit(self):
        import concurrent.futures
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.console.submit_gate = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            confirm = pool.submit(self.request, 'POST', '/api/confirm', {'confirm': True},
                                  None, self.cookie, self.csrf)
            self.assertTrue(self.console.submit_started.wait(2))
            self.clock[0] += 10000
            cleanup = pool.submit(self.portal._sweep)
            self.console.submit_gate.set()
            self.assertEqual(confirm.result(timeout=10)[0], 200)
            cleanup.result(timeout=10)
        self.assertEqual(self.console.submit_count, 1)
        self.assertNotIn(self.cookie, self.portal.sessions)
        self.assertTrue(self.portal.task_links)
        self.assertEqual(self.console.cancelled, [])

    def test_confirm_and_logout_concurrently_are_explicit(self):
        import concurrent.futures
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.console.submit_gate = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            confirm = pool.submit(self.request, 'POST', '/api/confirm', {'confirm': True},
                                  None, self.cookie, self.csrf)
            self.assertTrue(self.console.submit_started.wait(2))
            logout = pool.submit(self.request, 'POST', '/api/logout', {},
                                 None, self.cookie, self.csrf)
            self.console.submit_gate.set()
            confirm_status, confirm_body, _ = confirm.result(timeout=10)
            logout_status, logout_body, _ = logout.result(timeout=10)
        self.assertEqual(confirm_status, 200)
        self.assertEqual(confirm_body['status'], 'submitted')
        self.assertEqual(logout_status, 200)
        self.assertEqual(logout_body['status'], 'logged_out')
        self.assertEqual(logout_body['active_task_id'], 'task-1')
        self.assertEqual(self.console.submit_count, 1)
        self.assertEqual(self.request('GET', '/api/session', cookie=self.cookie)[0], 401)

    def test_unknown_submission_is_not_retried(self):
        from unittest.mock import patch
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        real_console = self.portal._console
        calls = []
        def flaky(session, method, path, payload=None, timeout=20):
            if method == 'POST' and path == '/api/runs':
                calls.append(payload)
                self.console.submit_count += 1
                raise portal_module.PortalError(504, 'response lost')
            return real_console(session, method, path, payload, timeout=timeout)
        with patch.object(self.portal, '_console', side_effect=flaky):
            status, first, _ = self.request('POST', '/api/confirm', {'confirm': True},
                                            cookie=self.cookie, csrf=self.csrf)
            status2, second, _ = self.request('POST', '/api/confirm', {'confirm': True},
                                              cookie=self.cookie, csrf=self.csrf)
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(first['status'], 'submit_unknown')
        self.assertEqual(second['status'], 'submit_unknown')
        self.assertFalse(first['retryable'])
        self.assertEqual(first['idempotency_key'], second['idempotency_key'])
        self.assertEqual(len(calls), 1)

    def test_confirm_is_atomic_over_100_rounds(self):
        import concurrent.futures
        import threading as _threading
        from collections import deque
        from poc.voice_portal import Portal
        portal = Portal(store=self.store, lab_url='http://127.0.0.1:8765',
                        lab_token='x' * 32, console_url='http://127.0.0.1:8770')
        submissions = []
        def console(session, method, path, payload=None, timeout=20):
            if method == 'POST' and path == '/api/runs':
                submissions.append(payload['idempotency_key'])
                return 202, {'task_id': f'task-{len(submissions)}'}
            if method == 'GET' and path.startswith('/api/runs/'):
                return 200, {'state': 'RUNNING'}
            raise AssertionError((method, path))
        portal._console = console
        for index in range(100):
            title_session = f'stress-{index}'
            work = self.store.suggest(title_session, '搜索穿成野神我靠香火斩诸神')['works'][0]['work_id']
            self.store.select(title_session, work, 1, 1)
            session = {'token': 'x' * 32, 'csrf': 'c' * 16,
                       'subject': 's' * 64,
                       'title_session': title_session, 'expires_at': 10 ** 12,
                       'requests': deque(), 'task_id': None, 'submission': 'idle',
                       'idempotency_key': None, 'submission_command': None,
                       'submission_error': None, 'revoked': False, 'transcribing': False,
                       'cancelled': False, 'lock': _threading.RLock()}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = [pool.submit(portal.confirm, session, True) for _ in range(2)]
                bodies = [result.result(timeout=10) for result in results]
            self.assertTrue(all(body['status'] == 'submitted' for body in bodies), index)
            self.assertEqual({body['task_id'] for body in bodies}, {f'task-{index + 1}'}, index)
            self.assertEqual(len(submissions), index + 1, index)

    def test_completed_task_does_not_extend_login_or_get_cancelled(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        self.console.state = 'SUCCESS'
        self.clock[0] += 1801
        self.portal._sweep()
        self.assertNotIn(self.cookie, self.portal.sessions)
        self.assertEqual(self.request('GET', '/api/session', cookie=self.cookie)[0], 401)
        self.assertEqual(self.console.cancelled, [])
        self.assertTrue(self.portal.task_links)

    def test_running_task_session_expires_but_task_is_not_cancelled_and_history_survives(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.request('POST', '/api/confirm', {'confirm': True}, cookie=self.cookie, csrf=self.csrf)
        self.clock[0] += 1801
        self.portal._sweep()
        self.assertNotIn(self.cookie, self.portal.sessions)
        self.assertEqual(self.console.cancelled, [])
        # Re-login is a fresh authorization session; history comes from the
        # authenticated console records, not from the old cookie.
        self.login()
        status, history, _ = self.request('GET', '/api/history', cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(history['count'], 1)
        self.assertEqual(history['records'][0]['task_id'], 'task-1')

    def test_cookie_max_age_tracks_server_sliding_ttl(self):
        status, data, cookie = self.request('POST', '/api/login', {'token': CONSOLE_TOKEN})
        self.assertEqual(status, 200)
        sid = cookie.split(';', 1)[0].split('=', 1)[1]
        self.assertIn('Max-Age=1800', cookie)
        self.clock[0] += 100
        status, _, refreshed = self.request('GET', '/api/session', cookie=sid)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=1800', refreshed)
        self.assertIn('HttpOnly', refreshed)
        self.assertIn('SameSite=Strict', refreshed)

    def test_absolute_session_limit_cannot_be_extended_by_requests(self):
        from poc.voice_portal import Portal
        portal = Portal(store=self.store, lab_url='http://127.0.0.1:8765',
                        lab_token='x' * 32, console_url=self.portal.console_url,
                        ttl=2, absolute_ttl=5, now=lambda: self.clock[0])
        sid, _ = portal.login(CONSOLE_TOKEN)
        for now in (1001, 1002, 1003, 1004):
            self.clock[0] = now
            self.assertLessEqual(portal.session(sid)['expires_at'], 1005)
        self.clock[0] = 1005
        with self.assertRaisesRegex(Exception, '过期'):
            portal.session(sid)
        self.assertNotIn(sid, portal.sessions)

    def test_history_requires_login_and_is_owner_scoped(self):
        self.assertEqual(self.request('GET', '/api/history')[0], 401)
        self.login()
        status, body, _ = self.request('GET', '/api/history', cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body['count'], 0)

    def test_rate_limit_is_per_session(self):
        self.login()
        with patch.object(portal_module, 'ASR_RATE', 1):
            self.assertEqual(self.upload()[0], 200)
            self.assertEqual(self.upload()[0], 429)

    def test_confirm_requires_explicit_boolean(self):
        self.login(); self.upload(); work = self.upload()[1]['title']['works'][0]['work_id']
        self.request('POST', '/api/select', {'work_id': work, 'season': 1, 'episode': 1},
                     cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(self.request('POST', '/api/confirm', {'confirm': 'true'},
                                      cookie=self.cookie, csrf=self.csrf)[0], 400)
        self.assertEqual(self.console.commands, [])


if __name__ == '__main__':
    unittest.main()