"""Consent-gated, standalone sherpa-onnx streaming ZIPformer benchmark.

No production imports, downloader, microphone access, or sample discovery.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
import platform
import re
import resource
import sys
import time
import unicodedata
import wave

MODEL_REPO = 'csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20'
MODEL_REVISION = '98590b7ed6443e77b714204da2757d75e1a642f4'
LICENSE_EVIDENCE = f'https://huggingface.co/{MODEL_REPO}/blob/{MODEL_REVISION}/README.md'
PINNED_FILES = {
    'encoder': ('encoder-epoch-99-avg-1.int8.onnx', '8fa764187a261844f859d7143ebaa563af5d10adfece4c18a8f414c88cba2a9b', 181895032),
    'decoder': ('decoder-epoch-99-avg-1.int8.onnx', '1a70c593d71e53f023f5f55b0b4cfff5055abb786ee3992e5f63dc2e273cc4fa', 13091040),
    'joiner': ('joiner-epoch-99-avg-1.int8.onnx', '1ed689c5ed19dbaa725d9d191bb4822b5f4855a39e1ffd28cbc1f340d25b2ee0', 3228404),
    'tokens': ('tokens.txt', 'a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3', 56317),
}
CTC_REPO = 'csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01'
CTC_REVISION = 'a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da'
CTC_PROVENANCE = f'https://huggingface.co/{CTC_REPO}/blob/{CTC_REVISION}/README.md'
CTC_LICENSE_EVIDENCE = 'https://huggingface.co/csukuangfj/icefall-streaming-zipformer-small-ctc-zh-2025-04-01/blob/6768aa8b65bc68e128af2ee08a7142dd0abe4be4/README.md'
CTC_PINNED_FILES = {
    'model': ('model.int8.onnx', '68c9c943840f7d9cf3e8a4970ba50f404feb5277f611fa82b7e72267786fa84a', 26342340),
    'tokens': ('tokens.txt', '6fed8c6c248516f38e7faa19404b57413e8ce259f1cbc1fa4aebc86eac32fdfd', 13366),
}
# Deliberately narrow: add licenses only after reviewing the sample rights.
SAMPLE_LICENSES = frozenset({'CC0-1.0', 'CC-BY-4.0', 'CC-BY-SA-4.0', 'LicenseRef-Explicit-Permission'})
CER_NORMALIZATION = 'NFKC; remove Unicode punctuation and whitespace; preserve case; Unicode code points'


class ValidationError(ValueError):
    """An input failed the provenance, consent, or audio-format gates."""


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _checked_file(entry, root):
    _require(isinstance(entry, dict), 'file entry must be an object')
    _require(_text(entry.get('path')), 'file path required')
    sha = entry.get('sha256')
    _require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha), 'sha256 must be 64 lowercase hex characters')
    path = (root / entry['path']).resolve()
    _require(path.is_file(), f'file unavailable: {path}')
    _require(_hash_file(path) == sha, f'sha256 mismatch: {path}')
    return path


def load_manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    _require(isinstance(data, dict) and data.get('schema_version') == 1, 'schema_version must be 1')
    return data, path.parent


def validate_model(data, root):
    _require(data.get('schema_version') == 1, 'schema_version must be 1')
    model = data.get('model')
    _require(isinstance(model, dict), 'model object required')
    repo = model.get('repo')
    _require(repo in (MODEL_REPO, CTC_REPO), 'unsupported model repo')
    compact = repo == CTC_REPO
    revision = CTC_REVISION if compact else MODEL_REVISION
    evidence = CTC_LICENSE_EVIDENCE if compact else LICENSE_EVIDENCE
    pins = CTC_PINNED_FILES if compact else PINNED_FILES
    _require(model.get('revision') == revision, 'model revision must match the reviewed commit')
    _require(model.get('license') == 'apache-2.0', 'model license must match upstream apache-2.0 declaration')
    _require(model.get('license_evidence') == evidence, 'license_evidence must identify the pinned upstream card')
    if compact:
        _require(model.get('provenance_evidence') == CTC_PROVENANCE, 'provenance_evidence must link ONNX artifacts to licensed checkpoint')
    files = model.get('files')
    _require(isinstance(files, dict) and set(files) == set(pins), f'model file roles must be {sorted(pins)}')
    # Check every declaration before reading any model artifact.
    for role, (name, sha, size) in pins.items():
        entry = files[role]
        _require(isinstance(entry, dict), f'{role} entry must be an object')
        _require(entry.get('sha256') == sha, f'{role} sha256 does not match pinned upstream artifact')
        _require(entry.get('size_bytes') == size, f'{role} size_bytes does not match upstream artifact')
        expected_url = f'https://huggingface.co/{repo}/resolve/{revision}/{name}'
        _require(entry.get('source_url') == expected_url, f'{role} source_url must match pinned upstream artifact')
    result = {}
    for role, (_, _, size) in pins.items():
        path = _checked_file(files[role], root)
        _require(path.stat().st_size == size, f'{role} size mismatch')
        result[role] = str(path)
    return result


def normalize_text(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text)
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def cer(reference, hypothesis):
    """Levenshtein character error rate (S+D+I)/N; may exceed 1."""
    _require(isinstance(reference, str) and isinstance(hypothesis, str), 'reference and hypothesis must be strings')
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    _require(bool(ref), 'reference is empty after normalization')
    # Each cell stores (distance, substitutions, deletions, insertions).
    previous = [(i, 0, 0, i) for i in range(len(hyp) + 1)]
    for row, char in enumerate(ref, 1):
        current = [(row, 0, row, 0)]
        for col, other in enumerate(hyp, 1):
            diagonal = previous[col - 1]
            if char == other:
                current.append(diagonal)
                continue
            substitution = (diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3])
            above, left = previous[col], current[-1]
            deletion = (above[0] + 1, above[1], above[2] + 1, above[3])
            insertion = (left[0] + 1, left[1], left[2], left[3] + 1)
            current.append(min((substitution, deletion, insertion), key=lambda cell: cell[0]))
        previous = current
    errors, substitutions, deletions, insertions = previous[-1]
    return {'cer': errors / len(ref), 'errors': errors, 'reference_characters': len(ref),
            'substitutions': substitutions, 'deletions': deletions, 'insertions': insertions}


def validate_samples(data, root):
    _require(data.get('schema_version') == 1, 'schema_version must be 1')
    samples = data.get('samples')
    _require(isinstance(samples, list) and bool(samples), 'nonempty samples array required')
    ids = set()
    # Check all authorizations before hashing or opening any sample audio.
    for sample in samples:
        _require(isinstance(sample, dict), 'sample must be an object')
        _require(sample.get('authorized') is True, 'sample authorized must be true')
        _require(_text(sample.get('id')) and sample['id'] not in ids, 'unique nonempty sample id required')
        ids.add(sample['id'])
        _require(_text(sample.get('origin')), 'explicit sample origin required')
        _require(isinstance(sample.get('license'), str) and sample['license'] in SAMPLE_LICENSES,
                 'sample license unsupported or unknown')
        if sample['license'] == 'LicenseRef-Explicit-Permission':
            _require(_text(sample.get('permission_evidence')), 'permission_evidence required for explicit permission')
        _require(_text(sample.get('reference')), 'sample reference required')
        _require(bool(normalize_text(sample['reference'])), 'sample reference empty after normalization')
    return [(sample, _checked_file(sample, root)) for sample in samples]


def read_wav(path):
    """Read only uncompressed signed PCM16, mono, 16000 Hz WAV."""
    try:
        with wave.open(str(path), 'rb') as wav:
            _require(wav.getcomptype() == 'NONE' and wav.getnchannels() == 1
                     and wav.getsampwidth() == 2 and wav.getframerate() == 16000,
                     'WAV must be uncompressed PCM signed 16-bit, mono, 16000 Hz')
            count = wav.getnframes()
            _require(count > 0, 'WAV must contain audio frames')
            raw = wav.readframes(count)
            _require(len(raw) == count * 2, 'truncated WAV PCM data')
    except (wave.Error, EOFError) as exc:
        raise ValidationError(f'invalid PCM WAV: {exc}') from exc
    pcm = array('h')
    pcm.frombytes(raw)
    if sys.byteorder != 'little':
        pcm.byteswap()
    return array('f', (value / 32768.0 for value in pcm)), count / 16000.0


def measurements(audio_seconds, wall_seconds, cpu_seconds, max_rss_bytes):
    _require(audio_seconds > 0 and wall_seconds >= 0 and cpu_seconds >= 0, 'invalid measurement')
    return {'audio_duration_seconds': audio_seconds, 'wall_inference_seconds': wall_seconds,
            'rtf': wall_seconds / audio_seconds, 'process_time_cpu_seconds': cpu_seconds,
            'max_rss_bytes': max_rss_bytes, 'max_rss_scope': 'peak_process_lifetime_not_delta'}


def _peak_rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == 'darwin' else value * 1024


def run(manifest, samples, *, consent=False, num_threads=2):
    _require(consent is True, 'explicit --consent is required before reading manifests or audio')
    _require(type(num_threads) is int and num_threads > 0, 'num_threads must be positive')
    model_data, model_root = load_manifest(manifest)
    sample_data, sample_root = load_manifest(samples)
    sample_paths = validate_samples(sample_data, sample_root)
    model_paths = validate_model(model_data, model_root)
    # Validate WAV formats before initializing the recognizer. No audio is retained globally.
    for _, path in sample_paths:
        read_wav(path)
    import sherpa_onnx
    _require(sherpa_onnx.__version__ == '2.0.1', 'this PoC requires inspected sherpa_onnx 2.0.1')
    cold_wall, cold_cpu = time.perf_counter(), time.process_time()
    compact = model_data['model']['repo'] == CTC_REPO
    factory = (sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc if compact
               else sherpa_onnx.OnlineRecognizer.from_transducer)
    recognizer = factory(
        **model_paths, num_threads=num_threads, sample_rate=16000, feature_dim=80,
        decoding_method='greedy_search', enable_endpoint_detection=False, provider='cpu')
    cold_cpu = time.process_time() - cold_cpu
    cold_wall = time.perf_counter() - cold_wall
    results = []
    for sample, path in sample_paths:
        waveform, duration = read_wav(path)
        start_wall, start_cpu = time.perf_counter(), time.process_time()
        stream = recognizer.create_stream()
        # Simulated streaming chunks, fed immediately without real-time sleeps.
        for offset in range(0, len(waveform), 3200):
            stream.accept_waveform(16000, waveform[offset:offset + 3200])
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
        # Flush right context; padding is timed but excluded from source duration.
        stream.accept_waveform(16000, array('f', [0.0]) * 10560)
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        hypothesis = recognizer.get_result(stream)
        elapsed_cpu = time.process_time() - start_cpu
        elapsed_wall = time.perf_counter() - start_wall
        result = {'id': sample['id'], 'reference': sample['reference'], 'hypothesis': hypothesis,
                  **cer(sample['reference'], hypothesis),
                  **measurements(duration, elapsed_wall, elapsed_cpu, _peak_rss_bytes())}
        results.append(result)
    audio = sum(row['audio_duration_seconds'] for row in results)
    wall = sum(row['wall_inference_seconds'] for row in results)
    cpu = sum(row['process_time_cpu_seconds'] for row in results)
    chars = sum(row['reference_characters'] for row in results)
    errors = sum(row['errors'] for row in results)
    return {'status': 'poc_inference_completed', 'sherpa_onnx_version': sherpa_onnx.__version__,
            'model_repo': model_data['model']['repo'], 'model_revision': model_data['model']['revision'],
            'recognizer_api': 'from_zipformer2_ctc' if compact else 'from_transducer',
            'runtime': {'machine': platform.machine(), 'system': platform.system(),
                        'python': platform.python_version(), 'provider': 'cpu', 'num_threads': num_threads,
                        'arm_inference_tested': platform.machine().lower().startswith(('arm', 'aarch'))},
            'cold_init_wall_seconds': cold_wall, 'cold_init_process_time_cpu_seconds': cold_cpu,
            'cold_init_scope': 'recognizer factory only; excludes Python import, hash checks, audio reads',
            'inference_scope': 'stream creation, immediate 200ms chunks, 660ms padding, decode and get_result; excludes audio reads and CER',
            'cer_normalization': CER_NORMALIZATION, 'samples': results,
            'aggregate': {**measurements(audio, wall, cpu, _peak_rss_bytes()),
                          'cer': errors / chars, 'errors': errors, 'reference_characters': chars}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, help='user-supplied pinned model JSON')
    parser.add_argument('--samples', required=True, help='user-supplied authorized sample JSON')
    parser.add_argument('--consent', action='store_true', help='authorize reading the listed samples and local benchmarking')
    parser.add_argument('--num-threads', type=int, default=2)
    args = parser.parse_args(argv)
    try:
        report = run(args.manifest, args.samples, consent=args.consent, num_threads=args.num_threads)
    except (ValidationError, OSError, ValueError, ImportError, RuntimeError) as exc:
        print(json.dumps({'status': 'rejected_or_unavailable', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
