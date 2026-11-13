#!/usr/bin/env python3
"""Time-limited, opt-in capture of real TV ASR plus actual shared route outcomes.

Private raw transcripts stay in a new 0700 directory; the public report contains
only hashes, lengths, route/status codes and observed TV state. No audio is
recorded, no test phrase is injected into ASR, and no Agent result is fabricated.
Optional cancel/reconnect tests act only after their exact spoken trigger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def private_json(path,data):
    from adbtv.execution import atomic_json
    atomic_json(path,data)
    path.chmod(0o600)


def redacted(record):
    """Only fixed, bounded evidence fields survive export. Never include text."""
    result={k:record.get(k) for k in ('index','hash','chars','received_at','source_at','origin',
                                    'asr_log_delivery_ms','route','intent','status','tv_observed_at')}
    state=record.get('tv') or {}
    result['tv']={k:state.get(k) for k in ('playing','state','package','wakefulness')}
    return result


def route_events(path,since):
    events=[]
    for candidate in (path.with_suffix('.old'),path):
        if not candidate.is_file():continue
        try:lines=candidate.read_text(encoding='utf-8').splitlines()
        except OSError:continue
        for line in lines:
            try:item=json.loads(line)
            except ValueError:continue
            if isinstance(item,dict) and type(item.get('at')) in (int,float) and item['at']>=since:
                events.append(item)
    return events


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consent-raw-text',action='store_true',help='required for this private, timed transcript capture')
    parser.add_argument('--duration',type=float,default=600)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'deploy/data')
    parser.add_argument('--private-dir',type=Path)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--remote-microphone-attested',action='store_true',help='operator states a real remote microphone is used; origin decoding reported separately')
    parser.add_argument('--allow-cancel-test',action='store_true')
    parser.add_argument('--allow-reconnect-test',action='store_true')
    args=parser.parse_args()
    if not args.consent_raw_text:parser.error('explicit --consent-raw-text required; no capture started')
    if not 10<=args.duration<=1800:parser.error('duration must be 10..1800 seconds')
    from adbtv.config import load_config
    from adbtv.cli import _ctx
    from adbtv.voice import parse_log_line,hash_text
    data=args.data_dir.expanduser().resolve()
    cfg=load_config(str(data/'config.yaml'))
    cfg.setdefault('paths',{})['data_dir']=str(data)
    cfg['execution']['state_dir']=str(data/'runtime')
    adb,device,_,_,_=_ctx(cfg)
    if adb.state()!='device':
        print('ASR_NOT_STARTED: TV ADB is not connected; turn on the TV and allow debugging',file=sys.stderr)
        return 2
    private=args.private_dir or (Path.home()/'ai-tv-butler-backups'/('b8-asr-private-'+time.strftime('%Y%m%d-%H%M%S')))
    private=private.expanduser().resolve();private.mkdir(parents=True,mode=0o700,exist_ok=False);private.chmod(0o700)
    records=[];actions=[];started=time.time();deadline=time.monotonic()+args.duration
    event_path=data/'runtime/voice_events.jsonl'
    heartbeat=data/'runtime/voice_worker.json'
    serial=cfg['adb']['serial'];base=shlex.split(cfg['adb'].get('binary') or 'adb')
    if serial:base+=['-s',serial]
    incoming=queue.Queue(maxsize=128)
    process=None;last_start=0.0;cancel_armed=False;reconnect_armed=False

    def api(path,payload=None):
        server=cfg['server'];host=server['host']
        if host in ('','0.0.0.0'):host='127.0.0.1'
        auth_value=(data/'runtime/console.token').read_text().strip()
        auth_headers={}
        auth_headers['X-Auth-Token']=auth_value
        auth_headers['Content-Type']='application/json'
        request=urllib.request.Request(f'http://{host}:{server["port"]}'+path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=auth_headers,
            method='POST' if payload is not None else 'GET')
        with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)

    def heartbeat_state():
        try:return json.loads(heartbeat.read_text())
        except (OSError,ValueError):return {}

    baseline=heartbeat_state()
    def pump(child):
        for line in child.stdout:
            utterance=parse_log_line(line)
            if utterance is None:continue
            if not utterance.source_at or not math.isfinite(utterance.source_at) or utterance.source_at<started-1:continue
            try:incoming.put_nowait(utterance)
            except queue.Full:break

    print('REAL_ASR_CAPTURE_STARTED; raw text remains private; use the actual TV remote microphone',flush=True)
    try:
        while time.monotonic()<deadline and len(records)<40:
            if process is None or process.poll() is not None:
                if process is not None:
                    process.wait(timeout=2)
                    process.stdout.close()
                if time.monotonic()-last_start<2:
                    time.sleep(.2);continue
                # No adb connect here: after a disconnect the production listener
                # itself must restore the connection, or the test will show failure.
                stamp=time.strftime('%m-%d %H:%M:%S.000',time.localtime(time.time()-1))
                process=subprocess.Popen(base+['logcat','-v','epoch','-T',stamp],stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,text=True,encoding='utf-8',errors='replace')
                threading.Thread(target=pump,args=(process,),daemon=True).start();last_start=time.monotonic()
            try:utterance=incoming.get(timeout=.5)
            except queue.Empty:utterance=None
            if utterance is not None:
                digest=hash_text(utterance.text)[:16]
                if not any(r['hash']==digest and abs(r['source_at']-utterance.source_at)<1 for r in records):
                    delivered=(utterance.at-utterance.source_at)*1000
                    record={'index':len(records)+1,'hash':digest,'chars':len(utterance.text),
                            'received_at':utterance.at,'source_at':utterance.source_at,'origin':utterance.origin,
                            'asr_log_delivery_ms':round(delivered,1) if 0<=delivered<300000 else None,
                            'asr_text':utterance.text,'route':None,'intent':None,'status':None,'tv':{}}
                    records.append(record)
                    print('REAL_ASR_OBSERVED',record['index'],'chars',record['chars'],'hash',digest,flush=True)
                    clean=utterance.text.strip().rstrip('。！!')
                    if args.allow_cancel_test and clean=='开始取消测试' and not cancel_armed:
                        try:
                            from adbtv.intent import parse_request
                            command='等20秒后打开飞牛'
                            if parse_request(command,cfg).action=='unknown':raise ValueError('unsupported fixed test command')
                            task=api('/api/runs',{'command':command})
                            actions.append({'case':'cancel_target_created','at':time.time(),'task_id':task['task_id']});cancel_armed=True
                            print('CANCEL_TARGET_RUNNING; now say 取消当前任务',flush=True)
                        except Exception:actions.append({'case':'cancel_target_failed','at':time.time()})
                    if args.allow_reconnect_test and clean=='开始重连测试' and not reconnect_armed:
                        # This disconnect is explicitly enabled by the operator.
                        reconnect_armed=True
                        prior=heartbeat_state()
                        result=subprocess.run(base+['disconnect',serial],capture_output=True,text=True,timeout=8)
                        actions.append({'case':'explicit_adb_disconnect','at':time.time(),'returncode':result.returncode,
                                        'before_adb_reconnects':(prior.get('stats') or {}).get('adb_reconnects')})
                        print('EXPLICIT_DISCONNECT_DONE; wait for production reconnect, then say 暂停播放',flush=True)
            events=route_events(event_path,started)
            now=time.time()
            for record in records:
                matches=[event for event in events if event.get('hash')==record['hash']
                         and event.get('at',0)>=record['received_at']-1 and event.get('status')]
                if matches:
                    event=min(matches,key=lambda x:x['at']);decision=event.get('decision') or {}
                    record.update(route=decision.get('route') or event.get('route'),intent=decision.get('intent'),status=event.get('status'))
                if not record.get('tv_observed_at') and (record.get('status') or now-record['received_at']>8):
                    try:
                        state=device.playback_status(include_ui=False);power=device.power_state()
                        record['tv']={'playing':state.get('playing'),'state':state.get('state'),
                                      'package':state.get('package'),'wakefulness':power.get('wakefulness')}
                        record['tv_observed_at']=time.time()
                    except Exception:record['tv']={'state':'unavailable'};record['tv_observed_at']=time.time()
            private_json(private/'raw-asr-private.json',{'capture_kind':'actual_tv_logcat_asr','started_at':started,'records':records,'actions':actions})
    except KeyboardInterrupt:
        print('CAPTURE_STOP_REQUESTED',flush=True)
    finally:
        if process is not None:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            process.stdout.close()
    for action in actions:
        if action.get('task_id'):
            try:action['final_task_state']=api('/api/runs/'+action['task_id']).get('state')
            except Exception:action['final_task_state']='unknown'
    ending=heartbeat_state()
    public_actions=[{k:v for k,v in action.items() if k!='task_id'} for action in actions]
    report={'capture_kind':'actual_tv_logcat_asr','raw_text_private':True,
            'remote_microphone_attested_by_operator':args.remote_microphone_attested,
            'started_at':started,'ended_at':time.time(),'observed_utterances':len(records),
            'no_observation_is_not_a_pass':not bool(records),
            'records':[redacted(r) for r in records],'actions':public_actions,
            'baseline_adb_reconnects':(baseline.get('stats') or {}).get('adb_reconnects'),
            'ending_adb_reconnects':(ending.get('stats') or {}).get('adb_reconnects')}
    private_json(args.report.expanduser().resolve(),report)
    print('REAL_ASR_CAPTURE_ENDED; observed',len(records),'report is redacted',flush=True)
    return 0 if records else 3


if __name__=='__main__':raise SystemExit(main())
