#!/usr/bin/env python3
"""Authenticated HTTPS push-to-talk portal for the isolated ASR laboratory.

The browser records microphone audio, converts it to 16 kHz mono PCM16 WAV with
standard Web Audio APIs, and uploads it here. This portal forwards the WAV to the
already-isolated ASR lab, resolves real library titles at work/season/episode
level, and only calls the existing console /api/runs after explicit confirmation.
It is not a production service dependency and never stores audio or console tokens
on disk.
"""
from __future__ import annotations

import argparse
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from io import BytesIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from poc.title_session import TitleSessionStore  # noqa: E402

MAX_JSON = 16 * 1024
MAX_AUDIO = 2_000_000
MAX_FRAMES = 960_000
MAX_SESSIONS = 64
SESSION_TTL = 1800
SESSION_ABSOLUTE = 8 * 3600
ASR_RATE = 30
ASR_WINDOW = 60
MAX_ACTIVE_TASKS = 1
STATIC = ROOT / 'poc' / 'web' / 'voice_portal'


class PortalError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_wav(payload):
    if not isinstance(payload, bytes) or len(payload) <= 44 or len(payload) > MAX_AUDIO:
        raise PortalError(413, '音频为空或超过 2 MB')
    try:
        with wave.open(BytesIO(payload), 'rb') as wav:
            if (wav.getcomptype() != 'NONE' or wav.getnchannels() != 1
                    or wav.getsampwidth() != 2 or wav.getframerate() != 16000):
                raise PortalError(415, '仅接受 16 kHz 单声道 PCM16 WAV')
            frames = wav.getnframes()
            if not 0 < frames <= MAX_FRAMES:
                raise PortalError(413, '音频时长必须在 60 秒以内')
            if len(wav.readframes(frames)) != frames * 2:
                raise PortalError(400, 'WAV 数据不完整')
            return frames
    except (wave.Error, EOFError) as exc:
        raise PortalError(400, '无法解析 WAV 音频') from exc


def _http_json(url, *, method='GET', headers=None, payload=None, timeout=20):
    data = None
    options = {'method': method, 'headers': dict(headers or {})}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        options['headers'].setdefault('Content-Type', 'application/json')
    request = urllib.request.Request(url, data=data, **options)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (ValueError, OSError):
            body = {'error': 'http_%d' % exc.code}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PortalError(502, '服务不可达或连接超时') from exc


