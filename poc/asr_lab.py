#!/usr/bin/env python3
"""Opt-in, isolated CPU ASR laboratory. Never executes television commands.

Every profile is a reviewed, immutable entry in models-v41.lock.json. Fetch is
explicit; inference is offline and cannot download anything. Each evaluation runs
one model per process so lifetime RSS is not polluted by another loaded model.
"""
from __future__ import annotations

import argparse
from array import array
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import urllib.request

from poc.sherpa_asr import ValidationError, _hash_file, _peak_rss_bytes, read_wav, validate_samples
from poc.asr_compare import character_metrics, contains_reference, aggregate, recognize

REGISTRY = Path(__file__).with_name('models-v41.lock.json')
MAX_AUDIO_SECONDS = 60
DEPENDENCIES = {'sherpa-onnx': '2.0.1', 'sherpa-onnx-core': '2.0.1',
                'jiwer': '4.0.0', 'rapidfuzz': '3.14.3', 'click': '8.1.8'}


def registry():
    return json.loads(REGISTRY.read_text(encoding='utf-8'))


def check_runtime():
    for name, expected in DEPENDENCIES.items():
        if version(name) != expected:
            raise ValidationError(f'{name} must be {expected}; use isolated requirements-v41.lock')


def checked_artifacts(directory, spec):
    directory = Path(directory).resolve()
    result = {}
    for role, entry in spec['files'].items():
        path = directory / entry['name']
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != entry['size_bytes']
                or _hash_file(path) != entry['sha256']):
            raise ValidationError(f'Pinned {role} artifact missing or hash/size mismatch')
        result[role] = str(path)
    return result


def validate_manifest(path, *, vad=False):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise ValidationError('experimental manifest schema_version must be 1')
    profile = manifest.get('profile')
    profiles = registry()['profiles']
    if not isinstance(profile, str) or profile not in profiles:
        raise ValidationError('Unknown reviewed model profile')
    model_dir = manifest.get('model_dir')
    if not isinstance(model_dir, str) or not model_dir.strip():
        raise ValidationError('model_dir required')
    spec = profiles[profile]
    files = checked_artifacts(path.parent / model_dir, spec)
    # Evidence is shipped and hashed independently of the caller's manifest.
    for evidence in spec['evidence']:
        local = REGISTRY.parent / evidence['path']
        if not local.is_file() or _hash_file(local) != evidence['sha256']:
            raise ValidationError('Pinned model license/provenance evidence mismatch')
    vad_file = None
    if vad:
        if not isinstance(manifest.get('vad_dir'), str):
            raise ValidationError('vad_dir required when VAD is enabled')
        vad_spec = registry()['vad']
        vad_file = checked_artifacts(path.parent / manifest['vad_dir'], vad_spec)['model']
        for evidence in vad_spec['evidence']:
            if _hash_file(REGISTRY.parent / evidence['path']) != evidence['sha256']:
                raise ValidationError('Pinned VAD license evidence mismatch')
    return manifest, spec, files, vad_file


def fetch_artifact(entry, destination):
    """Bounded HTTPS fetch with verified content; old valid artifacts are retained."""
    destination = Path(destination)
    if destination.is_file() and not destination.is_symlink():
        if destination.stat().st_size == entry['size_bytes'] and _hash_file(destination) == entry['sha256']:
            return
    if destination.exists() or destination.is_symlink():
        raise ValidationError('Existing artifact invalid; use a new cache directory')
    if not entry['url'].startswith('https://'):
        raise ValidationError('Model sources must use HTTPS')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive temp path prevents concurrent fetches clobbering another download.
    temporary = destination.with_name(destination.name + '.partial')
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open('xb') as out, urllib.request.urlopen(entry['url'], timeout=180) as response:
            if not response.geturl().startswith('https://'):
                raise ValidationError('Insecure model redirect rejected')
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > entry['size_bytes']:
                    raise ValidationError('Model artifact exceeds pinned size')
                out.write(chunk)
                digest.update(chunk)
        if total != entry['size_bytes'] or digest.hexdigest() != entry['sha256']:
            raise ValidationError('Downloaded model artifact failed size/hash verification')
        # Both paths are resolved and constrained before the atomic rename.
        if temporary.resolve().parent != destination.resolve().parent:
            raise ValidationError('Unsafe model destination')
        temporary.replace(destination)
    except Exception:
        # Keep the partial file for inspection; never expose it as a valid model.
        raise


