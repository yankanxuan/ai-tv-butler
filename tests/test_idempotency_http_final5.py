"""Console HTTP idempotency: replay, history, restart and failure boundaries."""
import concurrent.futures
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from adbtv.config import DEFAULTS
from adbtv.server import make_server

_UNSET = object()


class IdempotencyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['adb'].update(serial='mock-tv', fake_state=str(Path(self.tmp.name) / 'tv.json'))
        self.cfg['execution']['state_dir'] = self.tmp.name
        self.cfg['scheduler']['store'] = str(Path(self.tmp.name) / 'schedule.json')
        self.cfg['memory'].update(enabled=False, path=str(Path(self.tmp.name) / 'memory.sqlite3'))
        self.token = 'console-' + 'x' * 32
        self.cfg['server'].update(host='127.0.0.1', port=0, queue_size=4)
        self.cfg['server']['token'] = self.token
        self.calls = []
        self.context_patch = patch('adbtv.server.build_context', side_effect=self.context)
        self.context_patch.start()
        self.start()

    def context(self, cfg):
        agent = Mock()
        def run(command, task_id=None):
            self.calls.append((command, task_id))
            return {'ok': True, 'status': 'SUCCESS'}
        agent.run.side_effect = run
        return (Mock(), Mock(info=lambda: {'model': 'mock'}), Mock(), agent, Mock(tasks=[]))

    def start(self):
        self.server = make_server(self.cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def tearDown(self):
        self.stop(); self.context_patch.stop(); self.tmp.cleanup()

    def request(self, method, path, body=None, *, token=_UNSET, key=None):
        token = self.token if token is _UNSET else token
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {}
        if token: headers['X-Auth-Token'] = token
        if key: headers['X-Idempotency-Key'] = key
        data = None
        if body is not None:
            data = json.dumps(body); headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse(); raw = response.read(); conn.close()
        return response.status, (json.loads(raw) if raw else {})

    def submit(self, command='打开飞牛', key='idem-key-00000001'):
        return self.request('POST', '/api/runs', {'command': command}, key=key)

    def wait(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.calls:
                return
            time.sleep(.01)
        self.fail('task did not run')

    def test_same_key_replays_task_and_is_queryable(self):
        first = self.submit(); second = self.submit()
        self.assertEqual(first[0], 202); self.assertEqual(second[0], 202)
        self.assertEqual(first[1]['task_id'], second[1]['task_id'])
        self.assertTrue(second[1]['idempotent'])
        self.wait()
        status, record = self.request('GET', '/api/idempotency/idem-key-00000001')
        self.assertEqual(status, 200)
        self.assertEqual(record['record']['task_id'], first[1]['task_id'])
        self.assertIn(record['record']['task_state'], ('QUEUED', 'RUNNING', 'SUCCEEDED', 'SUCCESS'))
        self.assertEqual(len(self.calls), 1)
        status, listing = self.request('GET', '/api/idempotency')
        self.assertEqual(status, 200); self.assertEqual(len(listing['records']), 1)

    def test_two_concurrent_requests_with_same_key_create_one_task(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.submit) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual([result[0] for result in results], [202, 202])
        self.assertEqual({result[1]['task_id'] for result in results},
                         {results[0][1]['task_id']})
        self.assertTrue(any(result[1]['idempotent'] for result in results))
        self.wait()
        self.assertEqual(len(self.calls), 1)

    def test_reuse_for_different_command_is_conflict(self):
        self.submit('打开飞牛')
        status, body = self.submit('播放三体')
        self.assertEqual(status, 409)
        self.assertIn('different command', body['error'])

    def test_old_client_without_key_still_works_and_is_not_listed(self):
        status, body = self.request('POST', '/api/runs', {'command': '打开飞牛'})
        self.assertEqual(status, 202); self.assertFalse(body['idempotent'])
        status, listing = self.request('GET', '/api/idempotency')
        self.assertEqual(listing['records'], [])

    def test_restart_preserves_same_task_id(self):
        first = self.submit()
        self.wait()
        self.stop(); self.start()
        second = self.submit()
        self.assertEqual(second[0], 202)
        self.assertEqual(second[1]['task_id'], first[1]['task_id'])
        self.assertTrue(second[1]['idempotent'])
        self.assertEqual(len(self.calls), 1)

    def test_idempotency_endpoints_require_auth(self):
        self.assertEqual(self.request('GET', '/api/idempotency', token=None)[0], 401)
        self.assertEqual(self.request('GET', '/api/idempotency/no-such-key', token=None)[0], 401)

    def test_database_fault_prevents_task_creation(self):
        self.stop()
        db = Path(self.tmp.name) / 'idempotency.sqlite3'
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists(): db.unlink()
        db.mkdir()
        with self.assertRaises(Exception):
            make_server(self.cfg)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()


class MissingTaskViewTests(unittest.TestCase):
    def test_committed_record_without_task_file_is_unknown(self):
        import copy as _copy
        import tempfile
        from adbtv.idempotency import IdempotencyStore
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _copy.deepcopy(DEFAULTS)
            cfg['adb'].update(serial='mock-tv', fake_state=str(Path(tmp) / 'tv.json'))
            cfg['execution']['state_dir'] = tmp
            cfg['scheduler']['store'] = str(Path(tmp) / 'schedule.json')
            cfg['memory'].update(enabled=False, path=str(Path(tmp) / 'memory.sqlite3'))
            cfg['server'].update(host='127.0.0.1', port=0, queue_size=2)
            cfg['server']['token'] = 'console-' + 'x' * 32
            store = IdempotencyStore(Path(tmp) / 'idempotency.sqlite3', Path(tmp) / 'tasks')
            task_id, _ = store.submit(IdempotencyStore.subject(cfg['server']['token']),
                                      'missing-key-01', 'a' * 64, lambda value: None)
            with patch('adbtv.server.build_context', return_value=(Mock(), Mock(), Mock(),
                                                                    Mock(tasks=[]), Mock(tasks=[]))):
                server = make_server(cfg)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                port = server.server_address[1]
                client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                client.request('GET', f'/api/idempotency/missing-key-01',
                               headers={'X-Auth-Token': cfg['server']['token']})
                response = client.getresponse(); data = json.loads(response.read()); client.close()
                server.shutdown(); server.server_close(); thread.join(3)
            self.assertEqual(response.status, 200)
            self.assertEqual(data['record']['task_state'], 'UNKNOWN')
            self.assertEqual(data['record']['task_id'], task_id)