class Portal:
    def __init__(self, *, store, lab_url, lab_token, console_url, console_token=None,
                 ttl=SESSION_TTL, absolute_ttl=SESSION_ABSOLUTE, now=time.time):
        for name, url in (('lab', lab_url), ('console', console_url)):
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError(f'invalid {name} URL')
        if not lab_token or len(lab_token) < 32:
            raise ValueError('independent ASR lab token required')
        self.store, self.lab_url, self.lab_token = store, lab_url.rstrip('/'), lab_token
        self.console_url, self.console_token = console_url.rstrip('/'), console_token
        self.ttl, self.absolute_ttl, self._now = ttl, absolute_ttl, now
        if not 1 <= ttl <= absolute_ttl <= 7 * 24 * 3600:
            raise ValueError('session TTL must satisfy 1 <= idle <= absolute <= 7 days')
        self.sessions = {}
        self.task_links = {}
        self.lock = threading.RLock()
        self.asr_gate = threading.Semaphore(1)

    def _sweep(self):
        now = self._now()
        with self.lock:
            items = list(self.sessions.items())
            cutoff = now - 7 * 24 * 3600
            self.task_links = {key: value for key, value in self.task_links.items()
                               if value.get('created', 0) >= cutoff}
        # Auth lifetime never depends on a task. Remove the login session even
        # while a task runs; the task link and control-plane record stay intact.
        for sid, session in items:
            with session['lock']:
                if now < session['expires_at']:
                    continue
                with self.lock:
                    if self.sessions.get(sid) is session:
                        self.sessions.pop(sid, None)
                self.store.cancel(session['title_session'])

    def login(self, token):
        if not isinstance(token, str) or not 16 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise PortalError(400, '请输入有效的控制台令牌')
        status, _ = _http_json(self.console_url + '/api/config',
                               headers={'X-Auth-Token': token}, timeout=10)
        if status == 401:
            raise PortalError(401, '控制台令牌无效')
        if status != 200:
            raise PortalError(502, '控制台暂时不可用')
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                self._sweep()
                if len(self.sessions) >= MAX_SESSIONS:
                    raise PortalError(429, '登录会话已满，请稍后再试')
            created = self._now()
            self.sessions[sid] = {'token': token, 'csrf': csrf, 'created': created,
                                  'last_seen': created, 'subject': hashlib.sha256(token.encode()).hexdigest(),
                                  'expires_at': min(created + self.ttl, created + self.absolute_ttl),
                                  'requests': deque(),
                                  'title_session': 't-' + secrets.token_urlsafe(18),
                                  'original': None, 'task_id': None, 'cancelled': False,
                                  'lock': threading.RLock(), 'submission': 'idle',
                                  'idempotency_key': None, 'submission_command': None,
                                  'submission_error': None, 'revoked': False,
                                  'transcribing': False}
        return sid, csrf

    def session(self, sid):
        self._sweep()
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                raise PortalError(401, '登录已过期，请重新登录')
            now = self._now()
            if now >= session['expires_at']:
                self.sessions.pop(sid, None)
                self.store.cancel(session['title_session'])
                raise PortalError(401, '登录已过期，请重新登录')
            session['last_seen'] = now
            session['expires_at'] = min(now + self.ttl,
                                        session['created'] + self.absolute_ttl)
            return session

    def _console(self, session, method, path, payload=None, timeout=20):
        headers = {'X-Auth-Token': session['token']}
        if isinstance(payload, dict) and payload.get('idempotency_key'):
            headers['X-Idempotency-Key'] = str(payload['idempotency_key'])
        return _http_json(self.console_url + path, method=method,
                          headers=headers, payload=payload, timeout=timeout)

    def current_playback(self, session):
        try:
            status, body = self._console(session, 'POST', '/api/tool',
                                         {'name': 'tv_playback_status', 'args': {}}, timeout=8)
            state = body.get('result') if status == 200 and isinstance(body, dict) else {}
            if not isinstance(state, dict):
                return {}
            title = state.get('title') or state.get('media_title') or state.get('program')
            return {'title': title, 'playing': bool(state.get('playing')),
                    'state': state.get('state')} if title or 'playing' in state else {}
        except PortalError:
            return {}

    def _lab(self, payload, capture_kind):
        request = urllib.request.Request(
            self.lab_url + '/transcribe-upload', data=payload, method='POST',
            headers={'Authorization': 'Bearer ' + self.lab_token,
                     'Content-Type': 'audio/wav', 'X-ASR-Consent': 'true',
                     'X-Capture-Kind': capture_kind})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (ValueError, OSError):
                body = {'error': 'lab_http_%d' % exc.code}
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PortalError(504, 'ASR 实验服务超时或连接中断') from exc

    def transcribe(self, session, payload, capture_kind):
        if capture_kind not in ('push_to_talk_microphone_then_offline', 'offline_wav_input'):
            raise PortalError(400, '非法的采集类型')
        frames = _safe_wav(payload)
        with session['lock']:
            if session.get('revoked'):
                raise PortalError(401, '会话已注销')
            if session.get('transcribing'):
                raise PortalError(409, '已有识别请求正在进行')
            if session.get('submission') in ('submitting', 'unknown'):
                raise PortalError(409, '上次提交结果未确认，不能开始新的识别')
            if session.get('task_id'):
                task = self._query_task(session, session['task_id'])
                if task.get('state') not in ('SUCCESS', 'SUCCEEDED', 'PARTIAL',
                                             'FAILED', 'UNKNOWN', 'INTERRUPTED',
                                             'CANCELLED'):
                    raise PortalError(409, '上一任务仍在执行，不能开始新的识别')
                self._reset_submission(session)
            session['transcribing'] = True
        try:
            now = self._now()
            with self.lock:
                requests = session['requests']
                while requests and now - requests[0] > ASR_WINDOW:
                    requests.popleft()
                if len(requests) >= ASR_RATE:
                    raise PortalError(429, '识别请求过于频繁，请稍后再试')
                requests.append(now)
            if not self.asr_gate.acquire(blocking=False):
                raise PortalError(409, 'ASR 实验服务正忙，请稍后重试')
            try:
                status, body = self._lab(payload, capture_kind)
            finally:
                self.asr_gate.release()
            if status == 401:
                raise PortalError(502, 'ASR 实验服务认证失败')
            if status == 409:
                raise PortalError(409, 'ASR 实验服务正忙，请稍后重试')
            if status != 200 or not isinstance(body, dict) or not isinstance(body.get('asr'), dict):
                raise PortalError(502 if status >= 500 else 400, 'ASR 识别失败')
            hypothesis = body['asr'].get('hypothesis')
            if not isinstance(hypothesis, str) or not hypothesis.strip():
                raise PortalError(422, '没有识别出文字，请重录或手动输入')
            current = self.current_playback(session)
            title = self.store.suggest(session['title_session'], hypothesis,
                                       hypothesis=hypothesis, current=current)
            with session['lock']:
                session['original'] = hypothesis
                session['task_id'] = None
                session['cancelled'] = False
                session['submission'] = 'idle'
                session['idempotency_key'] = None
                session['submission_command'] = None
                session['submission_error'] = None
                return {'asr': body['asr'], 'title': title, 'capture_kind': capture_kind,
                        'audio_seconds': frames / 16000, 'current_playback': current,
                        'original_hypothesis': hypothesis}
        finally:
            with session['lock']:
                session['transcribing'] = False

    def retitle(self, session, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 256:
            raise PortalError(400, '修正文本须为 1..256 字符')
        original = session.get('original')
        result = self.store.suggest(session['title_session'], text,
                                    hypothesis=original or text,
                                    current=self.current_playback(session))
        return {'title': result, 'original_hypothesis': original, 'edited_hypothesis': text}

    def seasons(self, session, work_id):
        return self.store.seasons(session['title_session'], work_id)

    def episodes(self, session, work_id, season):
        return self.store.episodes(session['title_session'], work_id, season)

    def select(self, session, work_id, season, episode):
        return self.store.select(session['title_session'], work_id, season, episode)

    def confirm(self, session, confirm):
        if confirm is not True:
            raise PortalError(400, '必须明确确认后才执行')
        if session.get('task_id'):
            raise PortalError(409, '已有任务在执行或已完成，请先取消或开始新一轮')
        confirmed = self.store.confirm(session['title_session'], confirm=True)
        command = confirmed['command']
        try:
            status, body = self._console(session, 'POST', '/api/runs', {'command': command})
        except PortalError:
            self.store.cancel(session['title_session'])
            raise
        if status == 401:
            self.store.cancel(session['title_session'])
            raise PortalError(401, '控制台认证失效')
        if status == 409:
            self.store.cancel(session['title_session'])
            raise PortalError(409, '电视正忙，任务未提交')
        if status != 202 or not isinstance(body, dict) or not body.get('task_id'):
            self.store.cancel(session['title_session'])
            raise PortalError(502, '控制台未接受任务')
        session['task_id'] = body['task_id']
        session['cancelled'] = False
        return {'status': 'submitted', 'task_id': body['task_id'],
                'command': command, 'selection': confirmed['selection']}

    def logout(self, sid):
        with self.lock:
            session = self.sessions.pop(sid, None)
        if session is None:
            return {'revoked': False, 'active_task_id': None}
        with session['lock']:
            session['revoked'] = True
            active = session.get('task_id')
            self.store.cancel(session['title_session'])
            return {'revoked': True, 'active_task_id': active,
                    'submission': session.get('submission')}

    @staticmethod
    def _reset_submission(session):
        session['task_id'] = None
        session['submission'] = 'idle'
        session['idempotency_key'] = None
        session['submission_command'] = None
        session['submission_error'] = None
        session['cancelled'] = False
        session['confirmed_selection'] = None

    def _query_task(self, session, task_id):
        try:
            status, body = self._console(session, 'GET', f'/api/runs/{task_id}', timeout=5)
        except PortalError as exc:
            return {'error': exc.message}
        if status != 200 or not isinstance(body, dict):
            return {'error': '任务状态查询失败'}
        return {'state': body.get('state'), 'result': body.get('result')}

    def confirm(self, session, confirm):
        if confirm is not True:
            raise PortalError(400, '必须明确确认后才执行')
        with session['lock']:
            if session.get('revoked'):
                raise PortalError(401, '会话已注销')
            state = session.get('submission', 'idle')
            if state == 'submitted':
                # Idempotent retry: return the original task and, when possible,
                # recover its current state instead of submitting another task.
                task = self._query_task(session, session['task_id'])
                return {'status': 'submitted', 'task_id': session['task_id'],
                        'command': session.get('submission_command'),
                        'selection': session.get('confirmed_selection'),
                        'idempotent': True, 'task_state': task}
            if session.get('transcribing'):
                raise PortalError(409, '识别仍在进行，不能同时确认')
            if state == 'unknown':
                return {'status': 'submit_unknown', 'task_id': None,
                        'idempotency_key': session.get('idempotency_key'),
                        'retryable': False,
                        'error': '任务提交结果未知；为避免重复创建，未再次提交'}
            if state == 'submitting':
                # The session lock is held by the submitter, so this is only
                # reachable on a re-entrant call; do not issue a second request.
                return {'status': 'submit_in_progress', 'task_id': None,
                        'idempotency_key': session.get('idempotency_key'),
                        'retryable': True}
            if session.get('task_id'):
                raise PortalError(409, '已有任务在执行或已完成，请先取消或开始新一轮')
            key = secrets.token_urlsafe(18)
            session['submission'] = 'submitting'
            session['idempotency_key'] = key
            session['submission_error'] = None
            try:
                confirmed = self.store.confirm(session['title_session'], confirm=True)
            except ValueError:
                self._reset_submission(session)
                raise
            command = confirmed['command']
            session['submission_command'] = command
            session['confirmed_selection'] = confirmed['selection']
            try:
                status, body = self._console(
                    session, 'POST', '/api/runs', {'command': command,
                                                   'idempotency_key': key},
                    timeout=20)
            except PortalError as exc:
                # The request may have reached the console; do not create a
                # second task. Keep the key and report an unknown submission.
                session['submission'] = 'unknown'
                session['submission_error'] = exc.message
                session['task_id'] = None
                return {'status': 'submit_unknown', 'task_id': None,
                        'idempotency_key': key, 'retryable': False,
                        'error': '提交结果未知，未重复提交；' + exc.message}
            if status == 202 and isinstance(body, dict) and body.get('task_id'):
                session['task_id'] = body['task_id']
                session['submission'] = 'submitted'
                session['cancelled'] = False
                with self.lock:
                    self.task_links[(session['subject'], key)] = {
                        'task_id': body['task_id'], 'created': self._now(),
                        'command': command, 'state': 'RUNNING'}
                return {'status': 'submitted', 'task_id': body['task_id'],
                        'command': command, 'selection': confirmed['selection'],
                        'idempotency_key': key, 'idempotent': False}
            if status in (400, 401, 403, 409, 422):
                # A defined rejection means no task was created; allow a new
                # explicit attempt after clearing the consumed title snapshot.
                self.store.cancel(session['title_session'])
                self._reset_submission(session)
                message = (body or {}).get('error') if isinstance(body, dict) else None
                raise PortalError(status if status != 400 else 400,
                                  message or '控制台拒绝了任务')
            # 5xx, malformed 202 without a task id, or other ambiguous result:
            # never retry a new task automatically.
            session['submission'] = 'unknown'
            session['submission_error'] = '控制台响应不明确'
            session['task_id'] = None
            return {'status': 'submit_unknown', 'task_id': None,
                    'idempotency_key': key, 'retryable': False,
                    'error': '任务提交结果未知；为避免重复创建，未再次提交'}

    def cancel(self, session):
        with session['lock']:
            if session.get('revoked'):
                raise PortalError(401, '会话已注销')
            task_id = session.get('task_id')
            if not task_id:
                if session.get('submission') == 'unknown':
                    return {'status': 'cancel_failed', 'task_id': None,
                            'idempotency_key': session.get('idempotency_key'),
                            'retryable': True,
                            'error': '任务提交结果未知且没有 task_id，无法确认取消'}
                self.store.cancel(session['title_session'])
                session['submission'] = 'cancelled'
                session['cancelled'] = True
                return {'status': 'cancelled', 'task_id': None, 'final_state': 'NO_TASK'}
            try:
                status, body = self._console(session, 'POST',
                                             f'/api/runs/{task_id}/cancel', timeout=10)
            except PortalError as exc:
                return {'status': 'cancel_failed', 'task_id': task_id,
                        'idempotency_key': session.get('idempotency_key'),
                        'error': exc.message, 'retryable': True}
            if status not in (200, 202):
                return {'status': 'cancel_failed', 'task_id': task_id,
                        'idempotency_key': session.get('idempotency_key'),
                        'error': '控制台未接受取消请求', 'retryable': True,
                        'control': body}
            deadline = time.monotonic() + 2.0
            while time.monotonic() <= deadline:
                try:
                    state_status, task = self._console(session, 'GET',
                                                       f'/api/runs/{task_id}', timeout=5)
                except PortalError as exc:
                    return {'status': 'cancel_pending', 'task_id': task_id,
                            'idempotency_key': session.get('idempotency_key'),
                            'error': exc.message, 'retryable': True,
                            'control': body}
                if state_status != 200:
                    return {'status': 'cancel_pending', 'task_id': task_id,
                            'error': '取消后的状态查询失败', 'retryable': True,
                            'control': body}
                state = task.get('state')
                if state == 'CANCELLED':
                    self.store.cancel(session['title_session'])
                    session['task_id'] = None
                    session['submission'] = 'cancelled'
                    session['cancelled'] = True
                    return {'status': 'cancelled', 'task_id': task_id,
                            'idempotency_key': session.get('idempotency_key'),
                            'final_state': 'CANCELLED', 'control': body}
                if state in ('SUCCESS', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'UNKNOWN',
                             'INTERRUPTED'):
                    session['cancelled'] = False
                    return {'status': 'cancel_not_effective', 'task_id': task_id,
                            'idempotency_key': session.get('idempotency_key'),
                            'final_state': state, 'retryable': False,
                            'error': '任务已结束，最终状态不是 CANCELLED'}
                time.sleep(0.2)
            return {'status': 'cancel_pending', 'task_id': task_id,
                    'idempotency_key': session.get('idempotency_key'),
                    'final_state': 'RUNNING', 'retryable': True,
                    'error': '取消请求已发出，但未在时限内确认 CANCELLED'}

    def history(self, session):
        status, body = self._console(session, 'GET', '/api/idempotency', timeout=10)
        if status != 200 or not isinstance(body, dict):
            raise PortalError(502, '历史任务查询失败')
        records = body.get('records') or []
        # Only the authenticated owner's records are returned by the console;
        # never merge another session/token's task ids.
        return {'records': records, 'count': len(records)}

    def task(self, session):
        with session['lock']:
            task_id = session.get('task_id')
            submission = session.get('submission')
        if not task_id:
            return {'state': 'NONE', 'submission': submission,
                    'idempotency_key': session.get('idempotency_key')}
        status, body = self._console(session, 'GET', f'/api/runs/{task_id}')
        if status != 200:
            raise PortalError(502, '任务状态查询失败')
        return {'state': body.get('state'), 'result': body.get('result')}


def create_server(portal, *, host='127.0.0.1', port=8443, certfile=None, keyfile=None,
                  secure_cookie=True):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AITVVoicePortal/1'
        sys_version = ''

        def log_message(self, *args):
            pass

        def _session(self):
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            morsel = cookie.get('vp_sid')
            if morsel is None:
                raise PortalError(401, '未登录')
            session = portal.session(morsel.value)
            remaining = max(1, int(session['expires_at'] - portal._now()))
            value = f'vp_sid={morsel.value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={remaining}'
            if secure_cookie:
                value += '; Secure'
            self._cookie_refresh = value
            return session

        def _csrf(self, session):
            supplied = self.headers.get('X-Portal-CSRF', '')
            if not secrets.compare_digest(supplied, session['csrf']):
                raise PortalError(403, 'CSRF 校验失败，请刷新页面')

        def _send(self, code, value, *, content_type='application/json; charset=utf-8', extra=None):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8') if content_type.startswith('application/json') else value
            extra = dict(extra or {})
            refresh = getattr(self, '_cookie_refresh', None)
            if refresh and 'Set-Cookie' not in extra:
                extra['Set-Cookie'] = refresh
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
                             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if secure_cookie:
                self.send_header('Strict-Transport-Security', 'max-age=31536000')
            for key, val in extra.items():
                self.send_header(key, val)
            try:
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return

        def _body(self, limit):
            if self.headers.get('Transfer-Encoding'):
                raise PortalError(400, '不支持分块上传')
            try:
                length = int(self.headers.get('Content-Length', '0') or 0)
            except ValueError as exc:
                raise PortalError(400, '无效的请求长度') from exc
            if length <= 0:
                raise PortalError(400, '请求体为空')
            if length > limit:
                # Drain a bounded amount before replying so a normal over-limit
                # client receives 413 instead of racing a connection reset.
                self.close_connection = True
                remaining = min(length, 8 * 1024 * 1024)
                try:
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                raise PortalError(413, '请求体超出限制')
            try:
                payload = self.rfile.read(length)
            except (BrokenPipeError, ConnectionResetError):
                raise PortalError(400, '客户端提前关闭连接') from None
            if len(payload) != length:
                raise PortalError(400, '请求体不完整')
            return payload

        def do_GET(self):
            self._cookie_refresh = None
            try:
                path = urllib.parse.urlsplit(self.path).path
                if path == '/':
                    return self._send(200, (STATIC / 'index.html').read_bytes(),
                                      content_type='text/html; charset=utf-8')
                if path == '/app.js':
                    return self._send(200, (STATIC / 'app.js').read_bytes(),
                                      content_type='text/javascript; charset=utf-8')
                if path == '/style.css':
                    return self._send(200, (STATIC / 'style.css').read_bytes(),
                                      content_type='text/css; charset=utf-8')
                if path == '/api/health':
                    return self._send(200, {'service': 'voice-portal', 'ready': True})
                session = self._session()
                if path == '/api/session':
                    return self._send(200, {'csrf': session['csrf'],
                                            'index_fingerprint': portal.store.index_fingerprint,
                                            'expires_in': max(0, session['expires_at'] - portal._now())})
                if path == '/api/task':
                    return self._send(200, portal.task(session))
                if path == '/api/history':
                    return self._send(200, portal.history(session))
                return self._send(404, {'error': 'not_found'})
            except PortalError as exc:
                return self._send(exc.code, {'error': exc.message})
            except (OSError, ValueError):
                return self._send(500, {'error': '服务请求失败'})

        def do_POST(self):
            self._cookie_refresh = None
            try:
                path = urllib.parse.urlsplit(self.path).path
                if path == '/api/login':
                    body = json.loads(self._body(MAX_JSON))
                    sid, csrf = portal.login(body.get('token'))
                    cookie = f'vp_sid={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}'
                    if secure_cookie:
                        cookie += '; Secure'
                    return self._send(200, {'csrf': csrf}, extra={'Set-Cookie': cookie})
                if path == '/api/logout':
                    cookie = SimpleCookie(self.headers.get('Cookie', ''))
                    morsel = cookie.get('vp_sid')
                    revoked = portal.logout(morsel.value) if morsel is not None else {
                        'revoked': False, 'active_task_id': None}
                    clear = 'vp_sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
                    if secure_cookie:
                        clear += '; Secure'
                    return self._send(200, {'status': 'logged_out', **revoked},
                                      extra={'Set-Cookie': clear})
                session = self._session()
                self._csrf(session)
                if path == '/api/transcribe':
                    if (self.headers.get('Content-Type') or '').split(';', 1)[0] != 'audio/wav':
                        raise PortalError(415, '仅接受 audio/wav')
                    if self.headers.get('X-ASR-Consent', '').lower() != 'true':
                        raise PortalError(403, '需要明确授权')
                    capture = self.headers.get('X-Capture-Kind', 'push_to_talk_microphone_then_offline')
                    return self._send(200, portal.transcribe(session, self._body(MAX_AUDIO), capture))
                body = json.loads(self._body(MAX_JSON))
                if path == '/api/retitle':
                    return self._send(200, portal.retitle(session, body.get('text')))
                if path == '/api/seasons':
                    return self._send(200, portal.seasons(session, body.get('work_id')))
                if path == '/api/episodes':
                    return self._send(200, portal.episodes(session, body.get('work_id'), body.get('season')))
                if path == '/api/select':
                    return self._send(200, portal.select(session, body.get('work_id'),
                                                         body.get('season'), body.get('episode')))
                if path == '/api/confirm':
                    return self._send(200, portal.confirm(session, body.get('confirm')))
                if path == '/api/cancel':
                    return self._send(200, portal.cancel(session))
                return self._send(404, {'error': 'not_found'})
            except PortalError as exc:
                return self._send(exc.code, {'error': exc.message})
            except json.JSONDecodeError:
                return self._send(400, {'error': '请求格式无效'})
            except (ValueError, TypeError, KeyError) as exc:
                return self._send(400, {'error': str(exc) or '请求格式无效'})
            except OSError:
                return self._send(500, {'error': '服务请求失败'})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    portal.store.start_cleanup()
    original_close = server.server_close

    def close():
        portal.store.stop_cleanup()
        original_close()

    server.server_close = close
    if certfile or keyfile:
        if not (certfile and keyfile):
            raise ValueError('certfile and keyfile must be supplied together')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--index', help='Real MediaIndex cache; opened read-only')
    source.add_argument('--root', action='append', help='Read-only media root(s)')
    parser.add_argument('--path-map', help='JSON prefix map for container cache paths')
    parser.add_argument('--aliases', help='Reviewed alias JSON mapping')
    parser.add_argument('--lab-url', default=os.environ.get('ASR_LAB_URL', 'http://127.0.0.1:8765'))
    parser.add_argument('--lab-token-file')
    parser.add_argument('--console-url', default=os.environ.get('CONSOLE_URL', 'http://127.0.0.1:8770'))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', required=True, help='TLS certificate PEM')
    parser.add_argument('--key', required=True, help='TLS private key PEM')
    parser.add_argument('--allow-insecure-http', action='store_true',
                        help='Only for localhost automated tests; never for phone access')
    args = parser.parse_args(argv)
    try:
        from poc.asr_lab_server import load_token
        lab_token = load_token(args.lab_token_file)
        aliases = json.loads(Path(args.aliases).read_text()) if args.aliases else None
        path_map = json.loads(args.path_map) if args.path_map else None
        store = (TitleSessionStore.from_cache(args.index, path_map=path_map, aliases=aliases)
                 if args.index else TitleSessionStore.from_roots(args.root, aliases=aliases))
        portal = Portal(store=store, lab_url=args.lab_url, lab_token=str(lab_token),
                        console_url=args.console_url)
        if args.allow_insecure_http:
            if args.host not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('insecure HTTP is only allowed on localhost')
            server = create_server(portal, host=args.host, port=args.port, secure_cookie=False)
        else:
            server = create_server(portal, host=args.host, port=args.port,
                                   certfile=args.cert, keyfile=args.key, secure_cookie=True)
    except Exception as exc:
        print(f'VOICE_PORTAL_NOT_READY: {exc}', file=sys.stderr)
        return 2
    print(f'VOICE_PORTAL_READY https={not args.allow_insecure_http} host={args.host} port={args.port}',
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())