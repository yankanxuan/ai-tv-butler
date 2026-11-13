"""Persistent idempotency: replay, ownership, recovery, retention and DB faults."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from adbtv.idempotency import IdempotencyConflict, IdempotencyStore


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task_dir = self.root / 'tasks'
        self.store = IdempotencyStore(self.root / 'idempotency.sqlite3', self.task_dir)

    def task(self, task_id):
        path = self.task_dir / (task_id + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'id': task_id, 'state': 'QUEUED'}))
        return path

    def test_same_key_replays_without_second_task(self):
        calls = []
        def create(task_id):
            calls.append(task_id); self.task(task_id)
        first, replay = self.store.submit('owner', 'key-00000001', 'a' * 64, create)
        second, replay2 = self.store.submit('owner', 'key-00000001', 'a' * 64, create)
        self.assertEqual(first, second)
        self.assertFalse(replay); self.assertTrue(replay2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get('owner', 'key-00000001')['state'], 'committed')

    def test_key_reuse_for_other_command_is_rejected(self):
        self.store.submit('owner', 'key-00000002', 'a' * 64, lambda task_id: self.task(task_id))
        with self.assertRaises(IdempotencyConflict):
            self.store.submit('owner', 'key-00000002', 'b' * 64, lambda task_id: self.task(task_id))

    def test_pending_record_recovers_when_task_file_appears(self):
        self.store.submit('owner', 'key-00000003', 'a' * 64, lambda task_id: self.task(task_id))
        # Simulate a crash after the task JSON was written but before committed:
        connection = sqlite3.connect(self.store.path)
        connection.execute("UPDATE idempotency SET state='pending' WHERE key='key-00000003'")
        connection.commit(); connection.close()
        called = []
        task_id, replay = self.store.submit('owner', 'key-00000003', 'a' * 64,
                                             lambda value: called.append(value))
        self.assertTrue(replay)
        self.assertEqual(called, [])
        self.assertEqual(self.store.get('owner', 'key-00000003')['state'], 'committed')
        self.assertTrue((self.task_dir / (task_id + '.json')).is_file())

    def test_failed_create_rolls_back_and_retry_can_create(self):
        def fail(task_id):
            raise RuntimeError('filesystem failed')
        with self.assertRaises(RuntimeError):
            self.store.submit('owner', 'key-00000004', 'a' * 64, fail)
        self.assertIsNone(self.store.get('owner', 'key-00000004'))

    def test_owner_isolation(self):
        a, _ = self.store.submit('owner-a', 'key-00000005', 'a' * 64,
                                 lambda task_id: self.task(task_id))
        b, _ = self.store.submit('owner-b', 'key-00000005', 'a' * 64,
                                 lambda task_id: self.task(task_id))
        self.assertNotEqual(a, b)
        self.assertEqual(self.store.get('owner-a', 'key-00000005')['task_id'], a)

    def test_retention_purges_committed_but_not_pending(self):
        self.store.submit('owner', 'key-00000006', 'a' * 64,
                          lambda task_id: self.task(task_id))
        old = IdempotencyStore(self.root / 'idempotency.sqlite3', self.task_dir,
                               retention_seconds=3600, now=lambda: 10 ** 12)
        old.list('owner')
        self.assertIsNone(old.get('owner', 'key-00000006'))

    def test_database_fault_fails_closed(self):
        target = self.root / 'bad.sqlite3'
        target.mkdir()
        with self.assertRaises(sqlite3.Error):
            IdempotencyStore(target, self.task_dir)


if __name__ == '__main__':
    unittest.main()
