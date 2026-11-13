"""Persistent idempotency records for console task submission (stdlib SQLite).

The record is keyed by (authorization subject, client key). The subject is a
one-way hash of the console token, so one token cannot read another token's
records. A pending record plus the task JSON is reconciled on replay; a unique
key can never create two tasks.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid

RETENTION_SECONDS = 7 * 24 * 3600


class IdempotencyConflict(ValueError):
    """The same key was reused for a different command."""


class IdempotencyStore:
    def __init__(self, db_path, task_dir, *, retention_seconds=RETENTION_SECONDS,
                 now=time.time):
        self.path = Path(db_path).expanduser()
        self.task_dir = Path(task_dir).expanduser()
        self.retention_seconds = max(3600, int(retention_seconds))
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    @staticmethod
    def subject(token):
        if not isinstance(token, str) or not token:
            raise ValueError('authorization subject required')
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @staticmethod
    def validate_key(key):
        if (not isinstance(key, str) or not 8 <= len(key) <= 200
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise ValueError('invalid idempotency key')
        return key

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5,
                                     isolation_level=None)
        connection.execute('PRAGMA busy_timeout=5000')
        connection.execute('PRAGMA journal_mode=WAL')
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE IF NOT EXISTS idempotency (
                owner TEXT NOT NULL,
                key TEXT NOT NULL,
                task_id TEXT NOT NULL,
                command_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                PRIMARY KEY (owner, key)
            )''')
            connection.execute('CREATE INDEX IF NOT EXISTS idempotency_created '
                               'ON idempotency(created)')
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    def _task_path(self, task_id):
        return self.task_dir / (task_id + '.json')

    def _purge(self, connection):
        cutoff = self._now() - self.retention_seconds
        # Pending rows are reconciled first: a task file means the crash happened
        # after task creation; otherwise the reservation is safe to delete.
        pending = connection.execute(
            'SELECT owner, key, task_id FROM idempotency '
            'WHERE state = ? AND created < ?', ('pending', cutoff)).fetchall()
        for owner, key, task_id in pending:
            if self._task_path(task_id).is_file():
                connection.execute(
                    'UPDATE idempotency SET state=?, updated=? WHERE owner=? AND key=?',
                    ('committed', self._now(), owner, key))
            else:
                connection.execute(
                    'DELETE FROM idempotency WHERE owner=? AND key=?', (owner, key))
        connection.execute('DELETE FROM idempotency WHERE created < ? AND state = ?',
                           (cutoff, 'committed'))

    def submit(self, owner, key, command_hash, create_task):
        """Create or replay one task atomically enough to survive a crash.

        `create_task(task_id)` creates the task JSON and enqueues it; it is called
        while the SQLite write transaction is open. The caller must not notify
        workers until this returns successfully.
        """
        self.validate_key(key)
        if not isinstance(owner, str) or not owner:
            raise ValueError('owner required')
        if not isinstance(command_hash, str) or len(command_hash) < 16:
            raise ValueError('command hash required')
        connection = self._connect()
        replay = False
        created_id = None
        try:
            connection.execute('BEGIN IMMEDIATE')
            self._purge(connection)
            row = connection.execute(
                'SELECT task_id, command_hash, state, created FROM idempotency '
                'WHERE owner = ? AND key = ?', (owner, key)).fetchone()
            if row:
                task_id, recorded_hash, state, _ = row
                if recorded_hash != command_hash:
                    raise IdempotencyConflict('idempotency key reused for different command')
                replay = True
                if state != 'committed' and self._task_path(task_id).is_file():
                    connection.execute('UPDATE idempotency SET state=?, updated=? '
                                       'WHERE owner=? AND key=?',
                                       ('committed', self._now(), owner, key))
                if state == 'committed':
                    if not self._task_path(task_id).is_file():
                        raise RuntimeError('idempotent task record is missing')
                    connection.commit()
                    return task_id, True
                if self._task_path(task_id).is_file():
                    connection.execute('UPDATE idempotency SET state=?, updated=? '
                                       'WHERE owner=? AND key=?',
                                       ('committed', self._now(), owner, key))
                    connection.commit()
                    return task_id, True
                # Pending record whose task JSON was never created: reuse its id
                # and finish the reservation instead of creating a second task.
                create_task(task_id)
                connection.execute('UPDATE idempotency SET state=?, updated=? '
                                   'WHERE owner=? AND key=?',
                                   ('committed', self._now(), owner, key))
                connection.commit()
                return task_id, False
            task_id = uuid.uuid4().hex
            created_id = task_id
            connection.execute(
                'INSERT INTO idempotency(owner,key,task_id,command_hash,state,created,updated) '
                'VALUES(?,?,?,?,?,?,?)',
                (owner, key, task_id, command_hash, 'pending', self._now(), self._now()))
            create_task(task_id)
            connection.execute('UPDATE idempotency SET state=?, updated=? '
                               'WHERE owner=? AND key=?',
                               ('committed', self._now(), owner, key))
            connection.commit()
            return task_id, False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_committed(self, task_id):
        """Reconcile a task file created by a recovered queued task."""
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("UPDATE idempotency SET state='committed', updated=? WHERE task_id=? AND state='pending'",
                               (self._now(), task_id))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, owner, key):
        self.validate_key(key)
        connection = self._connect()
        try:
            row = connection.execute(
                'SELECT task_id, command_hash, state, created, updated '
                'FROM idempotency WHERE owner=? AND key=?', (owner, key)).fetchone()
            if row is None:
                return None
            return {'task_id': row[0], 'command_hash': row[1], 'state': row[2],
                    'created': row[3], 'updated': row[4]}
        finally:
            connection.close()

    def list(self, owner, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid limit')
        connection = self._connect()
        try:
            self._purge(connection)
            rows = connection.execute(
                'SELECT task_id, key, command_hash, state, created, updated '
                'FROM idempotency WHERE owner=? ORDER BY created DESC LIMIT ?',
                (owner, limit)).fetchall()
            return [{'task_id': row[0], 'key': row[1], 'command_hash': row[2],
                     'state': row[3], 'created': row[4], 'updated': row[5]}
                    for row in rows]
        finally:
            connection.close()