def prepare(profile, cache, *, with_vad=False):
    data = registry()
    if profile not in data['profiles']:
        raise ValidationError('Unknown reviewed model profile')
    cache = Path(cache).resolve()
    spec = data['profiles'][profile]
    for entry in spec['files'].values():
        fetch_artifact(entry, cache / profile / entry['name'])
    if with_vad:
        for entry in data['vad']['files'].values():
            fetch_artifact(entry, cache / 'silero-v5.1.2' / entry['name'])
    manifest = {'schema_version': 1, 'profile': profile, 'model_dir': profile,
                'vad_dir': 'silero-v5.1.2'}
    path = cache / (profile + '.json')
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValidationError('Existing experiment manifest differs; use a new cache')
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    return path


class Engine:
    def __init__(self, manifest_path, num_threads=2, vad=False):
        if type(num_threads) is not int or not 1 <= num_threads <= 4:
            raise ValidationError('num_threads must be 1..4')
        self.manifest, self.spec, files, vad_file = validate_manifest(manifest_path, vad=vad)
        check_runtime()
        import sherpa_onnx
        self.sherpa = sherpa_onnx
        started, cpu = time.perf_counter(), time.process_time()
        kwargs = dict(num_threads=num_threads, sample_rate=16000, feature_dim=80,
                      decoding_method='greedy_search', provider='cpu')
        kind = self.spec['kind']
        if kind == 'ctc':
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(**files, **kwargs)
        elif kind == 'transducer':
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(**files, **kwargs)
        elif kind == 'paraformer':
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(**files, **kwargs)
        else:
            raise ValidationError('Unsupported reviewed model kind')
        self.vad = None
        if vad_file:
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = vad_file
            config.silero_vad.threshold = 0.5
            config.silero_vad.min_silence_duration = 0.5
            config.silero_vad.min_speech_duration = 0.25
            config.silero_vad.max_speech_duration = 20
            config.sample_rate = 16000
            config.num_threads = 1
            self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=MAX_AUDIO_SECONDS + 2)
        self.initialization_seconds = time.perf_counter() - started
        self.initialization_cpu_seconds = time.process_time() - cpu
        self.num_threads = num_threads

    def transcribe(self, path):
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 16000 * 2 * MAX_AUDIO_SECONDS + 4096:
            raise ValidationError('WAV missing or exceeds 60 second input bound')
        audio, seconds = read_wav(path)
        if seconds > MAX_AUDIO_SECONDS:
            raise ValidationError('WAV exceeds 60 seconds')
        started, cpu = time.perf_counter(), time.process_time()
        segments = [(0, audio)]
        if self.vad is not None:
            self.vad.reset()
            for offset in range(0, len(audio), 512):
                self.vad.accept_waveform(audio[offset:offset + 512])
            self.vad.flush()
            segments = []
            while not self.vad.empty():
                segment = self.vad.front
                segments.append((int(segment.start), array('f', segment.samples)))
                self.vad.pop()
        texts, first, boundaries = [], None, []
        for start, samples in segments:
            before_decode = time.perf_counter() - started
            text, wall, partial = recognize(self.recognizer, samples)
            if first is None and partial is not None:
                first = before_decode + partial
            texts.append(text)
            boundaries.append({'start_sample': start, 'samples': len(samples)})
        wall = time.perf_counter() - started
        return {'hypothesis': ''.join(texts), 'audio_seconds': seconds,
                'wall_seconds': wall, 'cpu_seconds': time.process_time() - cpu,
                'rtf': wall / seconds, 'first_partial_seconds': first,
                'vad_enabled': self.vad is not None, 'vad_segments': boundaries,
                'capture_kind': 'offline_file_replay', 'realtime_microphone_tested': False}


