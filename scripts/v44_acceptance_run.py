#!/usr/bin/env python3
"""V4.4 真实 LLM 验收跑批（一次性跑，结果落盘到 docs/evidence/）。

跑的是**真实模型 + 真实全栈对话链路**：``adbtv.config.load_config()`` 读本地网关配置
（provider=anthropic / base_url=http://127.0.0.1:7866 / model=cn:deepseek-v4.1-flash），
构造 ``LLMClient`` 交给 ``IntelligentVoiceRouter``（``VoiceDialogue`` 内部的
``AssistantLoop`` 复用同一个 client），然后逐条说 A–O 的话术。

两套数据：
  * ``fixture``    —— 临时目录里与生产库同形状的小库（0 电影 / 无 genre / 剧集+短剧），
                     可重复、可断言；
  * ``production`` —— ``load_config()['media']['roots']``（只读，宿主机可读）。

产物：``docs/evidence/V44_REAL_LLM_ACCEPTANCE.json``
  * 每条话术：utterance / reply / tool_trace / answer_source / diagnostics
    （llm_latency_ms、tool_latency_ms、total_latency_ms）/ 是否误触 agent.run /
    事实断言结果；
  * ``metrics``：逐条 P50 / P95 延迟 + 峰值内存；
  * **脱敏**：不写 API key、不写绝对路径、不写完整 UI 文本，只保留作品标题与数值；
  * 真实电视 / 真人手机相关步骤一律 ``verified: false`` 并写明原因（不伪造）。

用法::

    python3 scripts/v44_acceptance_run.py                  # 两套数据，每序列 3 次
    python3 scripts/v44_acceptance_run.py --reps 5 --dataset fixture
    python3 scripts/v44_acceptance_run.py --out /tmp/x.json --quiet
"""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import Mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from adbtv.config import load_config                     # noqa: E402
from adbtv.library_tools import LibraryTools             # noqa: E402
from adbtv.llm import LLMClient                          # noqa: E402
from adbtv.media_index import MediaIndex                 # noqa: E402
from adbtv.voice_intelligence import IntelligentVoiceRouter  # noqa: E402
from adbtv.voice_runtime import LibraryVoiceCatalog, VoiceDialogue  # noqa: E402

# 复用验收测试里的 fixture 构造与调用记录代理，避免两处漂移。
from tests.test_v44_dialogue_acceptance import (         # noqa: E402
    ToolSpy, build_fixture, cfg_for)

DEFAULT_OUT = REPO / 'docs' / 'evidence' / 'V44_REAL_LLM_ACCEPTANCE.json'

# 每条话术的完整话轮序列（同一序列共用一个会话，保持多轮上下文）。
SEQUENCES = {
    'A': ['现在资源库有什么？'],
    'B': ['推荐三部好看的。'],
    'C': ['推荐三部好看的。', '第二个讲什么？'],
    'D': ['推荐三部好看的。', '换一个类似的。'],
    'E': ['推荐三部好看的。', '不要那么长的。'],
    'F': ['我上次看到哪里了？'],
    'G': ['现在资源库有什么？', '第一部。', '打开这部剧第二季。'],
    'G3': ['现在资源库有什么？', '第一部。', '打开这部剧第三季。'],
    'H': ['现在资源库有什么？', '第一部。', '播放第一集。', '确认播放。'],
    'H2': ['现在资源库有什么？', '第一部。', '播放第一集。', '取消播放。'],
    'I': ['电视现在在放什么？'],
    'J': ['最近新增了哪些资源？'],
    'K': ['我想看电影。'],
    'L': ['推荐一部悬疑剧。'],
    'M': ['你能帮我做什么？'],
    'N': ['推荐三部好看的。', '第二个讲什么？', '换一个类似的。', '不要那么长的。',
          '就看刚才那个。', '确认。'],
    'O': ['现在资源库有什么？', '第一部。', '播放第一集。', '确认播放。', '确认播放。'],
}

