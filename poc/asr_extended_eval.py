#!/usr/bin/env python3
"""Phase 2 metrics layered on the existing offline ASR reports.

Consumes real per-sample hypotheses from poc.asr_lab reports. It never changes
those hypotheses or claims microphone latency. Intent accuracy uses the production
parser only for context-free explicit commands; context-dependent commands are
reported separately instead of being silently counted as failures or successes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from adbtv.config import load_config  # noqa: E402
from adbtv.intent import parse_request  # noqa: E402
from poc.title_session import TitleSessionStore  # noqa: E402


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = max(0, min(len(values) - 1, int(round(fraction * (len(values) - 1)))))
    return values[index]


def _sample_metadata(samples_path):
    data = json.loads(Path(samples_path).read_text(encoding='utf-8'))
    rows = {}
    for sample in data.get('samples', []):
        if not isinstance(sample, dict) or not isinstance(sample.get('id'), str):
            raise ValueError('sample manifest has invalid sample')
        rows[sample['id']] = sample
    if not rows:
        raise ValueError('sample manifest is empty')
    return data, rows


def evaluate(report_paths, samples_path, store, *, config_path=None):
    manifest, metadata = _sample_metadata(samples_path)
    cfg = load_config(str(config_path or ROOT / 'config.example.yaml'))
    reports = [json.loads(Path(path).read_text(encoding='utf-8')) for path in report_paths]
    if not reports:
        raise ValueError('at least one model report is required')
    sample_ids = None
    output = []
    for report in reports:
        if report.get('status') != 'offline_evaluation_completed':
            raise ValueError('report is not a completed offline evaluation')
        rows = report.get('samples') or []
        ids = [row.get('id') for row in rows]
        if not rows or len(ids) != len(set(ids)):
            raise ValueError('report samples are empty or duplicated')
        if sample_ids is None:
            sample_ids = set(ids)
        elif set(ids) != sample_ids:
            raise ValueError('reports do not contain the same samples')
        enriched = []
        for row in rows:
            meta = metadata.get(row['id'], {})
            hypothesis = row.get('hypothesis')
            if not isinstance(hypothesis, str):
                raise ValueError('report row lacks hypothesis')
            expected = meta.get('expected_intent')
            action = None
            if expected and not meta.get('context_required'):
                try:
                    action = parse_request(hypothesis, cfg).action
                except Exception:
                    action = 'unknown'
            title = store.suggest(f"eval-{row['id']}", hypothesis, current=None)
            works = title.get('works') or []
            expected_title = meta.get('expected_title')
            top1 = bool(expected_title and works and works[0].get('title') == expected_title)
            top3 = bool(expected_title and any(w.get('title') == expected_title for w in works[:3]))
            false_suggestion = bool(
                meta.get('kind') == 'control' and title.get('status') == 'suggested' and works
            )
            rejected = title.get('status') == 'rejected'
            clarified = title.get('status') == 'needs_clarification'
            enriched.append({
                'id': row['id'], 'condition': row.get('condition'),
                'expected_intent': expected, 'predicted_intent': action,
                'intent_correct': (action == expected) if action is not None else None,
                'context_required': bool(meta.get('context_required')),
                'expected_title': expected_title, 'title_top1': top1, 'title_top3': top3,
                'false_suggestion': false_suggestion, 'title_rejected': rejected,
                'title_needs_clarification': clarified,
                'title_status': title.get('status'), 'title_reason': title.get('reason'),
                'cer': row.get('cer'), 'characters': row.get('characters'),
                'audio_seconds': row.get('audio_seconds'), 'wall_seconds': row.get('wall_seconds'),
                'first_partial_seconds': row.get('first_partial_seconds'),
            })
        conditions = {}
        for condition in sorted({row.get('condition') for row in enriched if row.get('condition')}):
            subset = [row for row in enriched if row.get('condition') == condition]
            conditions[condition] = _aggregate(subset)
        output.append({'profile': report.get('profile'), 'model': report.get('model'),
                       'samples': enriched, 'by_condition': conditions,
                       'overall': _aggregate(enriched)})
    return {
        'status': 'phase2_metrics_completed', 'evaluated_at': datetime.now(timezone.utc).isoformat(),
        'capture_kind': 'offline_file_replay', 'realtime_microphone_tested': False,
        'sample_manifest_sha256': __import__('hashlib').sha256(Path(samples_path).read_bytes()).hexdigest(),
        'corpus': {'samples': len(sample_ids), 'path': str(Path(samples_path)),
                   'declared_speakers': manifest.get('speakers'),
                   'conditions': manifest.get('conditions'),
                   'note': 'This report does not turn synthetic or file-replay audio into human microphone evidence.'},
        'models': output,
        'limitations': [
            'Intent accuracy is only for context-free explicit commands; context-dependent samples are reported separately.',
            'Title Top-1/Top-3 only counts samples with an expected title; the current corpus has very few.',
            'File replay latency is not microphone end-to-end latency.',
        ],
    }


def _aggregate(rows):
    chars = sum(row['characters'] or 0 for row in rows)
    errors = sum(round((row['cer'] or 0) * (row['characters'] or 0)) for row in rows)
    intent_rows = [row for row in rows if row['intent_correct'] is not None]
    title_rows = [row for row in rows if row['expected_title']]
    control_rows = [row for row in rows if row['expected_intent'] and row['expected_title'] is None]
    walls = [row['wall_seconds'] for row in rows if isinstance(row['wall_seconds'], (int, float))]
    partials = [row['first_partial_seconds'] for row in rows
                if isinstance(row['first_partial_seconds'], (int, float))]
    return {
        'samples': len(rows), 'cer': errors / chars if chars else None,
        'intent_accuracy': (sum(row['intent_correct'] for row in intent_rows) / len(intent_rows)
                            if intent_rows else None), 'intent_evaluated': len(intent_rows),
        'context_required': sum(bool(row['context_required']) for row in rows),
        'title_top1': (sum(row['title_top1'] for row in title_rows) / len(title_rows)
                       if title_rows else None), 'title_top3': (sum(row['title_top3'] for row in title_rows) / len(title_rows)
                                                                 if title_rows else None),
        'title_samples': len(title_rows),
        'false_suggestions': sum(row['false_suggestion'] for row in control_rows),
        'control_samples': len(control_rows),
        'false_suggestion_rate': (sum(row['false_suggestion'] for row in control_rows) / len(control_rows)
                                  if control_rows else None),
        'title_rejection_rate': (sum(row['title_rejected'] for row in rows) / len(rows) if rows else None),
        'clarification_rate': (sum(row['title_needs_clarification'] for row in rows) / len(rows) if rows else None),
        'latency': {'p50_wall_seconds': percentile(walls, .5), 'p95_wall_seconds': percentile(walls, .95),
                    'p50_first_partial_seconds': percentile(partials, .5),
                    'p95_first_partial_seconds': percentile(partials, .95)},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='append', required=True, help='poc.asr_lab report JSON')
    parser.add_argument('--samples', required=True)
    parser.add_argument('--index', help='MediaIndex cache')
    parser.add_argument('--root', action='append', help='Read-only media roots')
    parser.add_argument('--path-map', help='JSON prefix map for cache paths')
    parser.add_argument('--config', help='Config for the production parser (no TV access)')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if Path(args.output).exists():
            raise ValueError('refusing to overwrite an existing report')
        path_map = json.loads(args.path_map) if args.path_map else None
        store = (TitleSessionStore.from_cache(args.index, path_map=path_map)
                 if args.index else TitleSessionStore.from_roots(args.root))
        result = evaluate(args.report, args.samples, store, config_path=args.config)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': result['status'], 'models': len(result['models']),
                          'samples': result['corpus']['samples']}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'PHASE2_METRICS_REJECTED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())