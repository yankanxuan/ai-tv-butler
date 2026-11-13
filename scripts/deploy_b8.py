#!/usr/bin/env python3
"""Back up actual running images/data, build b8 and verify four real containers.

Requires an operator account with Docker access. No sudo/chown/socket permission
changes, no library writes, no data/key regeneration. A build/start/health
failure automatically starts the recorded old images. --rollback uses the same
private manifest. Default is a non-mutating command plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('console', 'scheduler', 'media', 'voice')


def run(args, *, capture=True, timeout=600):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=capture, timeout=timeout)
    if result.returncode:
        # Do not print compose config (may contain private paths or credentials).
        raise RuntimeError(f'{args[0]} {args[1] if len(args)>1 else ""} failed (exit {result.returncode}); run from a Docker-authorized NAS terminal')
    return result.stdout.strip() if capture else ''


def compose(project, *args):
    return ['docker', 'compose', '-f', str(ROOT/'docker-compose.yml'), '-p', project, *args]


def inspect_container(project, service):
    identifier=run(compose(project, 'ps', '-a', '-q', service), timeout=20)
    if not identifier or '\n' in identifier:
        raise RuntimeError(f'Expected exactly one {service} container')
    return json.loads(run(['docker', 'inspect', identifier], timeout=20))[0]


def key_digest(data):
    path=data/'media_key'
    if not path.is_file():
        raise RuntimeError('Existing shared media_key is missing; refusing to regenerate it')
    raw=path.read_bytes()
    if len(raw)!=32:raise RuntimeError('Existing media_key has invalid length; restore a valid key backup first')
    return hashlib.sha256(raw).hexdigest()


def verify_services(project, *, expected_version, timeout=240, wait=True):
    """Return evidence only when all four containers are actually healthy.

    A zero exit status from `up -d` proves nothing; health/version/healthcheck
    are re-read from the running containers.
    """
    deadline=time.monotonic()+timeout
    while True:
        checks={name:inspect_container(project,name) for name in SERVICES}
        states={name:row.get('State',{}).get('Health',{}).get('Status','missing') for name,row in checks.items()}
        running={name:row.get('State',{}).get('Running') for name,row in checks.items()}
        print('DOCKER_HEALTH',json.dumps({k:[states[k],running[k]] for k in SERVICES}),flush=True)
        if all(states[k]=='healthy' and running[k] is True for k in SERVICES):
            break
        if not wait or time.monotonic()>=deadline:
            raise RuntimeError(f'Services not healthy: {states}')
        time.sleep(3)
    versions={name:run(compose(project,'exec','-T',name,'ai-tv','--version'),timeout=30) for name in SERVICES}
    if expected_version and any(value!='ai-tv '+expected_version for value in versions.values()):
        raise RuntimeError(f'Container version mismatch: {versions}')
    for service in SERVICES:
        args=['python3','/app/healthcheck.py']
        if service!='console':args.append(service)
        run(compose(project,'exec','-T',service,*args),timeout=35)
    return {'health':states,'running':running,'versions':versions,'image_ids':{n:c['Image'] for n,c in checks.items()}}


def rollback(project, manifest_path):
    manifest_path=Path(manifest_path).resolve()
    manifest=json.loads(manifest_path.read_text())
    if manifest.get('project')!=project or set(manifest.get('images',{}))!=set(SERVICES):
        raise ValueError('Rollback manifest project/services mismatch')
    services={}
    for service,record in manifest['images'].items():
        tag=record.get('rollback_tag','')
        if not re.fullmatch(r'ai-tv-butler:pre-b8-[0-9T-]+-(?:console|scheduler|media|voice)',tag):
            raise ValueError('Untrusted rollback image tag')
        services[service]={'image':tag, 'build':None}
    override=manifest_path.parent/'rollback.compose.json'
    override.write_text(json.dumps({'services':services}))
    override.chmod(0o600)
    run(['docker','compose','-f',str(ROOT/'docker-compose.yml'),'-f',str(override),'-p',project,
         'up','-d','--no-build'],capture=False)
    evidence=verify_services(project,expected_version=None)
    expected={name:record['id'] for name,record in manifest['images'].items()}
    restored={name:evidence['image_ids'][name] for name in SERVICES}
    if restored!=expected:
        raise RuntimeError(f'Rollback did not restore recorded images: {restored} != {expected}')
    report={'status':'ROLLBACK_VERIFIED','checked_at':time.time(),**evidence,'restored_images':restored}
    path=manifest_path.parent/'rollback-verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2));path.chmod(0o600)
    print('ROLLBACK_VERIFIED: four containers healthy on restored images',flush=True)
    return report


def deploy(project, backup_root, *, health_timeout=240):
    # Permission failure must occur before stopping containers or editing data.
    run(['docker','info','--format','{{.ServerVersion}}'], timeout=20)
    current={name:inspect_container(project,name) for name in SERVICES}
    sources=[]
    for service,container in current.items():
        data=[m['Source'] for m in container.get('Mounts',[]) if m.get('Destination')=='/data']
        if len(data)!=1:raise RuntimeError(f'{service} has no unique /data mount')
        sources.append(Path(data[0]).resolve())
    if len(set(sources))!=1:raise RuntimeError('Services do not share the same /data directory')
    data=sources[0]
    before_key=key_digest(data)
    backup_root=Path(backup_root).expanduser().resolve()
    stamp=time.strftime('%Y%m%dT%H%M%S')
    directory=backup_root/('pre-b8-'+stamp)
    directory.mkdir(parents=True,mode=0o700,exist_ok=False)
    directory.chmod(0o700)
    images={}
    for service,container in current.items():
        image_id=container.get('Image','')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id):raise RuntimeError('Docker returned invalid image id')
        tag=f'ai-tv-butler:pre-b8-{stamp}-{service}'
        run(['docker','image','tag',image_id,tag],timeout=30)
        images[service]={'id':image_id,'rollback_tag':tag,'configured_image':container.get('Config',{}).get('Image')}
    # Immutable image archive before shutdown, backed up by actual IDs not assumed b7 tags.
    image_archive=directory/'images-private.tar'
    run(['docker','image','save','-o',str(image_archive),*sorted({r['id'] for r in images.values()})],timeout=600)
    image_archive.chmod(0o600)
    manifest={'project':project,'created_at':time.time(),'images':images,'data_dir':str(data),
              'media_key_sha256_before':before_key,'source_note':'Original published b7 source archive and private snapshot retained separately'}
    manifest_path=directory/'manifest-private.json'
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2));manifest_path.chmod(0o600)
    stopped=False
    try:
        run(compose(project,'stop',*SERVICES),capture=False,timeout=120)
        stopped=True
        with tarfile.open(directory/'data-private.tar.gz','w:gz',dereference=False) as archive:
            archive.add(data,arcname='data')
        (directory/'data-private.tar.gz').chmod(0o600)
        with tarfile.open(directory/'config-source-private.tar.gz','w:gz',dereference=False) as archive:
            for name in ('.env','config.yaml','docker-compose.yml'):
                path=ROOT/name
                if path.is_file():archive.add(path,arcname=name)
        (directory/'config-source-private.tar.gz').chmod(0o600)
        # Run with the same Compose args/UID/mount configuration as production.
        run(compose(project,'build'),capture=False,timeout=1800)
        run(compose(project,'up','-d'),capture=False,timeout=180)
        evidence=verify_services(project,expected_version='4.0.0b8',timeout=health_timeout)
        states=evidence['health'];versions=evidence['versions'];checks=evidence['image_ids']
        after_key=key_digest(data)
        if before_key!=after_key:raise RuntimeError('Media key unexpectedly changed')
        dependencies=json.loads(run(compose(project,'exec','-T','console','python3','-c',
            "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ['PyYAML','pydantic','pydantic-core','typing-extensions']}))"),timeout=20))
        if dependencies!={'PyYAML':'6.0.3','pydantic':'2.14.3','pydantic-core':'2.50.4','typing-extensions':'4.16.0'}:
            raise RuntimeError('Deployed runtime differs from the tested lock')
        manifest.update(status='DEPLOYED_VERIFIED',completed_at=time.time(),versions=versions,
                        health=states,dependencies=dependencies,media_key_preserved=True,
                        running_image_ids=evidence['image_ids'])
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2));manifest_path.chmod(0o600)
        print('B8_DEPLOYMENT_VERIFIED; private backup:',directory,flush=True)
        return manifest_path
    except BaseException:
        if stopped:
            print('DEPLOYMENT_FAILED: rolling back to backed-up image tags',flush=True)
            rollback(project,manifest_path)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--project',default='ai-tv-butler')
    parser.add_argument('--backup-root',type=Path,default=Path.home()/'ai-tv-butler-backups')
    parser.add_argument('--health-timeout',type=float,default=240)
    parser.add_argument('--verify-only',action='store_true',help='read running state without changing anything')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.project):parser.error('invalid project name')
    try:
        if args.rollback:
            rollback(args.project,args.rollback)
        elif args.verify_only:
            evidence=verify_services(args.project,expected_version=None,wait=False)
            print(json.dumps({'status':'RUNNING_STATE_VERIFIED',**evidence},ensure_ascii=False,indent=2))
        elif args.apply:
            deploy(args.project,args.backup_root,health_timeout=args.health_timeout)
        else:
            print('PLAN: Docker preflight -> record actual image IDs/shared data -> save images -> stop four services -> cold data/config backup -> build b8 -> up -> four healthy + versions + locked deps + unchanged media key; auto rollback on failure')
        return 0
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
        print(f'DEPLOYMENT_NOT_VERIFIED: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