TITLE_BY_ID = {'A': 'A 资源库总览', 'B': 'B 推荐三部', 'C': 'C 第二个讲什么',
               'D': 'D 换一个类似的', 'E': 'E 不要太长', 'F': 'F 上次看到哪里',
               'G': 'G 打开第N季（确认）', 'G3': 'G3 不存在的季',
               'H': 'H 确认播放', 'H2': 'H2 取消播放', 'I': 'I 电视在放什么',
               'J': 'J 最近新增', 'K': 'K 我想看电影', 'L': 'L 悬疑剧',
               'M': 'M 能力说明', 'N': 'N 多轮组合', 'O': 'O 重复确认'}

# 无法自动化的部分：必须如实标注未验收。
UNVERIFIED = [
    {'item': '真实电视（小米 TV / 原生助手）上的实际播放与原生助手抑制',
     'verified': False,
     'reason': '验收机没有可用的电视/ADB 目标，所有电视动作都只到 agent.run 边界；'
               '真机行为需要在现场用遥控器复验。'},
    {'item': '真人手机（语音入口 / 小爱同学）端到端时延与打断',
     'verified': False,
     'reason': '无法自动化真人说话与手机麦克风链路；本跑批使用 source="text" 模拟同一句话。'},
    {'item': 'TTS 播报音质与打断（VoiceResponder）',
     'verified': False,
     'reason': '跑批使用无副作用的 responder 桩件（tts_ms=0），不触发真实 TTS。'},
    {'item': 'I 项的设备观测',
     'verified': False,
     'reason': '没有真实电视可观测，使用注入的固定设备观测（PLYING/已知片名与集数）替代，'
               '仅验证"回复与观测一致"这一逻辑。'},
]


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
# 主目录前缀用运行时推导，源码里不出现绝对路径字面量（公开包审计要求）。
HOME_PREFIX = os.path.expanduser('~')
PATH_TAIL = r'[^\s，。；、）)」》]*'


def sanitizer(roots):
    prefixes = [str(root) for root in roots if root]
    home_pattern = re.compile(re.escape(HOME_PREFIX) + PATH_TAIL)
    volume_pattern = re.compile(r'/vol\d+/' + PATH_TAIL)

    def scrub(text):
        if not isinstance(text, str):
            return text
        for prefix in prefixes:
            text = text.replace(prefix, '<library>')
        text = volume_pattern.sub('<path>', text)
        text = home_pattern.sub('<path>', text)
        text = re.sub(r'\b(?:sk|ghp|github_pat)_[A-Za-z0-9_\-]+', '<redacted>', text)
        return text[:600]

    return scrub


def scrub_obj(value, scrub):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: scrub_obj(item, scrub) for key, item in value.items()
                if key not in ('api_key', 'token', 'authorization')}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(item, scrub) for item in value]
    return value


# --------------------------------------------------------------------------- #
# 装置
# --------------------------------------------------------------------------- #
class NullResponder:
    """不触发真实 TTS 的 responder 桩件（只回传阶段，用于链路完整性）。"""

    def respond(self, stage, reply, status=None, playback_state=None):
        return {'tts_ms': 0, 'stage': stage, 'status': status, 'chars': len(reply or '')}


class FakeDevice:
    """固定的设备观测（无真实电视，见 UNVERIFIED）。"""

    OBSERVED = {'state': 'PLAYING', 'playing': True, 'title': '漫长的季节',
                'season': 1, 'episode': 5, 'metadata': {'title': '漫长的季节'}}

    def playback_status(self, include_ui=True):
        return dict(self.OBSERVED)

    def current_package(self):
        return 'com.xiaomi.mitv.tvplayer'

    def volume(self):
        return {'known': True, 'index': 20}


