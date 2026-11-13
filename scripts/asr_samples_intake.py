#!/usr/bin/env python3
"""Turn operator-recorded speech into the ASR comparison manifest.

The operator records with any device (phone/NAS mic) and drops files plus a
small mapping JSON. This script converts to 16 kHz mono PCM16, hashes the result,
and writes a manifest requiring explicit per-file authorization.

mapping.json:
  {"speaker": "adult-female", "consent": "recorded by the operator for this benchmark",
   "files": [{"file": "clean-01.m4a", "reference": "打开飞牛", "condition": "clean",
              "proper_nouns": ["飞牛"]}]}
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

CONDITIONS=('clean','far-field','noise')
LICENSE='LicenseRef-Explicit-Permission'


def convert(source:Path,target:Path):
    if shutil.which('ffmpeg') is None:
        raise RuntimeError('ffmpeg 不可用，无法转换录音')
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(source),
                    '-ac','1','-ar','16000','-c:a','pcm_s16le',str(target)],check=True,timeout=120)
    with wave.open(str(target)) as handle:
        if handle.getnchannels()!=1 or handle.getframerate()!=16000 or handle.getsampwidth()!=2:
            raise RuntimeError(f'{target.name} 转换后不是 mono PCM16 16k')
        if handle.getnframes()<1600:
            raise RuntimeError(f'{target.name} 太短（<0.1s），请重录')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping',type=Path,required=True)
    parser.add_argument('--recordings',type=Path,required=True)
    parser.add_argument('--out-dir',type=Path,required=True)
    parser.add_argument('--consent',action='store_true',
                        help='operator confirms these recordings are authorized for this benchmark')
    args=parser.parse_args()
    if not args.consent:
        parser.error('--consent required: only authorized recordings may be processed')
    mapping=json.loads(args.mapping.read_text(encoding='utf-8'))
    consent=(mapping.get('consent') or '').strip()
    if not consent:
        parser.error('mapping.json needs a non-empty consent note')
    out=args.out_dir.expanduser().resolve()
    out.mkdir(parents=True,exist_ok=True,mode=0o700);out.chmod(0o700)
    samples=[]
    for index,item in enumerate(mapping.get('files') or [],1):
        source=(args.recordings/item['file']).resolve()
        if not source.is_file():
            raise FileNotFoundError(item['file'])
        condition=item.get('condition','clean')
        if condition not in CONDITIONS:
            raise ValueError(f'未知 condition: {condition}')
        reference=(item.get('reference') or '').strip()
        if not reference:
            raise ValueError(f'{item["file"]} 缺少 reference 文本')
        target=out/f'human-{index:02d}.wav'
        convert(source,target);target.chmod(0o600)
        samples.append({'id':f'human-{index:02d}','path':target.name,
                        'origin':f'Operator-recorded speech ({item["file"]}); authorized for this benchmark',
                        'license':LICENSE,'authorized':True,'permission_evidence':consent,
                        'reference':reference,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                        'condition':condition,'proper_nouns':item.get('proper_nouns') or [],
                        'speaker':mapping.get('speaker')})
        print(f'CONVERTED {item["file"]} -> {target.name} ({condition})',flush=True)
    manifest=out/'samples.json'
    manifest.write_text(json.dumps({'schema_version':1,'samples':samples},ensure_ascii=False,indent=2))
    manifest.chmod(0o600)
    print(f'MANIFEST_READY {manifest} samples={len(samples)}')
    print('下一步: poc/asr_compare.py --samples '+str(manifest)+' --consent --report <out>.json')
    return 0


if __name__=='__main__':
    sys.exit(main())