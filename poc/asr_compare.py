#!/usr/bin/env python3
"""Compare sherpa-onnx against recorded human speech, condition by condition.

This never replaces the Xiaomi assistant: it produces a measurement report for a
decision, using the same consent/provenance gates as the standalone PoC.

Sample manifest adds, per sample:
  condition: "clean" | "far-field" | "noise"
  proper_nouns: ["穿成野神"]        # names that must survive recognition
  speaker: "adult-female"           # coarse label only, never a name

Latency reported separately:
  first_recognition_seconds  — cold recognizer + first decode of sample 1
  first_decode_seconds       — time to the first non-empty hypothesis
  streaming_rtf              — wall time / audio duration for that sample
"""
from __future__ import annotations

import argparse
from array import array
import json
import platform
import resource
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import version
import wave
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from poc.sherpa_asr import (  # noqa: E402 - reuse the audited gates
    ValidationError, _checked_file, _hash_file, load_manifest, validate_model, validate_samples,
    read_wav, CER_NORMALIZATION, _peak_rss_bytes, normalize_text,
)

CONDITIONS=('clean','far-field','noise')


def character_metrics(reference, hypothesis):
    """JiWER/RapidFuzz minimum Levenshtein edits after our shared normalization."""
    if not isinstance(reference, str) or not isinstance(hypothesis, str):
        raise ValidationError('reference and hypothesis must be strings')
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        raise ValidationError('reference is empty after normalization')
    from jiwer import process_characters
    result = process_characters(ref, hyp)
    edits = []
    for chunk in result.alignments[0]:
        a = ref[chunk.ref_start_idx:chunk.ref_end_idx]
        b = hyp[chunk.hyp_start_idx:chunk.hyp_end_idx]
        if chunk.type == 'substitute':
            edits.extend([x, y] for x, y in zip(a, b))
        elif chunk.type == 'delete':
            edits.extend([x, None] for x in a)
        elif chunk.type == 'insert':
            edits.extend([None, y] for y in b)
    errors = result.substitutions + result.deletions + result.insertions
    return {'cer': result.cer, 'edits': edits, 'characters': len(ref),
            'errors': errors, 'substitutions': result.substitutions,
            'deletions': result.deletions, 'insertions': result.insertions}


def cer(reference, hypothesis):
    """Compatibility tuple: CER, complete minimum-edit alignment, reference size."""
    result = character_metrics(reference, hypothesis)
    return result['cer'], result['edits'], result['characters']


def contains_reference(reference, hypothesis):
    """Operational recall: is the whole normalized expected command present?"""
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    return bool(ref) and ref in hyp


def aggregate(subset):
    chars = sum(r['characters'] for r in subset)
    counts = {key: sum(r[key] for r in subset)
              for key in ('errors', 'substitutions', 'deletions', 'insertions')}
    audio = sum(r['audio_seconds'] for r in subset)
    wall = sum(r['wall_seconds'] for r in subset)
    nouns = [v for r in subset for v in r['proper_nouns'].values()]
    return {'samples': len(subset), 'cer': counts['errors'] / chars if chars else None,
            **counts, 'command_recalled': sum(bool(r['command_recalled']) for r in subset),
            'characters': chars, 'audio_seconds': audio, 'wall_seconds': wall,
            'rtf': wall / audio if audio else None,
            'proper_noun_hits': sum(bool(v) for v in nouns), 'proper_noun_total': len(nouns)}