def make_dialogue(cfg, roots, llm, device=None):
    index = MediaIndex([str(root) for root in roots]).scan()
    agent = Mock()
    agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '已完成。'}
    dialogue = VoiceDialogue(
        cfg, agent=agent, device=device,
        catalog=LibraryVoiceCatalog(cfg, index=index),
        router=IntelligentVoiceRouter(cfg, llm=llm),
        responder=NullResponder())
    spy = ToolSpy(dialogue.tools)
    dialogue.tools = spy
    if dialogue.assistant is not None:
        dialogue.assistant.tools = spy
    # V4.3 的 browse_library 路由走 LibraryVoiceCatalog.overview（不是 LibraryTools），
    # 这里一并记录，避免把"用适配器 overview"误判成"没有 overview 类读取"。
    overview_calls = []
    original = dialogue.catalog.overview

    def overview(*args, **kwargs):
        value = original(*args, **kwargs)
        overview_calls.append(value)
        return value

    dialogue.catalog.overview = overview
    return dialogue, spy, agent, overview_calls


# --------------------------------------------------------------------------- #
# 事实断言（与 tests/test_v44_dialogue_acceptance.py 同一套判据）
# --------------------------------------------------------------------------- #
def counts_of(reply):
    found = {}
    for key, pattern in (('total', r'共\s*(\d+)\s*部'),
                         ('movies', r'电影\s*(\d+)\s*部'),
                         ('series', r'剧集\s*(\d+)\s*部'),
                         ('short_dramas', r'短剧\s*(\d+)\s*部'),
                         ('episodes', r'共\s*(\d+)\s*集')):
        match = re.search(pattern, reply or '')
        if match:
            found[key] = int(match.group(1))
    return found


