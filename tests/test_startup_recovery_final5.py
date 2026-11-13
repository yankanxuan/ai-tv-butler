"""Startup recovery: QUEUED may be requeued; RUNNING must never be replayed."""
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from adbtv.config import DEFAULTS
from adbtv.execution import TaskControl, atomic_json, runtime_dir
from adbtv.idempotency import IdempotencyStore
from adbtv.server import make_server


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['adb'].update(serial='mock-tv', fake_state=str(Path(self.tmp.name) / 'tv.json'))
        self.cfg['execution']['state_dir'] = self.tmp.name
        self.cfg['scheduler']['store'] = str(Path(self.tmp.name) / 'schedule.json')
        self.cfg['memory'].update(enabled=False, path=str(Path(self.tmp.name) / 'memory.sqlite3'))
        self.cfg['server'].update(host='127.0.0.1', port=0, queue_size=4, workers=1)
        self.calls = []
        self.patch = patch('adbtv.server.build_context', side_effect=self.context)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def context(self, cfg):
        agent = Mock()
        agent.run.side_effect = lambda command, task_id=None: self.calls.append((command, task_id)) or {
            'ok': True, 'status': 'SUCCESS'}
        return (Mock(), Mock(info=lambda: {'model': 'mock'}), Mock(), agent, Mock(tasks=[]))

    def dead_queued(self, command='打开飞牛', task_id='recoverqueued01',
                    instance_id='dead-instance', owner_pid=1):
        task = TaskControl(self.cfg, command, task_id=task_id)
        record = json.loads(task.path.read_text())
        record.update(owner_pid=owner_pid, server_instance_id=instance_id, state='QUEUED')
        atomic_json(task.path, record)
        return task, record

    def start(self):
        self.server = make_server(self.cfg)
        self.addCleanup(self.close)

    def close(self):
        # These tests do not start serve_forever; shutdown() would wait forever.
        self.server.server_close()

    def wait_state(self, task_id, wanted, timeout=3):
        path = runtime_dir(self.cfg) / 'tasks' / (task_id + '.json')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                time.sleep(.01); continue
            if record.get('state') == wanted:
                return record
            time.sleep(.01)
        self.fail(f'{task_id} did not reach {wanted}')

    def test_committed_idempotent_queued_task_is_replayed_once(self):
        task, _ = self.dead_queued('打开飞牛', 'recoverqueued02')
        store = IdempotencyStore(runtime_dir(self.cfg) / 'idempotency.sqlite3',
                                 runtime_dir(self.cfg) / 'tasks')
        store.submit('owner', 'recover-key-0002', 'a' * 64, lambda task_id: None)
        # The record must point at the existing task id, as production would.
        import sqlite3
        connection = sqlite3.connect(store.path)
        connection.execute("UPDATE idempotency SET task_id=? WHERE key='recover-key-0002'", (task.id,))
        connection.commit(); connection.close()
        self.start()
        record = self.wait_state(task.id, 'SUCCEEDED')
        self.assertEqual(record['id'], task.id)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(store.get('owner', 'recover-key-0002')['task_id'], task.id)
        self.assertEqual(store.get('owner', 'recover-key-0002')['state'], 'committed')

    def test_running_task_is_marked_interrupted_and_not_replayed(self):
        task, record = self.dead_queued('打开飞牛', 'recoverrunning03')
        record.update(state='RUNNING')
        atomic_json(task.path, record)
        self.start()
        final = self.wait_state(task.id, 'INTERRUPTED')
        self.assertEqual(final['result']['status'], 'UNKNOWN')
        self.assertEqual(self.calls, [])

    def test_terminal_task_is_not_replayed(self):
        task, record = self.dead_queued('打开飞牛', 'recoverdone04')
        record.update(state='SUCCEEDED', result={'ok': True, 'status': 'SUCCESS'})
        atomic_json(task.path, record)
        self.start()
        time.sleep(.15)
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(task.path.read_text())['state'], 'SUCCEEDED')

    def legacy_queued(self, task_id, *, state='QUEUED', recovery_pid=1):
        task, record = self.dead_queued('打开飞牛', task_id, instance_id='legacy-console')
        record.update(state=state, recovery_owner_pid=recovery_pid)
        record.pop('recovery_owner_instance_id', None)
        atomic_json(task.path, record)
        return task, record

    def hold_lease(self, name):
        instances = runtime_dir(self.cfg) / 'instances'
        instances.mkdir(parents=True, exist_ok=True)
        fd = os.open(instances / (name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
        return fd

    def hold_recovery_leader(self):
        runtime = runtime_dir(self.cfg)
        runtime.mkdir(parents=True, exist_ok=True)
        fd = os.open(runtime / 'recovery-leader.lock', os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
        return fd

    def test_non_leader_does_not_migrate_legacy_task(self):
        self.hold_recovery_leader()
        task, _ = self.legacy_queued('legacynonleader1')
        server = make_server(self.cfg)  # recovery_fd cannot be acquired
        try:
            time.sleep(.2)
            record = json.loads(task.path.read_text())
            self.assertEqual(record['state'], 'QUEUED')
            self.assertNotIn('migration', record)
            self.assertEqual(self.calls, [])
        finally:
            server.server_close()

    def test_live_original_instance_protects_legacy_task(self):
        self.hold_lease('liveoriginal')
        task, _ = self.legacy_queued('legacyliveorig1')
        record = json.loads(task.path.read_text())
        record['server_instance_id'] = 'liveoriginal'
        atomic_json(task.path, record)
        self.start()
        time.sleep(.2)
        self.assertEqual(json.loads(task.path.read_text())['state'], 'QUEUED')
        self.assertEqual(self.calls, [])

    def test_live_recovery_instance_protects_new_format_task(self):
        self.hold_lease('liverecovery')
        task, _ = self.dead_queued('打开飞牛', 'recoveryliveowner1',
                                   instance_id='dead-server')
        record = json.loads(task.path.read_text())
        record.update(recovery_owner_pid=1, recovery_owner_instance_id='liverecovery')
        atomic_json(task.path, record)
        self.start()
        time.sleep(.2)
        self.assertEqual(json.loads(task.path.read_text())['state'], 'QUEUED')
        self.assertEqual(self.calls, [])

    def test_legacy_recovery_pid_without_instance_is_not_replayed(self):
        # Old records only have recovery_owner_pid. PID 1 may be reused by the
        # new container, so the only safe migration is INTERRUPTED/UNKNOWN.
        queued, _ = self.legacy_queued('legacypid1queued')
        running, _ = self.legacy_queued('legacypid1running', state='RUNNING')
        self.start()
        for task in (queued, running):
            final = self.wait_state(task.id, 'INTERRUPTED')
            self.assertEqual(final['result']['status'], 'UNKNOWN')
            self.assertEqual(final['id'], task.id)
            self.assertEqual(final['migration']['reason'],
                             'legacy_recovery_without_instance_lease')
            self.assertEqual(final['migration']['original_recovery_owner_pid'], 1)
        self.assertEqual(self.calls, [])

    def test_legacy_migration_is_stable_across_repeated_startup(self):
        task, _ = self.legacy_queued('legacyrepeat01')
        first = make_server(self.cfg)
        first.server_close()
        second = make_server(self.cfg)
        try:
            final = self.wait_state(task.id, 'INTERRUPTED')
            time.sleep(.15)
            self.assertEqual(final['id'], task.id)
            self.assertEqual(self.calls, [])
        finally:
            second.server_close()

    def test_legacy_migration_keeps_idempotency_record(self):
        task, _ = self.legacy_queued('legacyidem01')
        store = IdempotencyStore(runtime_dir(self.cfg) / 'idempotency.sqlite3',
                                 runtime_dir(self.cfg) / 'tasks')
        store.submit('owner', 'legacy-key-01', 'a' * 64, lambda value: None)
        import sqlite3
        connection = sqlite3.connect(store.path)
        connection.execute("UPDATE idempotency SET task_id=? WHERE key='legacy-key-01'", (task.id,))
        connection.commit(); connection.close()
        self.start()
        final = self.wait_state(task.id, 'INTERRUPTED')
        self.assertEqual(final['id'], task.id)
        self.assertEqual(store.get('owner', 'legacy-key-01')['task_id'], task.id)
        self.assertEqual(self.calls, [])

    def test_pid_1_reuse_does_not_make_dead_instance_live(self):
        # Docker may reuse PID 1 for the new console. Instance leases, not PIDs,
        # decide whether the old QUEUED/RUNNING owner is alive.
        queued, _ = self.dead_queued('打开飞牛', 'recoverpid1queued', owner_pid=1)
        running, record = self.dead_queued('打开飞牛', 'recoverpid1running', owner_pid=1)
        record.update(state='RUNNING'); atomic_json(running.path, record)
        self.start()
        self.assertEqual(self.wait_state(queued.id, 'SUCCEEDED')['id'], queued.id)
        self.assertEqual(self.wait_state(running.id, 'INTERRUPTED')['result']['status'], 'UNKNOWN')
        self.assertEqual(len(self.calls), 1)

    def test_only_one_concurrent_console_recovers_one_queued_task(self):
        task, _ = self.dead_queued('打开飞牛', 'recoverconcurrent1')
        first = make_server(self.cfg)
        second = make_server(self.cfg)
        try:
            self.assertEqual(self.wait_state(task.id, 'SUCCEEDED')['id'], task.id)
            time.sleep(.2)
            self.assertEqual(len(self.calls), 1)
        finally:
            first.server_close(); second.server_close()

    def test_live_instance_lease_blocks_recovery_until_abnormal_exit(self):
        runtime = runtime_dir(self.cfg)
        instances = runtime / 'instances'; instances.mkdir(parents=True, exist_ok=True)
        lease = instances / 'live-child.lock'
        code = '''import fcntl,os,sys,time
p=sys.argv[1]
fd=os.open(p,os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
print("ready",flush=True)
time.sleep(30)
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(lease)],
                                 stdout=subprocess.PIPE, text=True)
        def cleanup_child():
            if child.poll() is None:
                child.kill()
            try:
                child.stdout.close()
            except OSError:
                pass
        self.addCleanup(cleanup_child)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        task, _ = self.dead_queued('打开飞牛', 'recoverabnormal1',
                                   instance_id='live-child', owner_pid=child.pid)
        first = make_server(self.cfg)
        try:
            time.sleep(.25)
            self.assertEqual(self.calls, [])
            self.assertEqual(json.loads(task.path.read_text())['state'], 'QUEUED')
        finally:
            first.server_close()
        child.terminate(); child.wait(timeout=3)
        second = make_server(self.cfg)
        try:
            self.assertEqual(self.wait_state(task.id, 'SUCCEEDED')['id'], task.id)
            self.assertEqual(len(self.calls), 1)
        finally:
            second.server_close()

    def test_missing_task_for_committed_record_is_not_created(self):
        store = IdempotencyStore(runtime_dir(self.cfg) / 'idempotency.sqlite3',
                                 runtime_dir(self.cfg) / 'tasks')
        task_id, _ = store.submit('owner', 'recover-key-0005', 'a' * 64,
                                  lambda task_id: None)
        self.start()
        time.sleep(.15)
        self.assertEqual(self.calls, [])
        self.assertFalse((runtime_dir(self.cfg) / 'tasks' / (task_id + '.json')).exists())


if __name__ == '__main__':
    unittest.main()