def recognize(recognizer,audio):
    """Same streaming shape as the audited PoC: 200 ms chunks, then padding."""
    stream=recognizer.create_stream()
    started=time.perf_counter();first_decode=None
    for offset in range(0,len(audio),3200):
        stream.accept_waveform(16000,audio[offset:offset+3200])
        while recognizer.is_ready(stream):recognizer.decode_stream(stream)
        if first_decode is None:
            partial=recognizer.get_result(stream)
            if partial and partial.strip():first_decode=time.perf_counter()-started
    stream.accept_waveform(16000,array('f',[0.0])*10560)
    stream.input_finished()
    while recognizer.is_ready(stream):recognizer.decode_stream(stream)
    return recognizer.get_result(stream),time.perf_counter()-started,first_decode


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--samples',required=True)
    parser.add_argument('--consent',action='store_true')
    parser.add_argument('--num-threads',type=int,default=2)
    parser.add_argument('--report',required=True)
    args=parser.parse_args(argv)
    if not args.consent:parser.error('--consent required; no files were read')

    model,root=load_manifest(args.manifest)
    files=validate_model(model,root)
    declared=model['model']
    info={**files,'repo':declared['repo'],'revision':declared['revision'],
          'license':declared['license'],'license_evidence':declared['license_evidence'],
          'provenance_evidence':declared.get('provenance_evidence'),
          'kind':'ctc' if 'model' in files else 'transducer'}
    samples_root=Path(args.samples).resolve().parent
    samples=validate_samples(json.loads(Path(args.samples).read_text(encoding='utf-8')),samples_root)

    import sherpa_onnx
    init_started=time.perf_counter()
    if info['kind']=='ctc':
        recognizer=sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
            tokens=info['tokens'],model=info['model'],num_threads=args.num_threads,
            sample_rate=16000,feature_dim=80,decoding_method='greedy_search')
    else:
        recognizer=sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=info['tokens'],encoder=info['encoder'],decoder=info['decoder'],
            joiner=info['joiner'],num_threads=args.num_threads,sample_rate=16000,feature_dim=80,
            decoding_method='greedy_search')
    init_seconds=time.perf_counter()-init_started

    rows=[];first=None
    for index,(sample,path) in enumerate(samples):
        audio,seconds=read_wav(path)
        text,wall,first_decode=recognize(recognizer,audio)
        metrics=character_metrics(sample['reference'],text)
        recalled=contains_reference(sample['reference'],text)
        nouns={noun:contains_reference(noun,text) for noun in sample.get('proper_nouns') or []}
        if index==0:first={'cold_init_seconds':init_seconds,'first_sample_wall_seconds':wall,
                           'first_decode_seconds':first_decode}
        rows.append({'id':sample['id'],'condition':sample.get('condition','clean'),
                     'speaker':sample.get('speaker'),**metrics,'audio_sha256':sample['sha256'],
                     'proper_nouns':nouns,'command_recalled':recalled,'hypothesis':text,
                     'audio_seconds':seconds,'wall_seconds':wall,'rtf':wall/seconds,
                     'first_decode_seconds':first_decode})
        print('SAMPLE',sample['id'],sample.get('condition','clean'),f"cer={metrics['cer']:.3f}",flush=True)

    by_condition={c:aggregate([r for r in rows if r['condition']==c]) for c in CONDITIONS}
    report={'status':'comparison_completed',
            'evaluated_at':datetime.now(timezone.utc).isoformat(),
            'sample_manifest_sha256':_hash_file(Path(args.samples)),
            'metric':{'name':'Levenshtein CER','implementation':'jiwer.process_characters',
                      'jiwer_version':version('jiwer'),'rapidfuzz_version':version('rapidfuzz'),
                      'aggregation':'sum(S+D+I)/sum(normalized_reference_characters)'},
            'model':{'repo':info['repo'],'revision':info['revision'],'license':info['license'],
                     'license_evidence':info['license_evidence'],'provenance':info['provenance_evidence']},
            'sherpa_onnx_version':sherpa_onnx.__version__ if hasattr(sherpa_onnx,'__version__') else None,
            'runtime':{'machine':platform.machine(),'python':platform.python_version(),
                       'num_threads':args.num_threads,'provider':'cpu'},
            'cer_normalization':CER_NORMALIZATION,
            'latency':{**first,'continuous_samples_mean_seconds':
                       (sum(r['wall_seconds'] for r in rows[1:])/max(1,len(rows)-1)),
                       'note':'first_decode_seconds is time to a non-empty partial, not final text'},
            'cpu_time_seconds':time.process_time(),'peak_rss_bytes':_peak_rss_bytes(),
            'peak_rss_scope':'peak_process_lifetime_not_delta',
            'overall':aggregate(rows),'by_condition':by_condition,'samples':rows,
            'xiaomi_comparison':'Not measured here: Xiaomi transcripts come from real remote-mic logcat; '
                                'this report only measures sherpa-onnx on the supplied audio.'}
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('ASR_COMPARISON_WRITTEN',args.report,flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())