#!/usr/bin/env python3
"""Explicit push-to-talk capture using arecord/ffmpeg, followed by offline ASR.

The WAV and authorization manifest are saved only to operator-selected paths.
An imported WAV is always labelled offline input, never microphone acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unicodedata
import urllib.parse
import urllib.request
import uuid
import wave


def inspect_wav(path, max_frames=960_000):
    with wave.open(str(path), 'rb') as wav:
        if (wav.getcomptype() != 'NONE' or wav.getnchannels() != 1
                or wav.getsampwidth() != 2 or wav.getframerate() != 16000):
            raise ValueError('Expected 16 kHz mono PCM16 WAV')
        frames = wav.getnframes()
        if not 1600 <= frames <= max_frames or len(wav.readframes(frames)) != frames * 2:
            raise ValueError('Recording must contain 0.1–60 seconds of complete PCM audio')
    return frames


def convert_wav(source, target):
    if shutil.which('ffmpeg') is None:
        raise RuntimeError('ffmpeg is required for explicit 16 kHz mono PCM16 conversion')
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                    '-i', str(source), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)],
                   check=True, timeout=140, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    inspect_wav(target)


def record_microphone(target, *, recorder='auto', device='default', seconds=None, input_fn=input):
    selected = recorder
    if selected == 'auto':
        selected = 'arecord' if shutil.which('arecord') else 'ffmpeg'
    if not shutil.which(selected):
        raise RuntimeError('MICROPHONE_NOT_ACCEPTED: install arecord or ffmpeg; no capture performed')
    if selected == 'arecord':
        command = ['arecord', '-q', '-D', device, '-t', 'wav', '-f', 'S16_LE',
                   '-r', '16000', '-c', '1']
        command += ['-d', str(math.ceil(seconds if seconds is not None else 60))]
        command.append(str(target))
    else:
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                   '-f', 'alsa', '-i', device, '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le']
        command += ['-t', str(seconds if seconds is not None else 60)]
        command.append(str(target))
    if seconds is None:
        input_fn('Press Enter to START the authorized microphone recording: ')
    if seconds is not None:
        subprocess.run(command, check=True, timeout=math.ceil(seconds) + 15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
        try:
            input_fn('Recording; press Enter to STOP (maximum 60 seconds in output): ')
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                _, error = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
                raise RuntimeError('Microphone recorder did not stop cleanly')
        if process.returncode not in (0, 1, 255, -signal.SIGINT):
            raise RuntimeError('MICROPHONE_NOT_ACCEPTED: microphone recorder failed')
    if not target.is_file() or target.stat().st_size <= 44:
        raise RuntimeError('MICROPHONE_NOT_ACCEPTED: no usable microphone WAV was captured')


def submit(server_url, relative_path, token):
    parsed = urllib.parse.urlsplit(server_url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Experimental client only accepts a localhost HTTP server')
    body = json.dumps({'path': relative_path, 'consent': True}).encode()
    request = urllib.request.Request(server_url.rstrip('/') + '/transcribe', data=body,
                                     headers={'Authorization': 'Bearer ' + token,
                                              'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consent', action='store_true')
    parser.add_argument('--reference', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Explicit destination WAV; must not exist')
    parser.add_argument('--manifest', type=Path, help='Explicit manifest path, defaults next to output')
    parser.add_argument('--input-wav', type=Path, help='Offline conversion, never labelled microphone recording')
    parser.add_argument('--seconds', type=float, help='Fixed duration, otherwise Enter starts/stops microphone')
    parser.add_argument('--recorder', choices=('auto', 'arecord', 'ffmpeg'), default='auto')
    parser.add_argument('--device', default='default', help='Explicit ALSA microphone device')
    parser.add_argument('--condition', choices=('clean', 'far-field', 'noise'), default='clean')
    parser.add_argument('--proper-noun', action='append', default=[])
    parser.add_argument('--sample-id')
    parser.add_argument('--server-url', help='Optional explicit localhost laboratory service')
    parser.add_argument('--server-path', help='WAV path relative to the service audio_root')
    parser.add_argument('--token-file', help='Independent experiment token, never production token')
    args = parser.parse_args(argv)
    if not args.consent:
        parser.error('--consent required before any microphone or input WAV access')
    reference = ''.join(c for c in unicodedata.normalize('NFKC', args.reference)
                        if not c.isspace() and not unicodedata.category(c).startswith('P'))
    if not reference:
        parser.error('--reference must contain non-punctuation characters')
    if args.seconds is not None and (not math.isfinite(args.seconds) or not .1 <= args.seconds <= 60):
        parser.error('--seconds must be between 0.1 and 60')
    if args.input_wav and args.seconds is not None:
        parser.error('--seconds applies only to microphone capture')
    if bool(args.server_url) != bool(args.server_path):
        parser.error('--server-url and --server-path must be supplied together')
    output = args.output.expanduser().absolute()
    manifest = (args.manifest or output.with_suffix('.samples.json')).expanduser().absolute()
    if output.suffix.lower() != '.wav' or output.exists() or output.is_symlink() or manifest.exists() or manifest.is_symlink() or manifest == output:
        parser.error('Use new, distinct WAV and manifest destinations; refusing overwrite')
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='asr-push-to-talk-') as temporary:
            temporary = Path(temporary)
            if args.input_wav:
                source = args.input_wav.expanduser()
                if source.suffix.lower() != '.wav' or source.is_symlink() or not source.is_file():
                    raise ValueError('--input-wav must be an explicitly authorized regular WAV')
                kind = 'offline_wav_input'
                origin = 'Operator supplied an existing WAV for authorized local offline processing'
            else:
                source = temporary / 'microphone.wav'
                record_microphone(source, recorder=args.recorder, device=args.device, seconds=args.seconds)
                kind = 'push_to_talk_microphone_then_offline'
                origin = 'Operator explicitly started and stopped/fixed-duration captured a local ALSA microphone'
            normalized = temporary / 'normalized.wav'
            convert_wav(source, normalized)
            frames = inspect_wav(normalized)
            with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as destination:
                destination.write(normalized.read_bytes())
            output.chmod(0o600)
        sample = {'id': args.sample_id or 'ptt-' + uuid.uuid4().hex[:12],
                  'path': os.path.relpath(output, manifest.parent), 'origin': origin,
                  'capture_kind': kind, 'license': 'LicenseRef-Explicit-Permission', 'authorized': True,
                  'permission_evidence': 'Operator supplied --consent and reference for local capture/processing',
                  'reference': args.reference.strip(), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                  'condition': args.condition, 'proper_nouns': args.proper_noun,
                  'recorded_at': datetime.now(timezone.utc).isoformat(), 'audio_seconds': frames / 16000}
        with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as destination:
            json.dump({'schema_version': 1, 'capture_kind': kind, 'samples': [sample]}, destination,
                      ensure_ascii=False, indent=2)
        manifest.chmod(0o600)
        print(json.dumps({'capture_kind': kind, 'wav': str(output), 'manifest': str(manifest),
                          'audio_seconds': frames / 16000, 'streaming': False}, ensure_ascii=False))
        if args.server_url:
            from poc.asr_lab_server import load_token
            result = submit(args.server_url, args.server_path, load_token(args.token_file))
            print(json.dumps(result, ensure_ascii=False))
    except Exception:
        parser.exit(2, 'CAPTURE_OR_OFFLINE_PROCESSING_FAILED: no microphone/service acceptance claimed; verify local device and explicit paths\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
