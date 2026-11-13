#!/usr/bin/env python3
"""Rollback drill: verify the recorded backup really is restorable, then roll back
and re-verify four healthy containers. Must run where Docker is authorized.

Refuses to treat a successful command as success: health, version and image IDs
are re-read from the running containers after the rollback, and the backup
archive is restored into a throwaway directory to prove data recoverability.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import deploy_b8 as deploy  # noqa: E402


def extract_data_archive(archive, target):
    """Preflight every member; copy only regular files/directories under data/."""
    target = Path(target).resolve()
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        seen = set()
        for member in members:
            name = member.name.rstrip('/')
            parts = name.split('/')
            if (not name or '\\' in name or parts[0] != 'data'
                    or any(part in ('', '.', '..') for part in parts)
                    or not (member.isdir() or member.isreg())
                    or member.name.startswith('/') or name in seen
                    or (len(parts) == 1 and not member.isdir())):
                raise RuntimeError('Unsafe backup archive member')
            destination = target.joinpath(*parts)
            if not destination.resolve().is_relative_to(target):
                raise RuntimeError('Unsafe backup archive destination')
            seen.add(name)
        # Do not apply archive permissions/owners, create links, or use extractall.
        for member in members:
            destination = target / member.name.rstrip('/')
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError('Backup archive file has no content')
                with source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output)


def restored_config(data):
    """Use the real loader with Compose's /data settings, isolated from the host."""
    path = data / 'config.yaml'
    if not path.is_file():
        raise RuntimeError('config.yaml missing; backup not restorable')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('AI_TV_') and key not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(AI_TV_DATA_DIR='/data', HOME='/data', PYTHONDONTWRITEBYTECODE='1')
    code = '''
import json, os, sys
from pathlib import Path
from adbtv import config
from adbtv.execution import runtime_dir
raw = config.yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
if not isinstance(raw, dict) or not raw:
    raise ValueError('Configuration must be a nonempty mapping')
def check_sections(defaults, supplied):
    for key, value in supplied.items():
        if isinstance(defaults.get(key), dict):
            if not isinstance(value, dict):
                raise ValueError('Configuration section must be a mapping')
            check_sections(defaults[key], value)
check_sections(config.DEFAULTS, raw)
cfg = config.load_config(sys.argv[1])
state = Path(cfg['media'].get('state_dir') or runtime_dir(cfg).parent).expanduser()
# Compose's WORKDIR is /app; media.state_dir retains process-relative semantics.
if not state.is_absolute():
    state = Path('/app') / state
cfg['_restore_media_key_path'] = os.path.normpath(str(state / 'media_key'))
print(json.dumps(cfg))
'''
    result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Loader errors may quote credentials from YAML; do not echo stderr.
        raise RuntimeError('config.yaml invalid; backup not restorable')
    return json.loads(result.stdout)


def restore_check(manifest_path):
    """Return evidence only after configuration, key and actual SQLite pass."""
    manifest_path = Path(manifest_path)
    record = json.loads(manifest_path.read_text())
    if not isinstance(record, dict):
        raise ValueError('Backup manifest must be a mapping')
    archive = manifest_path.parent / 'data-private.tar.gz'
    if not archive.is_file():
        raise RuntimeError('data-private.tar.gz missing; backup not restorable')
    with tempfile.TemporaryDirectory(prefix='restore-check-') as tmp:
        target = Path(tmp)
        extract_data_archive(archive, target)
        data = target / 'data'
        cfg = restored_config(data)
        try:
            key_relative = PurePosixPath(cfg['_restore_media_key_path']).relative_to('/data')
        except (TypeError, ValueError):
            raise RuntimeError('Configured media_key is outside backed-up /data') from None
        key = data.joinpath(*key_relative.parts)
        if not key.resolve().is_relative_to(data.resolve()):
            raise RuntimeError('Configured media_key escapes backed-up /data')
        if not key.is_file() or len(key.read_bytes()) != 32:
            raise RuntimeError('media_key missing or invalid length; expected 32 bytes')
        digest = hashlib.sha256(key.read_bytes()).hexdigest()
        expected = record.get('media_key_sha256_before')
        if expected is not None and digest != expected:
            raise RuntimeError('media_key does not match backup manifest')
        # The console also uses this store with memory.enabled=false. Its path
        # need not end in .sqlite3; decoy databases cannot prove recoverability.
        configured = cfg['memory']['path']
        try:
            relative = PurePosixPath(configured).relative_to('/data')
        except (TypeError, ValueError):
            raise RuntimeError('Configured SQLite is outside backed-up /data') from None
        database = data.joinpath(*relative.parts)
        if not database.resolve().is_relative_to(data.resolve()):
            raise RuntimeError('Configured SQLite escapes backed-up /data')
        if not database.is_file():
            raise RuntimeError('Configured SQLite missing; backup not restorable')
        with database.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                raise RuntimeError('Configured SQLite has invalid database header')
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                integrity = db.execute('PRAGMA integrity_check').fetchall()
            if integrity != [('ok',)]:
                raise RuntimeError('Configured SQLite integrity_check failed')
        except sqlite3.Error as exc:
            raise RuntimeError('Configured SQLite integrity_check failed') from exc
        return {'archive_bytes': archive.stat().st_size,
                'required_files_found': ['config.yaml'],
                'config_restorable': True, 'media_key_restorable': True,
                'sqlite_files_in_backup': [relative.as_posix()],
                'sqlite_integrity_check': 'ok'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--project',default='ai-tv-butler')
    parser.add_argument('--apply',action='store_true',help='actually roll back; default only inspects')
    args=parser.parse_args()
    print('PLAN: inspect backup -> restore into temp dir -> (--apply) rollback images -> re-verify four containers')
    try:
        manifest=Path(args.manifest).resolve()
        restore=restore_check(manifest)
    except (OSError, ValueError, RuntimeError, tarfile.TarError,
            subprocess.TimeoutExpired, EOFError) as exc:
        print(f'DRILL_BLOCKED: backup restore validation failed: {exc}', file=sys.stderr)
        return 2
    print('BACKUP_RESTORE_CHECK',json.dumps(restore,ensure_ascii=False),flush=True)
    if not args.apply:
        print('DRILL_DRY_RUN_OK (nothing rolled back)')
        return 0
    before=deploy.verify_services(args.project,expected_version=None,wait=False)
    print('BEFORE_ROLLBACK',json.dumps(before['image_ids'],ensure_ascii=False),flush=True)
    report=deploy.rollback(args.project,manifest)
    verifier=manifest.parent/'rollback-verification.json'
    if not verifier.is_file():
        print('DRILL_FAILED: rollback produced no verification record',file=sys.stderr)
        return 3
    final=json.loads(verifier.read_text())
    print('AFTER_ROLLBACK',json.dumps({'health':final['health'],'versions':final['versions']},ensure_ascii=False),flush=True)
    print('ROLLBACK_DRILL_VERIFIED: backup restorable + four containers healthy on restored images',flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())