#!/usr/bin/env python3
"""Sequential paired ASR comparison; one fresh process per reviewed model."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from poc.asr_lab import private_json
from poc.sherpa_asr import ValidationError


def compare(manifests, samples, output_dir, *, consent=False, vad=False,
            num_threads=2, title_index=None, timeout=300):
    if consent is not True:
        raise ValidationError('Explicit consent required before any sample read')
    if len(manifests) < 2:
        raise ValidationError('At least two distinct model manifests required')
    resolved = [str(Path(p).resolve()) for p in manifests]
    if len(set(resolved)) != len(resolved):
        raise ValidationError('Duplicate model manifest')
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    reports, failures = [], []
    for index, manifest in enumerate(resolved):
        report_path = root / f'model-{index + 1}.json'
        command = [sys.executable, '-m', 'poc.asr_lab', 'evaluate', '--manifest', manifest,
                   '--samples', str(Path(samples).resolve()), '--report', str(report_path),
                   '--consent', '--num-threads', str(num_threads)]
        if vad:
            command.append('--vad')
        if title_index:
            command.extend(['--title-index', str(Path(title_index).resolve())])
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    cwd=Path(__file__).resolve().parents[1], timeout=timeout)
        except subprocess.TimeoutExpired:
            failures.append({'model_number': index + 1, 'reason': 'evaluation_timeout'})
            continue
        if result.returncode or not report_path.is_file():
            failures.append({'model_number': index + 1, 'reason': 'evaluation_failed',
                             'exit_code': result.returncode})
            # Avoid leaking raw user paths or transcripts from a native decoder.
            continue
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
            required = {'profile', 'model', 'model_registry_sha256', 'sample_manifest_sha256',
                        'runtime', 'initialization_seconds', 'initialization_cpu_seconds',
                        'inference_cpu_seconds', 'peak_rss_bytes', 'peak_rss_scope',
                        'overall', 'by_condition', 'samples'}
            valid = (isinstance(report, dict) and required <= report.keys()
                     and report.get('status') == 'offline_evaluation_completed'
                     and isinstance(report['samples'], list) and bool(report['samples'])
                     and report['overall']['samples'] == len(report['samples'])
                     and len({row['id'] for row in report['samples']}) == len(report['samples']))
            if not valid:
                raise ValueError('Invalid or incomplete evaluation report')
        except (ValueError, KeyError, TypeError):
            failures.append({'model_number': index + 1, 'reason': 'evaluation_report_invalid'})
            continue
        reports.append(report)
    if reports:
        identities = [{(r['id'], r['audio_sha256']) for r in report['samples']} for report in reports]
        if any(ids != identities[0] for ids in identities[1:]) or any(
                report['sample_manifest_sha256'] != reports[0]['sample_manifest_sha256'] for report in reports):
            failures.append({'reason': 'samples_changed_between_engines'})
        if len({report['profile'] for report in reports}) != len(reports):
            failures.append({'reason': 'duplicate_profile'})
    summary = {'status': 'paired_comparison_completed' if not failures and len(reports) == len(manifests) else 'paired_comparison_incomplete',
               'evaluated_at': datetime.now(timezone.utc).isoformat(),
               'capture_kind': 'offline_file_replay', 'realtime_microphone_tested': False,
               'execution': 'sequential_isolated_processes', 'vad_enabled': vad,
               'failures': failures, 'models': [{k: r[k] for k in (
                   'profile', 'model', 'model_registry_sha256', 'sample_manifest_sha256',
                   'runtime', 'initialization_seconds', 'initialization_cpu_seconds',
                   'inference_cpu_seconds', 'peak_rss_bytes', 'peak_rss_scope',
                   'overall', 'by_condition')} for r in reports],
               'xiaomi_comparison': 'Parallel existing production path retained. No same-audio Xiaomi A/B or microphone latency claim.',
               'production_default': 'xiaomi_unchanged'}
    private_json(root / 'comparison.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', action='append', required=True)
    parser.add_argument('--samples', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--consent', action='store_true')
    parser.add_argument('--vad', action='store_true')
    parser.add_argument('--num-threads', type=int, default=2)
    parser.add_argument('--title-index')
    args = parser.parse_args(argv)
    try:
        result = compare(args.manifest, args.samples, args.output_dir, consent=args.consent,
                         vad=args.vad, num_threads=args.num_threads, title_index=args.title_index)
        print(json.dumps({'status': result['status'], 'models': len(result['models']),
                          'failures': result['failures']}, ensure_ascii=False))
        return 0 if result['status'] == 'paired_comparison_completed' else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'ASR_COMPARISON_REJECTED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