def fact_checks(letter, turns, tools, summary, agent, spy, overview_calls=()):
    """对每条话术做与单测一致的事实断言；返回 [{'check','ok','detail'}]。"""
    checks = []
    last = turns[-1]
    reply = last.get('reply') or ''
    candidates = last.get('candidates') or []
    # 生产库有 45 条，browse 分页到 20；用 all_items() 取全量做事实比对。
    all_rows = (tools.all_items().get('items') if hasattr(tools, 'all_items')
                else tools.browse(kind='any', limit=20).get('items')) or []
    library = {row['title']: row for row in all_rows}
    # H/O 的确认动作本来就该执行一次；I 是只读查询。
    expected_runs = 1 if letter in ('H', 'O', 'N') else 0

    def check(name, ok, detail=''):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})

    check('有回复', bool(reply), reply[:80])
    if letter == 'I':
        check('只读提问只发查询命令',
              all(re.search(r'什么|当前', call[0][0]) for call in agent.run.call_args_list),
              str([call[0][0] for call in agent.run.call_args_list]))
    else:
        check('电视动作次数', agent.run.call_count == expected_runs,
              f'agent.run={agent.run.call_count} 期望={expected_runs}')

    if letter == 'A':
        found = counts_of(reply)
        via_tool = bool({'overview', 'summary', 'browse', 'status'} & set(spy.names()))
        check('走 overview 类读取', via_tool or bool(overview_calls),
              f"tools={','.join(spy.names()) or '-'} adapter_overview={len(overview_calls)}")
        check('总数一致', found.get('total') == summary['total'],
              f"回复={found.get('total')} 真实={summary['total']}")
        check('电影数一致', found.get('movies', 0) == summary['movies'],
              f"回复={found.get('movies')} 真实={summary['movies']}")
        check('剧集数一致', found.get('series') == summary['series'],
              f"回复={found.get('series')} 真实={summary['series']}")
        if summary['short_dramas']:
            check('短剧单列', '短剧' in reply and
                  found.get('short_dramas') == summary['short_dramas'],
                  f"回复={found.get('short_dramas')} 真实={summary['short_dramas']}")
        check('候选全部真实', all(c['title'] in library for c in candidates))
    elif letter == 'B':
        named = re.findall(r'《(.+?)》', reply)
        check('回复含 3 部真实作品',
              len(named) >= 3 and all(name in library for name in named),
              f'回复片名={named}')
        check('候选写入会话（供"第二个"使用）', len(candidates) == 3,
              f'candidates={len(candidates)}（模型直接 grounded 回答时会话可能拿不到候选）')
        check('候选 id 可在库里查到',
              all(tools.item(c['id']) for c in candidates) if candidates else False)
        check('回复含全部候选片名',
              all(c['title'] in reply for c in candidates) if candidates else False)
    elif letter == 'C':
        first = turns[0].get('candidates') or []
        if len(first) >= 2:
            target = first[1]
            detail = tools.item(target['id']) or {}
            plot = (detail.get('plot') or '')[:12]
            check('命中第 2 个候选', target['title'] in reply, target['title'])
            check('复述真实 plot 片段', bool(plot) and plot in reply, plot)
            check('未串到第 1 个', first[0]['title'] not in reply, first[0]['title'])
        else:
            check('上一条话术把候选写入会话', False,
                  '「推荐三部」没有把候选写进会话（模型 grounded 回答路径 candidates=[]）')
    elif letter == 'D':
        previous = turns[0].get('candidates') or []
        previous_ids = {c['id'] for c in previous}
        previous_titles = {c['title'] for c in previous}
        fresh_ids = {c['id'] for c in candidates}
        fresh_titles = {c['title'] for c in candidates}
        call = spy.find('recommend') or spy.last('recommend', 'browse')
        exclusions = set((call or {}).get('arguments', {}).get('exclusions') or [])
        # exclusions 允许用 id 或片名（recommend 两种都认）
        check('已给候选被排除',
              bool(previous) and (previous_ids <= exclusions or previous_titles <= exclusions),
              f'exclusions={sorted(exclusions)}')
        overlap = (fresh_ids & previous_ids) or (fresh_titles & previous_titles)
        check('新候选不重复', bool(candidates) and not overlap, f'重叠={sorted(overlap)}')
    elif letter == 'E':
        call = spy.find('recommend') or spy.last('recommend')
        limit = ((call or {}).get('arguments') or {}).get('max_episodes')
        if isinstance(limit, int) and candidates:
            worst = max(((tools.item(c['id']) or {}).get('episodes') or 0)
                        for c in candidates)
            check('集数上限生效', worst <= limit, f'最大 {worst} 集 / 上限 {limit}')
        else:
            # 没套上限时，至少必须如实追问/澄清，不得编造候选
            check('未套上限时如实澄清（不编造候选）',
                  not candidates and bool(re.search(r'太长|集数|多少集|时长|短一点', reply)),
                  f'limit={limit} candidates={len(candidates)} {reply[:60]}')
        check('不暴露内部降级文案', '大模型未启用' not in reply)
    elif letter == 'F':
        check('如实说明无记录或真实记录',
              ('没有记录' in reply.replace('还没有记录', '没有记录')
               or bool(re.search(r'上次记录到的是《', reply))), reply[:80])
        check('未编造集数', not re.search(r'第\s*\d+\s*集', reply)
              or bool(re.search(r'上次记录到的是《', reply)))
    elif letter in ('G', 'G3'):
        selected = (re.search(r'已选择《(.+?)》', turns[1].get('reply') or '')
                    if len(turns) > 1 else None)
        seasons = []
        if selected and selected.group(1) in library:
            seasons = (tools.episodes(library[selected.group(1)]['id']) or {}).get('seasons') or []
        if letter == 'G':
            if 2 in seasons:
                check('有第 2 季时走确认流程',
                      last.get('status') == 'NEEDS_CONFIRMATION', last.get('status'))
            else:
                check('该作品没有第 2 季时如实拒绝且不执行',
                      last.get('status') != 'SUCCESS'
                      and bool(re.search(r'没有|不存在|找不到', reply)),
                      f'seasons={seasons} {reply[:60]}')
        else:
            check('不存在的季不执行', last.get('status') != 'SUCCESS', last.get('status'))
        check('未直接执行', agent.run.call_count == 0, str(agent.run.call_count))
    elif letter == 'H':
        check('确认意图未被误路由（NATIVE/其他）',
              last.get('status') != 'NATIVE', last.get('status'))
        check('只执行一次', agent.run.call_count == 1, str(agent.run.call_count))
        command = agent.run.call_args[0][0] if agent.run.call_args else ''
        check('执行的是播放命令', command.startswith('播放'), command)
    elif letter == 'H2':
        check('取消后不执行', agent.run.call_count == 0, str(agent.run.call_count))
        check('回复确认取消', '取消' in reply, reply[:60])
    elif letter == 'I':
        check('回复与设备观测一致', FakeDevice.OBSERVED['title'] in reply, reply[:80])
        check('只读提问', all(re.search(r'什么|当前', c[0][0])
                              for c in agent.run.call_args_list))
    elif letter == 'J':
        rows = tools.recent(limit=5)['items']
        days = [row['added_days_ago'] for row in rows]
        check('按 added_days_ago 递增', days == sorted(days), str(days))
        check('回复含最近作品', all(row['title'] in reply for row in rows[:3]))
    elif letter == 'K':
        check('说明没有电影', bool(re.search(r'没有.{0,8}电影|电影.{0,6}0\s*部', reply)),
              reply[:80])
        check('给出真实剧集数', str(summary['series']) in reply, str(summary['series']))
        check('不编造电影', all(t in library for t in re.findall(r'《(.+?)》', reply)))
    elif letter == 'L':
        labelled = summary.get('with_genres', 0)
        declares = bool(re.search(r'没有.{0,10}(类型标签|题材标签|题材|类型)', reply))
        no_such_genre = bool(re.search(r'没有[“"]?悬疑[”"]?', reply))
        check('如实说明没有该题材或没有类型标签', declares or no_such_genre, reply[:120])
        if labelled == 0:
            check('不谎称库里有带标签的作品',
                  not re.search(r'有(?:类型|题材)标签的\s*\d+\s*部', reply), reply[:80])
        check('不编造该题材作品', not re.search(r'《[^》]*悬疑[^》]*》', reply))
    elif letter == 'M':
        check('能力说明', '影视库' in reply, reply[:80])
    elif letter == 'N':
        first = turns[0].get('candidates') or []
        restored = turns[-2] if len(turns) >= 2 else {}
        if len(first) >= 2:
            check('「就看刚才那个」命中第 2 个候选',
                  first[1]['title'] in (restored.get('reply') or ''), '')
        check('确认后执行一次', agent.run.call_count == 1, str(agent.run.call_count))
    elif letter == 'O':
        check('重复确认只执行一次', agent.run.call_count == 1, str(agent.run.call_count))
        check('第二次确认被拒绝', '没有可确认的操作' in reply, reply[:60])
    return checks


