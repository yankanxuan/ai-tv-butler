"""Offline restore verification; no Docker or production data access."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rollback_drill_b8_test', ROOT / 'scripts/rollback_drill_b8.py')
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class RestoreCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'source'
        self.data.mkdir()
        self.manifest = self.root / 'manifest-private.json'
        self.key = b'\n' + b'x' * 30 + b' '
        self.manifest.write_text(json.dumps({'media_key_sha256_before': hashlib.sha256(self.key).hexdigest()}))
        self.config('memory: {enabled: true}\n')
        (self.data / 'media_key').write_bytes(self.key)
        self.database('memory.sqlite3')

    def config(self, text):
        (self.data / 'config.yaml').write_text(text)

    def database(self, relative):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE evidence (value TEXT)')
            db.execute('INSERT INTO evidence VALUES (?)', ('restored',))
        return path

    def pack(self):
        archive = self.root / 'data-private.tar.gz'
        with tarfile.open(archive, 'w:gz', dereference=False) as tar:
            tar.add(self.data, arcname='data')
        return archive

    def check(self):
        self.pack()
        return drill.restore_check(self.manifest)

    def test_normal_backup_and_binary_key(self):
        report = self.check()
        self.assertTrue(report['config_restorable'])
        self.assertTrue(report['media_key_restorable'])
        self.assertEqual(report['required_files_found'], ['config.yaml'])
        self.assertEqual(report['sqlite_files_in_backup'], ['memory.sqlite3'])
        self.assertEqual(report['sqlite_integrity_check'], 'ok')
        self.assertEqual((self.data / 'media_key').read_bytes(), self.key)

    def test_configured_database_without_sqlite_extension(self):
        self.config('memory: {enabled: false, path: private/store.db}\n')
        self.database('private/store.db')
        (self.data / 'memory.sqlite3').write_bytes(b'irrelevant corrupt decoy')
        self.assertEqual(self.check()['sqlite_files_in_backup'], ['private/store.db'])

    def test_default_database_follows_execution_state_dir(self):
        self.config('execution: {state_dir: custom/runtime}\n')
        self.database('custom/memory.sqlite3')
        (self.data / 'custom/media_key').write_bytes(self.key)
        (self.data / 'media_key').write_bytes(b'invalid decoy')
        self.assertEqual(self.check()['sqlite_files_in_backup'], ['custom/memory.sqlite3'])

    def test_absolute_container_path_maps_into_restored_data(self):
        self.config('memory: {path: /data/nested/memory.db}\n')
        self.database('nested/memory.db')
        self.assertEqual(self.check()['sqlite_files_in_backup'], ['nested/memory.db'])

    def test_host_environment_cannot_override_archive_config(self):
        with patch.dict(os.environ, {'AI_TV_CONFIG': '/missing/config',
                                     'AI_TV_DATA_DIR': '/unrelated/data',
                                     'AI_TV_ADB_PORT': 'invalid'}):
            self.assertEqual(self.check()['sqlite_integrity_check'], 'ok')

    def test_missing_archive(self):
        with self.assertRaisesRegex(RuntimeError, 'data-private.tar.gz missing'):
            drill.restore_check(self.manifest)

    def test_missing_config(self):
        (self.data / 'config.yaml').unlink()
        with self.assertRaisesRegex(RuntimeError, 'config.yaml missing'):
            self.check()

    def test_invalid_configuration_is_not_replaced_by_defaults(self):
        for text in ('', '{}', '[]', 'memory: [', 'memory: broken', 'execution: []'):
            with self.subTest(text=text):
                self.config(text)
                with self.assertRaisesRegex(RuntimeError, 'config.yaml invalid'):
                    self.check()

    def test_missing_database_fails_even_with_valid_decoy(self):
        self.config('memory: {path: actual.db}\n')
        with self.assertRaisesRegex(RuntimeError, 'Configured SQLite missing'):
            self.check()
        self.assertFalse((self.data / 'actual.db').exists())

    def test_missing_default_database_also_fails_when_memory_disabled(self):
        self.config('memory: {enabled: false}\n')
        (self.data / 'memory.sqlite3').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Configured SQLite missing'):
            self.check()

    def test_database_outside_backup_is_rejected(self):
        for path in ('/external/memory.db', '/data/../external/memory.db', '../outside.db'):
            with self.subTest(path=path):
                self.config('memory: {path: ' + path + '}\n')
                with self.assertRaisesRegex(RuntimeError, 'outside backed-up /data'):
                    self.check()

    def test_corrupt_or_empty_sqlite_fails(self):
        for payload in (b'', b'not a database', b'SQLite format 3\x00' + b'\x00' * 1024):
            with self.subTest(payload=payload[:16]):
                (self.data / 'memory.sqlite3').write_bytes(payload)
                with self.assertRaisesRegex(RuntimeError, 'SQLite'):
                    self.check()

    def test_integrity_check_detects_corrupt_pages_after_valid_header(self):
        path = self.data / 'memory.sqlite3'
        content = bytearray(path.read_bytes())
        content[100] = 0  # Invalid sqlite_schema b-tree page type.
        path.write_bytes(content)
        with self.assertRaisesRegex(RuntimeError, 'integrity_check failed'):
            self.check()

    def test_integrity_check_non_ok_result_fails(self):
        connection = unittest.mock.MagicMock()
        connection.execute.return_value.fetchall.return_value = [('page corruption',)]
        with patch.object(drill.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'integrity_check failed'):
                self.check()
        connection.close.assert_called_once()

    def test_sqlite_is_opened_read_only(self):
        real_connect = sqlite3.connect
        with patch.object(drill.sqlite3, 'connect', wraps=real_connect) as connect:
            self.check()
        self.assertTrue(connect.call_args.args[0].endswith('?mode=ro'))
        self.assertTrue(connect.call_args.kwargs['uri'])

    def test_missing_or_wrong_length_key(self):
        path = self.data / 'media_key'
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'media_key missing or invalid length'):
            self.check()
        for length in (0, 31, 33):
            with self.subTest(length=length):
                path.write_bytes(b'x' * length)
                with self.assertRaisesRegex(RuntimeError, 'expected 32 bytes'):
                    self.check()

    def test_configured_media_key_path_ignores_root_decoy(self):
        for state in ('/data/private', '~/private', '../data/private'):
            with self.subTest(state=state):
                self.config('media: {state_dir: ' + state + '}\n')
                private = self.data / 'private'
                private.mkdir(exist_ok=True)
                (private / 'media_key').write_bytes(self.key)
                (self.data / 'media_key').write_bytes(b'invalid decoy')
                self.assertTrue(self.check()['media_key_restorable'])

    def test_missing_or_invalid_configured_media_key_cannot_use_decoy(self):
        self.config('media: {state_dir: /data/private}\n')
        with self.assertRaisesRegex(RuntimeError, 'media_key missing or invalid length'):
            self.check()
        private = self.data / 'private'
        private.mkdir()
        for length in (0, 31, 33):
            with self.subTest(length=length):
                (private / 'media_key').write_bytes(b'x' * length)
                with self.assertRaisesRegex(RuntimeError, 'expected 32 bytes'):
                    self.check()
        (private / 'media_key').write_bytes(b'y' * 32)
        with self.assertRaisesRegex(RuntimeError, 'does not match backup manifest'):
            self.check()

    def test_media_key_outside_backup_is_rejected(self):
        for state in ('/external', '/data/../external', 'private'):
            with self.subTest(state=state):
                self.config('media: {state_dir: ' + state + '}\n')
                with self.assertRaisesRegex(RuntimeError, 'media_key is outside backed-up /data'):
                    self.check()

    def test_missing_default_media_key_follows_custom_execution_state(self):
        self.config('execution: {state_dir: custom/runtime}\n')
        with self.assertRaisesRegex(RuntimeError, 'media_key missing or invalid length'):
            self.check()

    def test_key_must_match_manifest_when_digest_is_recorded(self):
        (self.data / 'media_key').write_bytes(b'y' * 32)
        with self.assertRaisesRegex(RuntimeError, 'does not match backup manifest'):
            self.check()

    def test_corrupt_archive_fails(self):
        (self.root / 'data-private.tar.gz').write_bytes(b'not a gzip tar')
        with self.assertRaises(tarfile.TarError):
            drill.restore_check(self.manifest)

    def test_apply_does_not_touch_docker_when_restore_fails(self):
        (self.data / 'media_key').unlink()
        self.pack()
        with patch.object(drill.deploy, 'verify_services') as verify, \
                patch.object(drill.deploy, 'rollback') as rollback, \
                patch.object(drill.sys, 'argv', ['drill', '--manifest', str(self.manifest), '--apply']), \
                patch('sys.stdout', new_callable=io.StringIO), \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(drill.main(), 2)
            self.assertIn('DRILL_BLOCKED', stderr.getvalue())
        verify.assert_not_called()
        rollback.assert_not_called()

    def test_invalid_manifest_is_blocked_before_any_docker_call(self):
        for payload in (None, 'broken json', '[]'):
            with self.subTest(payload=payload):
                if payload is None:
                    self.manifest.unlink()
                else:
                    self.manifest.write_text(payload)
                with patch.object(drill.deploy, 'verify_services') as verify, \
                        patch.object(drill.deploy, 'rollback') as rollback, \
                        patch.object(drill.sys, 'argv', ['drill', '--manifest', str(self.manifest), '--apply']), \
                        patch('sys.stdout', new_callable=io.StringIO), \
                        patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    self.assertEqual(drill.main(), 2)
                    self.assertIn('DRILL_BLOCKED', stderr.getvalue())
                verify.assert_not_called()
                rollback.assert_not_called()

    def test_successful_dry_run_never_touches_docker(self):
        self.pack()
        with patch.object(drill.deploy, 'verify_services') as verify, \
                patch.object(drill.deploy, 'rollback') as rollback, \
                patch.object(drill.sys, 'argv', ['drill', '--manifest', str(self.manifest)]), \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(drill.main(), 0)
            self.assertIn('DRILL_DRY_RUN_OK', stdout.getvalue())
        verify.assert_not_called()
        rollback.assert_not_called()


class SafeExtractionTests(unittest.TestCase):
    def test_rejects_unsafe_members_before_extracting_any_file(self):
        cases = [('../escape', tarfile.REGTYPE),
                 ('/tmp/escape', tarfile.REGTYPE),
                 ('data/../escape', tarfile.REGTYPE),
                 ('data/./file', tarfile.REGTYPE),
                 ('data//file', tarfile.REGTYPE),
                 ('data\\escape', tarfile.REGTYPE),
                 ('unrelated/file', tarfile.REGTYPE),
                 ('data/link', tarfile.SYMTYPE),
                 ('data/hardlink', tarfile.LNKTYPE),
                 ('data/device', tarfile.CHRTYPE),
                 ('data/block', tarfile.BLKTYPE),
                 ('data/fifo', tarfile.FIFOTYPE),
                 ('data/unknown', b'Z')]
        for name, kind in cases:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    safe = tarfile.TarInfo('data/first')
                    safe.size = 4
                    tar.addfile(safe, io.BytesIO(b'safe'))
                    bad = tarfile.TarInfo(name)
                    bad.type = kind
                    bad.linkname = '../escape'
                    tar.addfile(bad)
                target = root / 'extract'
                target.mkdir()
                with self.assertRaisesRegex(RuntimeError, 'Unsafe backup archive'):
                    drill.extract_data_archive(archive, target)
                self.assertEqual(list(target.iterdir()), [])
                self.assertFalse((root / 'escape').exists())

    def test_duplicate_members_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'duplicate.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                tar.addfile(tarfile.TarInfo('data/file'))
                tar.addfile(tarfile.TarInfo('data/file'))
            target = root / 'extract'
            target.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'Unsafe backup archive'):
                drill.extract_data_archive(archive, target)
            self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
