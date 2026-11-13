#!/usr/bin/env python3
"""V4.4 closed-loop smoke test against the REAL media library and a REAL LLM.

Drives the production dialogue stack (``VoiceDialogue``) with:

* the real local catalog (``/vol1/1000/影视``, read-only),
* the configured LLM gateway (``adbtv.llm.LLMClient``),
* a stubbed TV agent/device, so nothing is executed on the real television.

The point is to prove the chain "查资源 → 自然推荐 → 多轮选择 → 确认 → 播放"
end to end and to record what the model actually did (tool trace, latencies),
without touching the TV. Run: ``python3 scripts/v44_smoke.py``
"""
from __future__ import annotations

import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adbtv.config import load_config                      # noqa: E402
from adbtv.library_tools import LibraryTools              # noqa: E402
from adbtv.llm import LLMClient                           # noqa: E402
from adbtv.media_index import MediaIndex                  # noqa: E402
from adbtv.voice_intelligence import IntelligentVoiceRouter  # noqa: E402
from adbtv.voice_runtime import LibraryVoiceCatalog, VoiceDialogue  # noqa: E402

TURNS = [
    '现在资源库有什么？',
    '推荐三部好看的。',
    '第二个讲什么？',
    '不要太长的。',
    '就看刚才那个。',
    '第一集。',
    '确认。',
]

EVIDENCE = ROOT / 'docs' / 'evidence' / 'V44_LEAD_CLOSED_LOOP.json'


class _Device:
    """Stub TV: reports a stable, known state; never executes anything."""

    def current_package(self):
        return 'com.mitv.mivideoplayer'

    def playback_status(self, include_ui=False):
        return {'state': 'playing', 'playing': True}


def _sanitize(value):
    """Strip absolute paths and secrets from recorded evidence."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    for token in (os.environ.get('DEEPSEEK_API_KEY') or '', ):
        if token:
            text = text.replace(token, '<redacted>')
    for path in ('/vol1/1000/影视', '/media', str(ROOT)):
        text = text.replace(path, '<media-root>')
    return json.loads(text)


def build_dialogue(cfg, index, llm):
    agent = Mock()
    agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '已播放'}
    responder = Mock()
    responder.respond.return_value = {'tts_ms': 0}
    return VoiceDialogue(
        cfg,
        agent=agent,
        device=_Device(),
        catalog=LibraryVoiceCatalog(cfg, index=index),
        router=IntelligentVoiceRouter(cfg, llm=llm),
        responder=responder,
    ), agent


def main():
    cfg = load_config()
    # V4.4 enables the natural-language layer; the smoke test needs both the
    # semantic router and the read-only assistant loop switched on.
    cfg.setdefault('voice', {}).setdefault('intelligence', {})['enabled'] = True
    cfg['voice']['intelligence'].setdefault('llm_timeout_seconds', 30)
    cfg.setdefault('voice', {}).setdefault('assistant', {})['enabled'] = True
    roots = [str(p) for p in (cfg.get('media', {}).get('roots') or [])]
    if not roots:
        print('no media roots configured', file=sys.stderr)
        return 2
    started = time.monotonic()
    index = MediaIndex(roots).scan()
    scan_ms = (time.monotonic() - started) * 1000
    tools = LibraryTools(cfg, index=index)
    summary = tools.summary()
    overview = tools.overview() if hasattr(tools, 'overview') else summary
    llm = LLMClient(cfg)
    print(f'library: {json.dumps(summary, ensure_ascii=False)}')
    print(f'scan: {scan_ms:.0f} ms, llm enabled={llm.enabled} model={llm.model}')

    dialogue, agent = build_dialogue(cfg, index, llm)
    turns, latencies = [], []
    for utterance in TURNS:
        began = time.monotonic()
        result = dialogue.handle(utterance, tv_id='smoke-tv', source='text')
        elapsed = (time.monotonic() - began) * 1000
        latencies.append(elapsed)
        metrics = result.get('metrics') or {}
        record = {
            'utterance': utterance,
            'status': result.get('status'),
            'reply': result.get('reply'),
            'candidates': [{'id': c.get('id'), 'title': c.get('title'),
                            'kind': c.get('kind'), 'episodes': c.get('episodes')}
                           for c in (result.get('candidates') or [])],
            'tool_calls': result.get('tool_trace') or [],
            'decision': {k: (result.get('decision') or {}).get(k)
                         for k in ('route', 'intent', 'reason')},
            'answer_source': metrics.get('answer_source'),
            'llm_latency_ms': metrics.get('llm_latency_ms'),
            'tool_latency_ms': metrics.get('tool_latency_ms'),
            'turn_ms': round(elapsed, 1),
        }
        turns.append(record)
        print(f'\n[{utterance}] -> {record["status"]}  ({elapsed:.0f} ms)')
        print(f'  reply: {record["reply"]}')
        if record['tool_calls']:
            print('  tools: ' + ', '.join(c['tool'] for c in record['tool_calls']))

    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    evidence = _sanitize({
        'version': '4.4.0',
        'library_summary': summary,
        'library_overview': overview,
        'model': llm.model,
        'scan_ms': round(scan_ms, 1),
        'turns': turns,
        'metrics': {
            'turn_ms_p50': round(statistics.median(latencies), 1),
            'turn_ms_p95': round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1),
            'peak_rss_mb': round(peak_mb, 1),
        },
        'verified': {
            'real_media_library': True,
            'real_llm': True,
            'real_tv_execution': False,
            'note': 'agent/device are stubs: playback was confirmed by the dialogue '
                    'stack but not executed on the physical TV.',
        },
    })
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nevidence: {EVIDENCE}')
    print(f'metrics: {json.dumps(evidence["metrics"], ensure_ascii=False)}')
    print(f'agent.run calls (playback executions): {agent.run.call_count}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