# --------------------------------------------------------------------------- #
# 跑批
# --------------------------------------------------------------------------- #
def run_dataset(name, cfg, roots, reps, out_records, quiet=False):
    tools_for_facts = LibraryTools(cfg, index=MediaIndex([str(r) for r in roots]).scan())
    summary = tools_for_facts.summary()
    scrub = sanitizer(roots)
    started = time.perf_counter()
    records = []
    for letter, turns_text in SEQUENCES.items():
        samples = []
        rep_records = []
        for rep in range(reps):
            llm = LLMClient(cfg)
            device = FakeDevice() if letter == 'I' else None
            dialogue, spy, agent, overview_calls = make_dialogue(cfg, roots, llm, device)
            turns = []
            for text in turns_text:
                wall = time.perf_counter()
                result = dialogue.handle(text, tv_id='tv1', source='text')
                wall_ms = (time.perf_counter() - wall) * 1000
                metrics = result.get('metrics') or {}
                turns.append({
                    'utterance': scrub(text),
                    'status': result.get('status'),
                    'intent': (result.get('decision') or {}).get('intent'),
                    'route': (result.get('route') or (result.get('decision') or {}).get('route')),
                    'decision_source': (result.get('decision') or {}).get('source'),
                    'reply': scrub(result.get('reply') or ''),
                    'candidates': [{'id': c.get('id'), 'title': c.get('title'),
                                    'episodes': c.get('episodes')}
                                   for c in (result.get('candidates') or [])],
                    'answer_source': metrics.get('answer_source'),
                    'diagnostics': {key: metrics.get(key) for key in
                                    ('model_name', 'decision_source', 'tool_name',
                                     'tool_status', 'llm_latency_ms', 'tool_latency_ms',
                                     'total_latency_ms', 'fallback_reason', 'failed_stage',
                                     'route_ms', 'llm_ms', 'total_ms', 'observe_ms')},
                    'wall_ms': round(wall_ms, 2),
                })
            samples.append(turns[-1]['wall_ms'])
            checks = fact_checks(letter, turns, tools_for_facts, summary, agent, spy,
                                 overview_calls)
            trace = [{'tool': call['tool'], 'ok': call['ok'],
                      'arguments': scrub_obj(call['arguments'], scrub),
                      'latency_ms': round(call['latency_ms'], 2),
                      'error': call.get('error')}
                     for call in spy.calls]
            rep_records.append({
                'rep': rep + 1,
                'passed': all(item['ok'] for item in checks),
                'checks': checks,
                'utterance': turns[-1]['utterance'],
                'reply': turns[-1]['reply'],
                'status': turns[-1]['status'],
                'intent': turns[-1]['intent'],
                'answer_source': turns[-1]['answer_source'],
                'diagnostics': turns[-1]['diagnostics'],
                'tool_trace': trace,
                'candidates': turns[-1]['candidates'],
                'agent_run_called': agent.run.call_count,
                'agent_run_commands': [c[0][0] for c in agent.run.call_args_list],
                'turns': turns if len(turns) > 1 else None,
            })
        # 证据里保留"第一次失败"的那一轮（更有诊断价值），全通过则保留最后一轮。
        chosen = next((item for item in rep_records if not item['passed']), rep_records[-1])
        last_record = {key: value for key, value in chosen.items()
                       if key not in ('rep', 'passed', 'checks')}
        last_record.update({
            'id': letter,
            'title': TITLE_BY_ID.get(letter, letter),
            'fact_checks': chosen['checks'],
            'reps': {'total': len(rep_records),
                     'passed': sum(1 for item in rep_records if item['passed'])},
            'rep_failures': [{'rep': item['rep'],
                              'failed': [check['check'] for check in item['checks']
                                         if not check['ok']]}
                             for item in rep_records if not item['passed']],
            'detail_rep': chosen['rep'],
            'verified': letter != 'I',
            'verified_reason': ('' if letter != 'I' else
                                '无真实电视可观测，使用注入的固定设备观测'),
        })
        last_record['passed'] = last_record['reps']['passed'] == last_record['reps']['total']
        last_record['latency_samples_ms'] = [round(value, 2) for value in samples]
        last_record['latency_p50_ms'] = round(statistics.median(samples), 2)
        last_record['latency_p95_ms'] = round(
            statistics.quantiles(samples, n=20)[-1] if len(samples) > 1 else samples[0], 2)
        records.append(last_record)
        if not quiet:
            state = 'PASS' if last_record['passed'] else 'FAIL'
            print(f'  [{name}] {letter:<3} {state} '
                  f'reps={last_record["reps"]["passed"]}/{last_record["reps"]["total"]} '
                  f'p50={last_record["latency_p50_ms"]:.0f}ms '
                  f'p95={last_record["latency_p95_ms"]:.0f}ms '
                  f'status={last_record["status"]}', flush=True)
    out_records[name] = {'records': records, 'summary': summary,
                         'elapsed_s': round(time.perf_counter() - started, 1)}
    return records


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def main(argv=None):
    parser = argparse.ArgumentParser(description='V4.4 真实 LLM 对话验收跑批')
    parser.add_argument('--reps', type=int, default=3, help='每条话术重复次数（默认 3）')
    parser.add_argument('--dataset', choices=('both', 'fixture', 'production'),
                        default='both')
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)

    cfg = load_config()
    production_roots = [Path(root) for root in
                        ((cfg.get('media') or {}).get('roots') or [])]
    datasets = {}
    tmpdir = None
    try:
        if args.dataset in ('both', 'fixture'):
            tmpdir = tempfile.TemporaryDirectory()
            fixture_root = build_fixture(Path(tmpdir.name))
            fixture_cfg = cfg_for(fixture_root)
            fixture_cfg['llm'] = cfg.get('llm')      # 真实模型配置
            datasets['fixture'] = (fixture_cfg, [fixture_root])
        if args.dataset in ('both', 'production'):
            readable = [root for root in production_roots if root.is_dir()]
            if not readable:
                print(f'!! 生产库不可读，跳过 production：{production_roots}', file=sys.stderr)
            else:
                datasets['production'] = (cfg, readable)

        tracemalloc.start()
        records = {}
        started = time.perf_counter()
        for name, (dataset_cfg, roots) in datasets.items():
            if not args.quiet:
                print(f'== dataset {name}: {len(roots)} 个只读根，reps={args.reps}')
            run_dataset(name, dataset_cfg, roots, max(1, args.reps), records, args.quiet)
        elapsed = time.perf_counter() - started
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()

    all_samples = []
    for dataset in records.values():
        for record in dataset['records']:
            all_samples.extend(record['latency_samples_ms'])
    metrics = {
        'reps': args.reps,
        'elapsed_s': round(elapsed, 1),
        'latency_ms': {
            'p50': percentile(all_samples, 0.50),
            'p95': percentile(all_samples, 0.95),
            'max': round(max(all_samples), 2) if all_samples else None,
            'samples': len(all_samples),
            'per_utterance': {
                name: {record['id']: {'p50': record['latency_p50_ms'],
                                      'p95': record['latency_p95_ms'],
                                      'samples': record['latency_samples_ms']}
                       for record in dataset['records']}
                for name, dataset in records.items()},
        },
        'memory': {
            'tracemalloc_peak_mb': round(peak / 1024 / 1024, 2),
            'tracemalloc_current_mb': round(current / 1024 / 1024, 2),
            'ru_maxrss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        },
    }

    scrub_all = sanitizer([str(root) for root in production_roots]
                          + [str(Path(tmpdir.name)) if tmpdir else ''])
    payload = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'contract': 'docs/V44_CONTRACT.md',
        'model': {'provider': (cfg.get('llm') or {}).get('provider'),
                  'model': (cfg.get('llm') or {}).get('model'),
                  'base_url': (cfg.get('llm') or {}).get('base_url')},
        'note': ('真实 LLM + 真实对话链路；不写 API key、不写绝对路径、不写完整 UI 文本。'
                 '每轮 agent.run 次数即"是否误执行电视动作"的证据。'),
        'datasets': records,
        'metrics': metrics,
        'unverified': UNVERIFIED,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(scrub_obj(payload, scrub_all),
                                   ensure_ascii=False, indent=2), encoding='utf-8')
    if not args.quiet:
        print(f'== 写出 {out_path}')
        print(f'   延迟 P50={metrics["latency_ms"]["p50"]}ms '
              f'P95={metrics["latency_ms"]["p95"]}ms '
              f'峰值内存={metrics["memory"]["tracemalloc_peak_mb"]}MB '
              f'(rss {metrics["memory"]["ru_maxrss_mb"]}MB)')
        for name, dataset in records.items():
            failed = [r['id'] for r in dataset['records'] if not r['passed']]
            print(f'   {name}: {len(dataset["records"]) - len(failed)}/'
                  f'{len(dataset["records"])} PASS 失败项={failed}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
