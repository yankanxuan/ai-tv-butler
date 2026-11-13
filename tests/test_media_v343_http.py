"""HTTP-level V3.4.3 acceptance for restarts and readiness."""
import http.client
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from adbtv.media_server import MediaServer


class HttpReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'library'
        self.root.mkdir()
        self.file = self.root/'film.mp4'
        self.file.write_bytes(b'0123456789'*100)
        self.state = Path(self.tmp.name)/'state'
        self.server = MediaServer([str(self.root)], host='127.0.0.1', state_dir=self.state).start()
        self.addCleanup(lambda: self.server.stop())

    def request(self, path, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=3)
        c.request('GET', path, headers=headers or {})
        r = c.getresponse(); body=r.read(); c.close()
        return r.status, body

    def test_live_service_restart_accepts_previous_signed_range(self):
        url = self.server.url_for(str(self.file))
        parsed=urlsplit(url)
        path=parsed.path+'?'+parsed.query
        self.assertEqual(self.request('/film.mp4')[0],401)
        self.assertEqual(self.request(path, {'Range':'bytes=10-19'}), (206,b'0123456789'))
        port=self.server.port
        self.server.stop()
        self.server=MediaServer([str(self.root)], host='127.0.0.1', port=port,state_dir=self.state).start()
        self.assertEqual(self.request(path, {'Range':'bytes=10-19'}), (206,b'0123456789'))

    def test_readiness_rejects_unreadable_directory(self):
        self.root.chmod(0o000)
        try:
            self.assertEqual(self.request('/readyz')[0],503)
            self.assertEqual(self.request('/livez')[0],200)
        finally:
            self.root.chmod(0o700)

    def test_readiness_rejects_unreadable_key(self):
        with patch.object(Path,'read_bytes',side_effect=PermissionError('denied')):
            self.assertEqual(self.request('/readyz')[0],503)

    def test_docker_health_command_fails_if_real_root_is_missing(self):
        import os, yaml
        config=Path(self.tmp.name)/'config.yaml'
        config.write_text(yaml.safe_dump({'media':{'advertise_host':'127.0.0.1','serve_port':self.server.port}}))
        env=dict(os.environ, AI_TV_CONFIG=str(config), AI_TV_DATA_DIR=self.tmp.name)
        command=[sys.executable,'scripts/healthcheck.py','media']
        before=subprocess.run(command,env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(before.returncode,0,before.stderr)
        self.file.unlink();self.root.rmdir()
        after=subprocess.run(command,env=env,capture_output=True,text=True,timeout=5)
        self.assertNotEqual(after.returncode,0)
        self.assertNotIn(str(self.root),after.stdout)

    def test_fn_os_acl_access_is_not_denied_by_mode_bits(self):
        import os, stat
        original = Path.stat
        root = self.root
        def acl_stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == root:
                fields = list(value)
                fields[0] = stat.S_IFDIR
                return os.stat_result(fields)
            return value
        with patch.object(Path, 'stat', acl_stat), patch('os.access', return_value=True):
            self.assertTrue(self.server.readiness()['ok'])
