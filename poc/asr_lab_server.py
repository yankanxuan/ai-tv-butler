#!/usr/bin/env python3
"""Opt-in, isolated offline ASR HTTP laboratory. Never imports production routes."""
from __future__ import annotations

import argparse
import hmac
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import wave
import threading
import wave


class RequestRejected(ValueError):
    pass


def load_token(token_file=None):
    """Only ASR_LAB_* settings: no production config/token discovery."""
    configured = os.environ.get('ASR_LAB_TOKEN')
    token_file = token_file or os.environ.get('ASR_LAB_TOKEN_FILE')
    if configured and token_file:
        raise ValueError('Use ASR_LAB_TOKEN or an experiment token file, not both')
    if token_file:
        path = Path(token_file).expanduser()
        if path.name in ('console.token', 'media_key'):
            raise ValueError('Production token/key files are not experiment credentials')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Experiment token must be a regular file')
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('Experiment token file is too large')
        configured = raw.decode('ascii').strip()
    if (not isinstance(configured, str) or len(configured) < 32 or len(configured) > 4096
            or not configured.isascii() or any(c.isspace() for c in configured)):
        raise ValueError('An independent ASCII experiment token of at least 32 characters is required')
    return configured


def snapshot_audio(audio_root, relative, destination, max_bytes, max_frames):
    """Walk under an open root FD without following links, then make a bounded copy.

    The engine sees a private immutable-for-this-request snapshot, so swapping a
    writable host audio entry cannot redirect recognition outside the audio root.
    """
    if not isinstance(relative, str) or not relative or len(relative) > 1024:
        raise RequestRejected('A relative WAV path is required')
    parts = relative.split('/')
    if (relative.startswith('/') or '\\' in relative or '\x00' in relative
            or any(p in ('', '.', '..') for p in parts) or not relative.lower().endswith('.wav')):
        raise RequestRejected('Path must name a WAV strictly below audio_root')
    descriptor = os.open(audio_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(source_fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes or info.st_size <= 44:
                raise RequestRejected('Audio file is not a bounded regular WAV')
            payload = source.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise RequestRejected('Audio exceeds byte limit')
    finally:
        os.close(descriptor)
    destination.write_bytes(payload)
    try:
        with wave.open(str(destination), 'rb') as wav:
            if (wav.getcomptype() != 'NONE' or wav.getnchannels() != 1
                    or wav.getsampwidth() != 2 or wav.getframerate() != 16000):
                raise RequestRejected('WAV must be 16 kHz mono PCM16')
            frames = wav.getnframes()
            if not 0 < frames <= max_frames:
                raise RequestRejected('WAV frame count exceeds limit or is empty')
            if len(wav.readframes(frames)) != frames * 2:
                raise RequestRejected('Truncated WAV')
    except (wave.Error, EOFError) as exc:
        raise RequestRejected('Invalid PCM WAV') from exc
    return frames



def validate_uploaded_wav(payload, destination, max_frames):
    """Validate a browser-produced WAV before it reaches the fixed engine."""
    if not isinstance(payload, bytes) or len(payload) <= 44:
        raise RequestRejected('Uploaded WAV is empty or truncated')
    try:
        with wave.open(BytesIO(payload), 'rb') as wav:
            if (wav.getcomptype() != 'NONE' or wav.getnchannels() != 1
                    or wav.getsampwidth() != 2 or wav.getframerate() != 16000):
                raise RequestRejected('WAV must be 16 kHz mono PCM16')
            frames = wav.getnframes()
            if not 0 < frames <= max_frames:
                raise RequestRejected('WAV frame count exceeds limit or is empty')
            if len(wav.readframes(frames)) != frames * 2:
                raise RequestRejected('Truncated WAV')
    except (wave.Error, EOFError) as exc:
        raise RequestRejected('Invalid PCM WAV') from exc
    destination.write_bytes(payload)
    return frames


def create_server(manifest_path, audio_root, *, token, host='127.0.0.1', port=0,
                  title_cache=None, num_threads=2, vad=False, max_audio_bytes=2_000_000,
                  max_frames=960_000, engine_factory=None, suggester=None):
    """Load one fixed validated Engine before binding/advertising readiness."""
    if not token or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('Independent experiment token required')
    if (type(num_threads) is not int or not 1 <= num_threads <= 4
            or type(max_audio_bytes) is not int or not 44 < max_audio_bytes <= 2_000_000
            or type(max_frames) is not int or not 0 < max_frames <= 960_000):
        raise ValueError('Invalid ASR laboratory resource limits')
    root = Path(audio_root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('audio_root must be an existing directory, not a symlink')
    root = root.resolve()
    if engine_factory is None:
        from poc.asr_lab import Engine
        engine_factory = Engine
    engine = engine_factory(manifest_path, num_threads=num_threads, vad=vad)
    if title_cache is not None and suggester is None:
        from poc.title_candidates import TitleSuggester
        suggester = TitleSuggester.from_cache(title_cache)
    busy = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'ASRLab/1.1'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            # No transcripts, bearer tokens, host paths, or audio in request logs.
            pass

        def reply(self, code, value):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            try:
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_GET(self):
            if self.path != '/health':
                return self.reply(404, {'error': 'not_found'})
            self.reply(200, {'service': 'asr-lab', 'ready': True,
                             'capture_kind': 'offline_file_transcription'})

        def do_POST(self):
            if self.path not in ('/transcribe', '/transcribe-upload'):
                return self.reply(404, {'error': 'not_found'})
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + token).encode('ascii')):
                return self.reply(401, {'error': 'unauthorized'})
            if self.headers.get('Transfer-Encoding'):
                return self.reply(400, {'error': 'chunked_requests_not_supported'})
            upload = self.path == '/transcribe-upload'
            request = None
            payload = None
            capture_kind = 'offline_file_transcription'
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if upload:
                    media_type = (self.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
                    if media_type not in ('audio/wav', 'audio/x-wav'):
                        return self.reply(415, {'error': 'audio_wav_required'})
                    if self.headers.get('X-ASR-Consent', '').lower() != 'true':
                        return self.reply(403, {'error': 'explicit_consent_required'})
                    if not 44 < length <= max_audio_bytes:
                        # Drain a bounded body before 413 so a normal oversized
                        # client gets a response instead of a send-side reset.
                        self.close_connection = True
                        remaining = min(max(0, length), max_audio_bytes + 1024 * 1024)
                        try:
                            while remaining > 0:
                                chunk = self.rfile.read(min(65536, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        return self.reply(413, {'error': 'audio_size_limit'})
                    try:
                        payload = self.rfile.read(length)
                    except (BrokenPipeError, ConnectionResetError):
                        return self.reply(400, {'error': 'client_closed_early'})
                    if len(payload) != length:
                        raise RequestRejected('Truncated upload')
                    capture_kind = self.headers.get('X-Capture-Kind', 'uploaded_offline_wav')
                    if capture_kind not in ('uploaded_offline_wav', 'push_to_talk_microphone_then_offline',
                                            'offline_wav_input'):
                        return self.reply(400, {'error': 'invalid_capture_kind'})
                else:
                    if not 0 < length <= 8192:
                        return self.reply(413, {'error': 'request_body_limit'})
                    request = json.loads(self.rfile.read(length))
                    if not isinstance(request, dict) or request.get('consent') is not True:
                        return self.reply(403, {'error': 'explicit_consent_required'})
                    if set(request) - {'path', 'consent'}:
                        return self.reply(400, {'error': 'only_path_and_consent_allowed'})
            except (ValueError, UnicodeDecodeError):
                return self.reply(400, {'error': 'invalid_json_request'})
            except RequestRejected:
                return self.reply(400, {'error': 'invalid_or_unavailable_audio'})
            if not busy.acquire(blocking=False):
                return self.reply(409, {'error': 'busy', 'retryable': True})
            try:
                with tempfile.TemporaryDirectory(prefix='asr-lab-request-') as temporary:
                    snapshot = Path(temporary) / 'audio.wav'
                    if upload:
                        validate_uploaded_wav(payload, snapshot, max_frames)
                    else:
                        snapshot_audio(root, request.get('path'), snapshot, max_audio_bytes, max_frames)
                    result = engine.transcribe(snapshot)
                    if not isinstance(result, dict) or not isinstance(result.get('hypothesis'), str):
                        raise RuntimeError('Engine returned invalid result')
                    suggestions = suggester.suggest(result['hypothesis']) if suggester else {'enabled': False, 'candidates': []}
                    code, value = 200, {'asr': result, 'suggestions': suggestions,
                                        'capture_kind': capture_kind}
            except (RequestRejected, OSError):
                code, value = 400, {'error': 'invalid_or_unavailable_audio'}
            except Exception:
                code, value = 500, {'error': 'recognition_failed'}
            finally:
                busy.release()
            # Finish snapshot cleanup and release recognition before the client
            # can receive completion and immediately submit its next request.
            return self.reply(code, value)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.ready = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--audio-root', required=True)
    parser.add_argument('--title-cache')
    parser.add_argument('--token-file')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--num-threads', type=int, default=2)
    parser.add_argument('--vad', action='store_true')
    parser.add_argument('--max-audio-bytes', type=int, default=2_000_000)
    parser.add_argument('--max-frames', type=int, default=960_000)
    args = parser.parse_args(argv)
    try:
        token = load_token(args.token_file)
        server = create_server(args.manifest, args.audio_root, token=token, host=args.host,
                               port=args.port, title_cache=args.title_cache,
                               num_threads=args.num_threads, vad=args.vad,
                               max_audio_bytes=args.max_audio_bytes, max_frames=args.max_frames)
    except Exception:
        parser.exit(2, 'ASR_LAB_NOT_READY: experiment token, model validation/loading or input configuration failed\n')
    print('ASR_LAB_READY: isolated offline file transcription only', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
