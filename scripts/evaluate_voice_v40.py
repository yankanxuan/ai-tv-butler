#!/usr/bin/env python3
"""Routing-only V4 measurements. Never sends commands to a TV.

Default uses the deterministic Chinese corpus fixtures. --real-model opts into
requests to the configured model endpoint, reporting schema validity and safe
semantic outcomes separately. Neither mode measures ASR recognition accuracy.
"""
from __future__ import annotations
import argparse
import copy
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def percentile(values,p):
    values=sorted(values)
    return round(values[max(0,math.ceil(len(values)*p)-1)],3) if values else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-model',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--interval',type=float,default=.25,help='spacing for real-model cases; zero tests bursts')
    args=parser.parse_args()
    rows=[]
    if args.real_model:
        from adbtv.config import load_config
        from adbtv.voice_intelligence import IntelligentVoiceRouter
        cfg=copy.deepcopy(load_config())
        cfg.setdefault('voice',{}).setdefault('intelligence',{})['enabled']=True
        router=IntelligentVoiceRouter(cfg)
        cases=[('找部悬疑剧','search'),('找个科幻电影','search'),('国产的','clarify'),
               ('第二部','clarify'),('不要这个','clarify'),
               ('打开B站然后把音量调到8','sequence'),
               ('关闭客厅的灯并且暂停视频','clarify'),('那个接着播','clarify')]
        for index,(text,expected) in enumerate(cases):
            if index and args.interval>0:time.sleep(min(args.interval,10))
            started=time.monotonic();decision=router.decide(text,{})
            matched=(decision.route=='clarify') if expected=='clarify' else decision.intent==expected
            row=dict(id=f'real_{index+1}',expected=expected,matched=matched,
                     elapsed_ms=(time.monotonic()-started)*1000,**decision.audit())
            # A timeout's generic clarification is safe, but is not comprehension accuracy.
            row['semantic_match']=bool(matched and decision.source=='llm')
            rows.append(row)
            print(json.dumps(row,ensure_ascii=False),flush=True)
        mode='configured_model_no_adb'
    else:
        from tests.test_voice_corpus_v40 import CORPUS,evaluate_fixture_case
        for case in CORPUS:
            started=time.monotonic();decision,llm,_=evaluate_fixture_case(case)
            rows.append(dict(id=case['id'],category=case['category'],mode=case['mode'],
                expected_route=case['expected_route'],matched=decision.route==case['expected_route'],
                llm_calls=len(llm.requests),elapsed_ms=(time.monotonic()-started)*1000,
                **decision.audit()))
        mode='deterministic_fixture_not_model_accuracy'
    values=[r['elapsed_ms'] for r in rows]
    summary={'mode':mode,'cases':len(rows),'route_agreement_percent':round(sum(r['matched'] for r in rows)*100/len(rows),2),
             'latency_ms':{'p50':percentile(values,.5),'p95':percentile(values,.95),'p99':percentile(values,.99)},
             'asr_accuracy':None,'adb_e2e_success':None,'tts_latency':None}
    if args.real_model:
        summary['schema_valid_model_responses']=sum(r['source']=='llm' for r in rows)
        summary['semantic_match_percent']=round(sum(r['semantic_match'] for r in rows)*100/len(rows),2)
    report={'summary':summary,'rows':rows}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
