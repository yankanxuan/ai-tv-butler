"""V3.4.3 regressions: persistent auth, readiness and LAN discovery."""
import concurrent.futures
import http.client
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from adbtv.media_server import MediaAuth, MediaServer, MediaLinks


def initialize_key(args):
    directory, gate = args
    gate.wait(20)
    auth = MediaAuth(Path(directory) / 'key', Path(directory) / 'sessions.json')
    return auth._secret.hex()


class KeyReliabilityTests(unittest.TestCase):
    def test_binary_whitespace_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            raw = b'\n' + b'x' * 30 + b' '
            key.write_bytes(raw)
            self.assertEqual(MediaAuth(key, Path(tmp)/'sessions')._secret, raw)
            self.assertEqual(key.read_bytes(), raw)

    def test_invalid_length_fails_without_replacement(self):
        for length in (0, 31, 33):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as tmp:
                key = Path(tmp)/'key'
                key.write_bytes(b'x'*length)
                with self.assertRaises((OSError, ValueError)):
                    MediaAuth(key, Path(tmp)/'sessions')
                self.assertEqual(key.read_bytes(), b'x'*length)

    def test_write_failure_never_returns_an_unusable_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = MediaAuth(Path(tmp)/'key', Path(tmp)/'sessions')
            with patch('os.replace', side_effect=OSError('disk failed')):
                with self.assertRaises(OSError):
                    auth.open_session('film.mp4')

    def test_lock_failure_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('fcntl.flock', side_effect=OSError('lock failed')):
                with self.assertRaises(OSError):
                    MediaAuth(Path(tmp)/'key', Path(tmp)/'sessions')

    def test_16_process_initialization_for_10_rounds(self):
        import subprocess, sys
        code = '''import sys,time
from pathlib import Path
from adbtv.media_server import MediaAuth
root=Path(sys.argv[1])
while not (root/'go').exists(): time.sleep(.002)
a=MediaAuth(root/'key',root/'sessions')
print(a._secret.hex())
'''
        for iteration in range(10):
            with self.subTest(round=iteration), tempfile.TemporaryDirectory() as tmp:
                processes = []
                try:
                    processes = [subprocess.Popen([sys.executable, '-c', code, tmp],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        for _ in range(16)]
                    (Path(tmp)/'go').touch()
                    values = []
                    # A shared deadline, not a fresh 30 s for every child, keeps
                    # one slow import from multiplying the suite's wait time.
                    deadline = time.monotonic() + 45
                    for process in processes:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError(
                                f'media-key child startup exceeded shared deadline; '
                                f'{sum(1 for p in processes if p.poll() is None)} still alive')
                        try:
                            out, err = process.communicate(timeout=remaining)
                        except subprocess.TimeoutExpired as exc:
                            alive = [p.pid for p in processes if p.poll() is None]
                            raise AssertionError(
                                f'media-key child did not finish before shared deadline; '
                                f'alive={alive}; possible CPU starvation or lock deadlock') from exc
                        self.assertEqual(process.returncode, 0, err)
                        values.append(out.strip())
                    self.assertEqual(len(set(values)), 1)
                    self.assertEqual(bytes.fromhex(values[0]), (Path(tmp)/'key').read_bytes())
                finally:
                    # A failed communicate must never leave children holding the
                    # temp directory or competing with later rounds.
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                    for process in processes:
                        if process.poll() is None:
                            try:
                                process.communicate(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                        else:
                            try:
                                process.communicate(timeout=1)
                            except Exception:
                                pass

    def test_restart_preserves_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = MediaAuth(Path(tmp)/'key', Path(tmp)/'sessions')
            sid, signature = first.open_session('film.mp4')
            second = MediaAuth(Path(tmp)/'key', Path(tmp)/'sessions')
            self.assertTrue(second.touch('film.mp4', sid, signature))


class ReadinessTests(unittest.TestCase):
    def test_readiness_tracks_roots_and_key_without_path_disclosure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'library'
            root.mkdir()
            server = MediaServer([str(root)], host='127.0.0.1', state_dir=Path(tmp)/'state').start()
            self.addCleanup(server.stop)
            def get(path):
                c = http.client.HTTPConnection('127.0.0.1', server.port, timeout=3)
                c.request('GET', path)
                r = c.getresponse(); body = r.read(); c.close()
                return r.status, body
            self.assertEqual(get('/livez')[0], 200)
            status, body = get('/readyz')
            self.assertEqual(status, 200)
            self.assertNotIn(tmp.encode(), body)
            root.rmdir()
            self.assertEqual(get('/livez')[0], 200)
            self.assertEqual(get('/readyz')[0], 503)
            root.mkdir()
            server.auth.key_path.write_bytes(b'broken')
            self.assertEqual(get('/readyz')[0], 503)