def private_json(path, data):
    path = Path(path)
    # Evaluation output can contain user transcripts; use exclusive, private files.
    with open(path, 'x', encoding='utf-8', opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write('\n')


def evaluate(manifest, samples_path, *, consent=False, num_threads=2, vad=False, title_index=None):
    if consent is not True:
        raise ValidationError('Explicit --consent required before reading samples')
    sample_path = Path(samples_path).resolve()
    doc = json.loads(sample_path.read_text(encoding='utf-8'))
    samples = validate_samples(doc, sample_path.parent)
    conditions = {'clean', 'far-field', 'noise'}
    if any(sample.get('condition', 'clean') not in conditions for sample, _ in samples):
        raise ValidationError('Unknown sample condition')
    engine = Engine(manifest, num_threads=num_threads, vad=vad)
    suggester = None
    if title_index:
        from poc.title_candidates import TitleSuggester
        suggester = TitleSuggester.from_cache(title_index)
    rows = []
    for sample, path in samples:
        result = engine.transcribe(path)
        text = result['hypothesis']
        row = {'id': sample['id'], 'condition': sample.get('condition', 'clean'),
               'audio_sha256': sample['sha256'], 'reference': sample['reference'],
               'source_capture_kind': sample.get('capture_kind', 'existing_authorized_recording'),
               **result, **character_metrics(sample['reference'], text),
               'proper_nouns': {noun: contains_reference(noun, text) for noun in sample.get('proper_nouns', [])},
               'command_recalled': contains_reference(sample['reference'], text)}
        if suggester:
            row['title_suggestions'] = suggester.suggest(text)
        rows.append(row)
    return {'status': 'offline_evaluation_completed', 'evaluated_at': datetime.now(timezone.utc).isoformat(),
            'capture_kind': 'offline_file_replay', 'realtime_microphone_tested': False,
            'profile': engine.manifest['profile'], 'model': engine.spec,
            'model_registry_sha256': _hash_file(REGISTRY),
            'sample_manifest_sha256': _hash_file(sample_path),
            'runtime': {'machine': platform.machine(), 'python': platform.python_version(),
                        'versions': {name: version(name) for name in DEPENDENCIES},
                        'cpu_provider': 'cpu', 'num_threads': num_threads,
                        'cpu_model': platform.processor()},
            'initialization_seconds': engine.initialization_seconds,
            'initialization_cpu_seconds': engine.initialization_cpu_seconds,
            'inference_cpu_seconds': sum(r['cpu_seconds'] for r in rows),
            'peak_rss_bytes': _peak_rss_bytes(), 'peak_rss_scope': 'single_model_process_lifetime',
            'vad_enabled': vad,
            'vad_model': registry()['vad'] if vad else None,
            'vad_settings': {'threshold': 0.5, 'min_silence_seconds': 0.5,
                             'min_speech_seconds': 0.25, 'max_speech_seconds': 20,
                             'window_samples': 512} if vad else None,
            'decoding': {'method': 'greedy_search', 'chunk_samples': 3200,
                         'right_context_padding_samples': 10560, 'sample_rate': 16000},
            'overall': aggregate(rows),
            'by_condition': {c: aggregate([r for r in rows if r['condition'] == c]) for c in sorted(conditions)},
            'samples': rows, 'xiaomi_default_unchanged': True,
            'timing_note': 'Unpaced file replay; first_partial_seconds is not live microphone latency. Inference CPU excludes model loading and file reads; lifetime RSS includes scorer/imports.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('prepare')
    fetch.add_argument('--profile', required=True)
    fetch.add_argument('--cache', required=True)
    fetch.add_argument('--with-vad', action='store_true')
    run = sub.add_parser('evaluate')
    run.add_argument('--manifest', required=True)
    run.add_argument('--samples', required=True)
    run.add_argument('--report', required=True)
    run.add_argument('--consent', action='store_true')
    run.add_argument('--num-threads', type=int, default=2)
    run.add_argument('--vad', action='store_true')
    run.add_argument('--title-index')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            print(prepare(args.profile, args.cache, with_vad=args.with_vad))
        else:
            if Path(args.report).exists():
                raise ValidationError('Refusing to overwrite evaluation evidence')
            result = evaluate(args.manifest, args.samples, consent=args.consent,
                              num_threads=args.num_threads, vad=args.vad, title_index=args.title_index)
            private_json(args.report, result)
            print(json.dumps({'status': result['status'], 'profile': result['profile'],
                              'overall': result['overall']}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'ASR_LAB_REJECTED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
