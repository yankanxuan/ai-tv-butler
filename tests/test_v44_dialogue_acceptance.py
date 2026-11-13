"""V4.4 对话验收测试：15 条必备话术（A–O）+ 6 组对抗场景。

这些用例走的是**真实全栈链路**（与 ``tests/test_assistant_v43.py`` 的
``NaturalLanguageTests`` 同一骨架）::

    VoiceDialogue(cfg, agent=Mock, catalog=LibraryVoiceCatalog(...),
                  router=IntelligentVoiceRouter(cfg, llm=Mock(enabled=False)),
                  responder=Mock)
    dialogue.handle(text, tv_id='tv1', source='text')

即「用户说话 → 路由 → 工具调用 → 回复 → 确认 → 执行」的完整路径。所有断言只引用
**真实工具返回值 / 真实设备观测 / 真实会话状态**，不依赖网络（无 LLM、无 NAS、无电视），
可以在 CI 里跑。真实 LLM 跑批见 ``scripts/v44_acceptance_run.py``。

每条话术都检查七件事：
意图、实际调用的工具、工具真实返回值、事实准确性、候选一致性、
是否误执行电视动作（``agent.run`` 只能由用户明确确认触发）、最终反馈、延迟。

约定：标了 ``@unittest.expectedFailure`` 的用例带 ``KNOWN-V44-BUG-n`` 注释，指向已上报
给 lead 的源码缺陷；装饰器移除后该断言即为验收标准。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from adbtv.config import DEFAULTS
from adbtv.media_index import MediaIndex
from adbtv.voice_intelligence import IntelligentVoiceRouter
from adbtv.voice_runtime import LibraryVoiceCatalog, VoiceDialogue

# 确定性路径的宽松上限（CI 机器差异很大，这里只拦住"卡死/退化"）。
LATENCY_BUDGET_MS = 3000.0


# --------------------------------------------------------------------------- #
# fixture：一个与生产库形状一致的小库
#   0 部电影、0 个 genre 标签、3 部剧 + 2 部短剧、集数差异大、NFO 带 plot/studio
# --------------------------------------------------------------------------- #
FIXTURE_SPECS = (
    # (目录, 季号, 集数, 多少天前入库, tvshow.nfo 内容)
    (('剧集', '三体 (2023)'), 1, 6, 2,
     '<tvshow><plot>地球文明与三体文明的第一次接触，人类科学家发现物理学规律正在失效。</plot>'
     '<studio>腾讯视频</studio></tvshow>'),
    (('剧集', '三体 (2023)'), 2, 4, 2, None),
    (('剧集', '漫长的季节 (2023)'), 1, 12, 9,
     '<tvshow><plot>东北小城一桩碎尸案跨越十八年，三个老男人困在秋天里。</plot>'
     '<studio>企鹅影视</studio></tvshow>'),
    (('剧集', '大明王朝 (2007)'), 1, 46, 40,
     '<tvshow><plot>嘉靖年间朝廷与海瑞的博弈。</plot>'
     '<studio>湖南广播电视台</studio></tvshow>'),
    (('短剧', '师兄太稳健 (2026)'), 1, 200, 1,
     '<tvshow><plot>穿越者苟在宗门里稳健修行。</plot>'
     '<uniqueid type="hongguo">1234</uniqueid></tvshow>'),
    (('短剧', '我不是大师 (2026)'), 1, 30, 5,
     '<tvshow><plot>外卖员误入武林成为大师。</plot>'
     '<uniqueid type="hongguo">5678</uniqueid></tvshow>'),
)


def build_fixture(root: Path) -> Path:
    """生产库形状的 fixture：0 电影、无 genre、剧集 + 短剧、NFO 有 plot。"""
    now = time.time()
    for parts, season, count, days, nfo in FIXTURE_SPECS:
        base = root.joinpath(*parts)
        folder = base / f'Season {season:02d}'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = now - days * 86400
        stem = parts[-1].split(' (')[0]
        for episode in range(1, count + 1):
            video = folder / f'{stem} S{season:02d}E{episode:03d}.mkv'
            video.write_bytes(b'v')
            os.utime(video, (stamp, stamp))
        if nfo:
            nfo_path = base / 'tvshow.nfo'
            nfo_path.write_text(nfo, encoding='utf-8')
            os.utime(nfo_path, (stamp, stamp))
    (root / '电影').mkdir(parents=True, exist_ok=True)   # 存在但为空：0 部电影
    return root


def cfg_for(root, *, assistant=True):
    cfg = copy.deepcopy(DEFAULTS)
    cfg['media'] = {'enabled': True, 'roots': [str(root)]}
    cfg['voice']['intelligence'] = {'enabled': True, 'native_settle_seconds': 0}
    cfg['voice']['assistant'] = {'enabled': assistant}
    return cfg


# --------------------------------------------------------------------------- #
# 观测工具：记录真实工具调用、真实返回值、真实延迟
# --------------------------------------------------------------------------- #
class ToolSpy:
    """``LibraryTools`` 的透传代理，记录每一次真实调用与真实返回值。"""

    TRACKED = ('summary', 'overview', 'status', 'browse', 'search', 'recommend',
               'recent', 'item', 'episodes', 'health', 'find')

    def __init__(self, inner):
        object.__setattr__(self, 'inner', inner)
        object.__setattr__(self, 'calls', [])

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name not in self.TRACKED or not callable(attr):
            return attr

        def recorder(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:                       # noqa: BLE001 - 记录后原样抛出
                self.calls.append({'tool': name, 'arguments': dict(kwargs),
                                   'args': args, 'ok': False,
                                   'error': type(exc).__name__,
                                   'latency_ms': (time.perf_counter() - started) * 1000})
                raise
            self.calls.append({'tool': name, 'arguments': dict(kwargs), 'args': args,
                               'ok': True, 'result': result,
                               'latency_ms': (time.perf_counter() - started) * 1000})
            return result

        return recorder

    # -- 断言辅助 --
    def names(self):
        return [call['tool'] for call in self.calls]

    def last(self, *names):
        for call in reversed(self.calls):
            if call['tool'] in names:
                return call
        return None

    def result_of(self, *names):
        call = self.last(*names)
        return (call or {}).get('result')

    def find(self, tool, **match):
        """找一次满足条件的真实调用（参数按 kwargs 匹配）。"""
        for call in reversed(self.calls):
            if call['tool'] != tool:
                continue
            if all(call['arguments'].get(key) == value for key, value in match.items()):
                return call
        return None


class Clock:
    """可控时钟，用来验证确认请求的 TTL 过期。"""

    def __init__(self):
        self.now = time.monotonic()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptedLLM:
    """按脚本返回内容并记录调用次数的假模型（用于"模型编造"等对抗场景）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.enabled = True
        self.model = 'scripted-fake'
        self.calls = []

    def chat(self, messages, system=None, timeout=None):
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError('no scripted reply left')
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def decision_json(**overrides):
    payload = {'route': 'agent', 'intent': 'assistant', 'target': '',
               'parameters': {}, 'confidence': 0.9, 'reply': '', 'risk': 'none'}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class DialogueHarness(unittest.TestCase):
    """公共装置：fixture 库 + 全栈 VoiceDialogue + 真实调用记录。"""

    assistant = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = build_fixture(Path(self.tmp.name))
        self.cfg = cfg_for(self.root, assistant=self.assistant)
        self.agent = Mock()
        self.agent.run.return_value = {'ok': True, 'status': 'SUCCESS', 'summary': '已完成。'}
        self.responder = Mock()
        self.responder.respond.return_value = {'tts_ms': 0}
        self.dialogue = self.make_dialogue()

    # -- 装置 --
    def make_dialogue(self, *, llm=None, device=None, clock=None, roots=None):
        cfg = self.cfg if roots is None else cfg_for(roots, assistant=self.assistant)
        scan_roots = [str(roots)] if roots is not None else [str(self.root)]
        index = MediaIndex(scan_roots).scan()
        dialogue = VoiceDialogue(
            cfg, agent=self.agent, device=device,
            catalog=LibraryVoiceCatalog(cfg, index=index),
            router=IntelligentVoiceRouter(cfg, llm=llm if llm is not None else Mock(enabled=False)),
            responder=self.responder, clock=clock or time.monotonic)
        # 记录真实工具调用（LibraryTools 由 VoiceDialogue 自己构造，这里换成代理）。
        spy = ToolSpy(dialogue.tools)
        dialogue.tools = spy
        if dialogue.assistant is not None:
            dialogue.assistant.tools = spy
        dialogue.tool_spy = spy
        # 记录 catalog 适配器的 overview 调用（V4.3 路由走的是它）。
        self.catalog_overview_calls = []
        original = dialogue.catalog.overview

        def overview(*args, **kwargs):
            value = original(*args, **kwargs)
            self.catalog_overview_calls.append(value)
            return value

        dialogue.catalog.overview = overview
        return dialogue

    # -- 说话 --
    def say(self, text, tv='tv1', dialogue=None):
        dialogue = dialogue or self.dialogue
        started = time.perf_counter()
        result = dialogue.handle(text, tv_id=tv, source='text')
        result['_wall_ms'] = (time.perf_counter() - started) * 1000
        self.assertLess(result['_wall_ms'], LATENCY_BUDGET_MS,
                        f'{text} 单轮耗时 {result["_wall_ms"]:.0f}ms 超过预算')
        return result

    # -- 事实来源 --
    @staticmethod
    def _inner(dialogue):
        """直接访问真实 LibraryTools（不经代理，避免污染被记录的调用轨迹）。"""
        tools = dialogue.tools
        return getattr(tools, 'inner', tools)

    def summary(self, dialogue=None):
        return self._inner(dialogue or self.dialogue).summary()

    def library_titles(self, dialogue=None):
        rows = self._inner(dialogue or self.dialogue).browse(kind='any', limit=20)['items']
        return {row['title']: row for row in rows}

    def item(self, item_id, dialogue=None):
        return self._inner(dialogue or self.dialogue).item(item_id)

    @staticmethod
    def counts(reply):
        """从回复里抽出现实数值（用于与真实 summary 逐项比对）。"""
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

    def select_work(self, ordinal_cn, dialogue=None):
        """先用一次真实浏览建立候选，再按序号选择，返回被选中的候选。"""
        dialogue = dialogue or self.dialogue
        browse = self.say('现在资源库有什么？', dialogue=dialogue)
        self.assertTrue(browse['candidates'])
        self.say(f'第{ordinal_cn}部。', dialogue=dialogue)
        selected = dialogue.sessions.get('tv1').selected_content
        self.assertIsNotNone(selected, '选择后必须记录真实候选')
        return selected


# --------------------------------------------------------------------------- #
# A. 「现在资源库有什么？」
# --------------------------------------------------------------------------- #
class InventoryTests(DialogueHarness):
    utterance = '现在资源库有什么？'

    def test_a_overview_route_uses_real_catalog_and_never_touches_the_tv(self):
        result = self.say(self.utterance)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn(result['decision']['intent'], ('browse_library', 'assistant'))
        # 必须走 overview 类读取（catalog_overview / library_summary / browse 或适配器 overview）
        overview_tools = {'overview', 'summary', 'browse', 'status'}
        used_tool = bool(overview_tools & set(self.dialogue.tool_spy.names()))
        self.assertTrue(used_tool or self.catalog_overview_calls,
                        f'没有调用任何 overview 类读取：{self.dialogue.tool_spy.names()}')
        self.assertEqual(result['metrics'].get('answer_source'), 'local_catalog')
        # 不得误触电视动作
        self.agent.run.assert_not_called()
        # 候选与真实库一致（回复里出现的片名必须真实存在）
        real = self.library_titles()
        for candidate in result['candidates']:
            self.assertIn(candidate['title'], real)
        self.assertEqual(len(result['candidates']), len(real))

    def test_a_counts_match_summary_item_by_item(self):
        """回复里的每个数字都必须等于 summary() 的真实值（短剧单列，不并进剧集）。"""
        result = self.say(self.utterance)
        real = self.summary()
        counts = self.counts(result['reply'])
        self.assertIn('total', counts, result['reply'])
        self.assertEqual(counts['total'], real['total'])
        self.assertEqual(counts.get('movies', 0), real['movies'])
        self.assertEqual(counts.get('series'), real['series'])
        if real['short_dramas']:
            self.assertIn('短剧', result['reply'],
                          '库里有短剧时必须单列，不能并进剧集')
            self.assertEqual(counts.get('short_dramas'), real['short_dramas'])

    def test_j_recent_uses_recent_tool_and_is_ordered_by_added_days(self):
        result = self.say('最近新增了哪些资源？')
        self.assertEqual(result['status'], 'SUCCESS')
        call = self.dialogue.tool_spy.last('recent', 'browse')
        self.assertIsNotNone(call, '最近新增必须走 recent 类工具')
        self.agent.run.assert_not_called()
        rows = self._inner(self.dialogue).recent(limit=5)['items']
        days = [row['added_days_ago'] for row in rows]
        self.assertEqual(days, sorted(days), 'recent 必须按 added_days_ago 递增')
        self.assertEqual([c['title'] for c in result['candidates']],
                         [row['title'] for row in rows])
        for title in [row['title'] for row in rows]:
            self.assertIn(title, result['reply'])

    def test_k_zero_movies_is_reported_honestly_with_real_series_count(self):
        result = self.say('我想看电影。')
        self.assertEqual(result['status'], 'SUCCESS')
        real = self.summary()
        self.assertEqual(real['movies'], 0, 'fixture 前提：库里 0 部电影')
        self.assertIn('没有电影', result['reply'])
        self.assertIn(str(real['series']), result['reply'])
        self.assertIn(str(real['episodes']), result['reply'])
        self.agent.run.assert_not_called()
        # 不得编造任何电影
        real_titles = set(self.library_titles())
        for title in re.findall(r'《(.+?)》', result['reply']):
            self.assertIn(title, real_titles, f'回复里的《{title}》不在库里')


# --------------------------------------------------------------------------- #
# B / D / E / L：推荐类
# --------------------------------------------------------------------------- #
class RecommendationTests(DialogueHarness):

    def test_b_three_recommendations_are_real_items(self):
        result = self.say('推荐三部好看的。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(len(result['candidates']), 3, '「推荐三部」必须给 3 条')
        self.agent.run.assert_not_called()
        for candidate in result['candidates']:
            detail = self.item(candidate['id'])
            self.assertIsNotNone(detail, f'候选 id {candidate["id"]} 必须能在库里查到')
            self.assertEqual(detail['title'], candidate['title'])
            self.assertIn(detail['title'], result['reply'])
        self.assertEqual(len({c['id'] for c in result['candidates']}), 3, '候选不得重复')

    def test_d_change_one_excludes_every_previous_candidate(self):
        """换一个类似的只排除 last_item，而推荐结果从不写 last_item（曾有 KNOWN-V44-BUG 记录）。"""
        first = self.say('推荐三部好看的。')
        previous = {c['id'] for c in first['candidates']}
        self.assertEqual(len(previous), 3)
        second = self.say('换一个类似的。')
        self.assertEqual(second['status'], 'SUCCESS')
        self.agent.run.assert_not_called()
        call = self.dialogue.tool_spy.last('recommend')
        self.assertIsNotNone(call, '换一个必须走 recommend 工具')
        exclusions = set(call['arguments'].get('exclusions') or [])
        self.assertTrue(previous <= exclusions,
                        f'已给候选必须全部排除，实际 exclusions={exclusions}')
        fresh = {c['id'] for c in second['candidates']}
        self.assertTrue(fresh, '换一个必须有新候选')
        self.assertEqual(fresh & previous, set(), '新候选不得与上一次重复')

    def test_e_short_request_caps_episode_count(self):
        """「不要那么长的。」必须让集数上限真实生效（曾有 OPEN-V44-2）。"""
        self.say('推荐三部好看的。')
        result = self.say('不要那么长的。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertNotIn('大模型未启用', result['reply'])
        call = self.dialogue.tool_spy.last('recommend')
        self.assertIsNotNone(call, '「不要太长」必须走 recommend 并带 max_episodes')
        limit = call['arguments'].get('max_episodes')
        self.assertIsInstance(limit, int)
        self.assertGreater(limit, 0)
        self.assertTrue(result['candidates'], '必须给出符合上限的候选')
        for candidate in result['candidates']:
            detail = self.item(candidate['id'])
            self.assertLessEqual(detail['episodes'], limit,
                                 f'{detail["title"]} 有 {detail["episodes"]} 集，超过上限 {limit}')
        self.agent.run.assert_not_called()

    def test_l_genre_request_without_genre_metadata_is_declared_unsupported(self):
        """V4.4 契约 L：题材覆盖必须用 `with_genres` 计数（已修，装饰器已移除）。

        metadata_missing 的口径是「plot/genres/studio/year 任一存在」，与「有没有
        genre/tag」不同。fixture 5 部都有 plot+studio、0 部有 genre，回复却说
        「本地库里有类型标签的 5 部作品中没有"悬疑"」——编造事实。
        """
        rows = self._inner(self.dialogue).all_items()['items']
        self.assertFalse([row['title'] for row in rows if row['genres']],
                         'fixture 前提：没有任何类型标签')
        result = self.say('推荐一部悬疑剧。')
        reply = result['reply']
        self.assertTrue(
            re.search(r'没有.{0,8}(类型标签|题材标签|类型|题材)'
                      r'|无法按.{0,6}(类型|题材)|缺少.{0,6}(类型|题材)', reply),
            f'缺少类型标签时必须如实说明，实际回复：{reply}')
        # 不得谎称库里存在"有类型标签的作品"
        self.assertIsNone(re.search(r'有类型标签的\s*\d+\s*部', reply),
                          f'库里 0 部有 genre/tag，不得声称有带标签的作品：{reply}')
        self.agent.run.assert_not_called()


# --------------------------------------------------------------------------- #
# C / F：详情与进度
# --------------------------------------------------------------------------- #
class DetailAndRecallTests(DialogueHarness):

    def test_c_second_one_describes_the_second_real_candidate(self):
        first = self.say('推荐三部好看的。')
        second_candidate = first['candidates'][1]
        other = first['candidates'][0]
        result = self.say('第二个讲什么？')
        self.assertEqual(result['status'], 'SUCCESS')
        detail = self.item(second_candidate['id'])
        self.assertTrue(detail['plot'], 'fixture 前提：该作品有 NFO plot')
        self.assertIn(second_candidate['title'], result['reply'])
        self.assertIn(detail['plot'][:12], result['reply'], '必须复述真实 NFO plot 片段')
        self.assertNotIn(other['title'], result['reply'], '不得串到第 1 个候选')
        call = self.dialogue.tool_spy.find('item', item_id=second_candidate['id'])
        self.assertIsNotNone(call, '详情必须走 catalog_detail(item) 且命中该候选 id')
        self.agent.run.assert_not_called()

    def test_f_recall_without_record_is_honest(self):
        result = self.say('我上次看到哪里了？')
        self.assertIn(result['status'], ('SUCCESS', 'NEEDS_CLARIFICATION'))
        self.assertIn('没有记录', result['reply'].replace('还没有记录', '没有记录'))
        self.assertIsNone(re.search(r'第\s*\d+\s*集', result['reply']),
                          f'无记录时不得编造集数：{result["reply"]}')
        self.agent.run.assert_not_called()

    def test_f_recall_after_a_real_play_reports_the_observed_position(self):
        selected = self.select_work('二')
        self.say('播放第一集。')
        self.say('确认播放。')
        played = self.dialogue.sessions.get('tv1').played_history
        self.assertTrue(played, '确认播放后必须记录真实播放历史')
        result = self.say('我上次看到哪里了？')
        self.assertIn(played[0]['title'], result['reply'])
        self.assertIn('第1集', result['reply'])
        self.assertIn(selected['title'], result['reply'])


# --------------------------------------------------------------------------- #
# G / H / O：确认与幂等
# --------------------------------------------------------------------------- #
class PlaybackConfirmationTests(DialogueHarness):

    def test_g_play_requires_confirmation_and_never_executes_early(self):
        selected = self.select_work('一')
        result = self.say('播放第一集。')
        self.assertEqual(result['status'], 'NEEDS_CONFIRMATION')
        self.assertIn(selected['title'], result['reply'])
        self.agent.run.assert_not_called()
        pending = self.dialogue.sessions.get('tv1').pending_action
        self.assertTrue(pending, '必须留下真实待确认动作')
        self.assertIn(selected['title'], pending['command'])

    def test_g_nonexistent_season_is_never_executed_or_faked(self):
        self.select_work('一')                      # 三体只有第 1/2 季
        result = self.say('打开这部剧第三季。')
        self.assertNotEqual(result['status'], 'SUCCESS')
        self.agent.run.assert_not_called()
        self.assertNotIn('已完成', result['reply'])
        self.assertIsNone(re.search(r'正在播放|已经?播放|播放中', result['reply']),
                          f'不存在的季不得声称已播放：{result["reply"]}')

    def test_g_open_this_work_season_goes_through_confirmation(self):
        """「打开这部剧第二季。」也必须走确认，不得直接执行（曾有 KNOWN-V44-BUG-3）。"""
        self.select_work('一')                      # 三体，有第 1/2 季
        result = self.say('打开这部剧第二季。')
        self.assertEqual(result['status'], 'NEEDS_CONFIRMATION')
        self.agent.run.assert_not_called()
        pending = self.dialogue.sessions.get('tv1').pending_action
        self.assertIsNotNone(pending)
        self.assertIn('第2季', pending['command'])

    def test_h_confirm_executes_exactly_one_play(self):
        selected = self.select_work('一')
        self.say('播放第一集。')
        result = self.say('确认播放。')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(self.agent.run.call_count, 1, '确认后必须只执行一次')
        command = self.agent.run.call_args[0][0]
        self.assertIn(selected['title'], command)
        self.assertTrue(command.startswith('播放'))
        self.assertIsNone(self.dialogue.sessions.get('tv1').pending_action,
                          '执行后 pending 必须清空')

    def test_h_cancel_never_executes_and_clears_pending(self):
        self.select_work('一')
        self.say('播放第一集。')
        result = self.say('取消播放。')
        self.agent.run.assert_not_called()
        self.assertIn('取消', result['reply'])
        self.assertIsNone(self.dialogue.sessions.get('tv1').pending_action)

    def test_o_duplicate_confirmation_executes_once(self):
        self.select_work('一')
        self.say('播放第一集。')
        self.say('确认播放。')
        self.assertEqual(self.agent.run.call_count, 1)
        again = self.say('确认播放。')
        self.assertEqual(self.agent.run.call_count, 1, '重复确认必须幂等')
        self.assertNotEqual(again['status'], 'SUCCESS')
        self.assertIn('没有可确认的操作', again['reply'])

    def test_o_expired_confirmation_is_rejected(self):
        clock = Clock()
        dialogue = self.make_dialogue(clock=clock)
        self.say('现在资源库有什么？', dialogue=dialogue)
        self.say('第一部。', dialogue=dialogue)
        self.say('播放第一集。', dialogue=dialogue)
        self.assertIsNotNone(dialogue.sessions.get('tv1').pending_action)
        clock.advance(600)                          # 超过会话 TTL
        result = self.say('确认播放。', dialogue=dialogue)
        self.agent.run.assert_not_called()
        self.assertIn('没有可确认的操作', result['reply'])


# --------------------------------------------------------------------------- #
# I：设备观测
# --------------------------------------------------------------------------- #
class DeviceObservationTests(DialogueHarness):
    OBSERVED = {'state': 'PLAYING', 'playing': True, 'title': '漫长的季节',
                'season': 1, 'episode': 5, 'metadata': {'title': '漫长的季节'}}

    def make_device(self):
        device = Mock()
        device.playback_status.return_value = dict(self.OBSERVED)
        device.current_package.return_value = 'com.xiaomi.mitv.tvplayer'
        return device

    def test_i_what_is_on_tv_matches_the_device_observation(self):
        dialogue = self.make_dialogue(device=self.make_device())
        result = self.say('电视现在在放什么？', dialogue=dialogue)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertIn(self.OBSERVED['title'], result['reply'])
        self.assertIn(str(self.OBSERVED['episode']), result['reply'])
        self.assertEqual(result['metrics'].get('answer_source'), 'device_status')
        # 只允许只读查询命令，不得出现播放/暂停等状态变更
        for call in self.agent.run.call_args_list:
            command = call[0][0]
            self.assertTrue(re.search(r'什么|当前', command),
                            f'只读提问不得触发状态变更命令：{command}')
        playback = dialogue.sessions.get('tv1').current_playback or {}
        self.assertEqual(playback.get('title'), self.OBSERVED['title'])

    def test_i_device_failure_is_not_replaced_by_an_invented_title(self):
        device = Mock()
        device.playback_status.side_effect = RuntimeError('adb down')
        device.current_package.side_effect = RuntimeError('adb down')
        dialogue = self.make_dialogue(device=device)
        result = self.say('电视现在在放什么？', dialogue=dialogue)
        for title in re.findall(r'《(.+?)》', result['reply']):
            self.assertIn(title, self.library_titles(dialogue),
                          '观测失败时不得编造片名')


# --------------------------------------------------------------------------- #
# M：能力说明
# --------------------------------------------------------------------------- #
class CapabilityTests(DialogueHarness):

    def test_m_capability_answer_touches_nothing(self):
        result = self.say('你能帮我做什么？')
        self.agent.run.assert_not_called()
        self.assertIn('影视库', result['reply'])
        self.assertTrue(re.search(r'播放|推荐|查询', result['reply']))

    def test_m_capability_answer_does_not_leak_internal_diagnostics(self):
        """能力说明不得暴露内部诊断（曾有 OPEN-V44-4）。"""
        result = self.say('你能帮我做什么？')
        self.assertNotIn('大模型', result['reply'])
        self.assertNotIn('未启用', result['reply'])


# --------------------------------------------------------------------------- #
# N：多轮组合 B → C → D → E → 「就看刚才那个。」→「确认。」
# --------------------------------------------------------------------------- #
class MultiTurnTests(DialogueHarness):

    def test_n_chain_keeps_candidates_consistent(self):
        first = self.say('推荐三部好看的。')
        self.assertEqual(len(first['candidates']), 3)
        second = first['candidates'][1]
        described = self.say('第二个讲什么？')
        self.assertIn(second['title'], described['reply'])
        swapped = self.say('换一个类似的。')
        self.assertTrue(swapped['candidates'])
        self.assertIn(swapped['status'], ('SUCCESS', 'NEEDS_CLARIFICATION'))

    def test_n_look_at_that_one_restores_the_right_candidate(self):
        """「就看刚才那个。」→「确认。」必须走完整播放链路（曾有 OPEN-V44-5）。"""
        first = self.say('推荐三部好看的。')
        second = first['candidates'][1]
        self.say('第二个讲什么？')
        restored = self.say('就看刚才那个。')
        self.assertEqual(restored['status'], 'NEEDS_CONFIRMATION')
        pending = self.dialogue.sessions.get('tv1').pending_action
        self.assertIsNotNone(pending, '「就看刚才那个」必须建立真实待确认动作')
        self.assertEqual(pending.get('candidate_id'), second['id'])
        self.assertIn(second['title'], pending['command'])
        played = self.say('确认。')
        self.assertEqual(self.agent.run.call_count, 1)
        self.assertIn(second['title'], self.agent.run.call_args[0][0])
        self.assertEqual(played['status'], 'SUCCESS')


# --------------------------------------------------------------------------- #
# 对抗测试
# --------------------------------------------------------------------------- #
class AdversarialTests(DialogueHarness):

    # 1. 模型编造
    def test_1_invented_titles_and_counts_are_replaced_by_real_tool_render(self):
        llm = ScriptedLLM([
            decision_json(),
            json.dumps({'action': 'answer',
                        'text': '本地有 12 部电影，推荐《不存在的神剧》和《虚构大片》。'},
                       ensure_ascii=False),
        ])
        dialogue = self.make_dialogue(llm=llm)
        result = self.say('推荐三部好看的。', dialogue=dialogue)
        self.assertNotIn('不存在的神剧', result['reply'])
        self.assertNotIn('虚构大片', result['reply'])
        self.assertNotIn('12 部电影', result['reply'])
        self.assertEqual(result['metrics'].get('fallback_reason'), 'ungrounded_answer')
        self.assertEqual(result['metrics'].get('decision_source'), 'deterministic')
        real = self.library_titles(dialogue)
        self.assertTrue(result['candidates'])
        for candidate in result['candidates']:
            self.assertIn(candidate['title'], real)
        self.agent.run.assert_not_called()

    # 2. 工具报错
    def test_2_tool_failure_is_reported_as_failure_not_success(self):
        llm = ScriptedLLM([
            decision_json(),
            json.dumps({'action': 'tool', 'tool': 'catalog_summary', 'arguments': {}},
                       ensure_ascii=False),
        ])
        dialogue = self.make_dialogue(llm=llm)
        dialogue.tools.summary = Mock(side_effect=RuntimeError('index exploded'))
        result = self.say('统计一下本地有多少部作品。', dialogue=dialogue)
        self.assertEqual(result['metrics'].get('fallback_reason'), 'tool_error')
        self.assertEqual(result['metrics'].get('failed_stage'), 'tool')
        self.assertEqual(result['metrics'].get('answer_source'), 'unverified')
        self.assertTrue(re.search(r'没有取到|无法|失败|不可用|出错|没有得到', result['reply']),
                        f'工具失败必须明确说明，实际：{result["reply"]}')
        self.assertFalse(result['candidates'])
        self.agent.run.assert_not_called()

    def test_2b_recommend_failure_must_not_be_reported_as_an_empty_library(self):
        """工具报错不得说成「库里没有作品」（曾有 KNOWN-V44-BUG-7）。"""
        llm = ScriptedLLM([
            decision_json(),
            json.dumps({'action': 'tool', 'tool': 'catalog_recommend',
                        'arguments': {'limit': 3}}, ensure_ascii=False),
        ])
        dialogue = self.make_dialogue(llm=llm)
        dialogue.tools.recommend = Mock(side_effect=RuntimeError('boom'))
        real = self.summary(dialogue)
        self.assertGreater(real['total'], 0, 'fixture 前提：库里有作品')
        result = self.say('推荐三部好看的。', dialogue=dialogue)
        self.assertEqual(result['metrics'].get('fallback_reason'), 'tool_error')
        self.assertNotIn('没有可推荐的作品', result['reply'],
                         '工具报错不得说成库里没有作品')
        self.assertTrue(re.search(r'没有取到|无法|失败|不可用|出错|没有得到', result['reply']))

    # 3. 库不可用
    def test_3_unreadable_root_is_reported_as_unavailable_not_zero(self):
        missing = Path(self.tmp.name) / 'gone'
        dialogue = self.make_dialogue(roots=missing)
        result = self.say('现在资源库有什么？', dialogue=dialogue)
        self.assertIn(result['status'], ('UNAVAILABLE', 'NEEDS_CLARIFICATION'))
        self.assertTrue(re.search(r'无法访问|读不到|不可用', result['reply']),
                        f'必须明确说明读不到：{result["reply"]}')
        self.assertIsNone(re.search(r'共\s*0\s*部', result['reply']),
                          '不得把不可用报成 0 部')
        self.agent.run.assert_not_called()

    # 4. 文件被删除
    def test_4_deleted_file_is_rejected_at_confirm_time(self):
        dialogue = self.dialogue
        browse = self.say('现在资源库有什么？')
        target_title = browse['candidates'][0]['title']
        self.say('第一部。')
        self.say('播放第一集。')
        self.assertIsNotNone(dialogue.sessions.get('tv1').pending_action)
        victim = next(self.root.glob(f'**/{target_title} (*)'))
        self.assertTrue(victim.is_dir(), victim)
        shutil.rmtree(victim)
        # 强制重建索引（清掉 60s 缓存）
        dialogue.catalog.index = MediaIndex([str(self.root)]).scan()
        dialogue.catalog._loaded_at = 0
        dialogue.tool_spy.inner._items = None
        result = self.say('确认播放。')
        self.agent.run.assert_not_called()
        self.assertIn('重新校验未通过', result['reply'])
        self.assertIn('未执行', result['reply'])
        self.assertNotEqual(result['status'], 'SUCCESS')

    # 5. 重复确认 —— 见 PlaybackConfirmationTests.test_o_duplicate_confirmation_executes_once
    # 6. 多用户串话
    def test_6_two_tvs_never_share_candidates_or_pending(self):
        dialogue = self.dialogue
        tv1 = self.say('推荐三部好看的。', tv='tv1')
        tv2 = self.say('现在资源库有什么？', tv='tv2')
        self.assertEqual(len(tv1['candidates']), 3)
        self.assertEqual(len(tv2['candidates']), 5)
        self.assertNotEqual([c['id'] for c in tv1['candidates']],
                            [c['id'] for c in tv2['candidates']])
        self.assertEqual([c['title'] for c in dialogue.sessions.get('tv1').recent_results],
                         [c['title'] for c in tv1['candidates']])
        self.assertEqual([c['title'] for c in dialogue.sessions.get('tv2').recent_results],
                         [c['title'] for c in tv2['candidates']])
        # 序号解析必须只看本机候选：tv1 只有 3 条，第 5 条必须查不到
        out_of_range = self.say('第五个讲什么。', tv='tv1')
        self.assertNotIn('讲的是', out_of_range['reply'])
        in_range = self.say('第五个讲什么。', tv='tv2')
        self.assertIn('讲的是', in_range['reply'])
        # pending 互不污染
        self.say('第一部。', tv='tv1')
        self.say('播放第一集。', tv='tv1')
        self.assertIsNotNone(dialogue.sessions.get('tv1').pending_action)
        cross = self.say('确认播放。', tv='tv2')
        self.agent.run.assert_not_called()
        self.assertIn('没有可确认的操作', cross['reply'])
        self.assertIsNotNone(dialogue.sessions.get('tv1').pending_action,
                             'tv2 的确认不得清掉 tv1 的待确认动作')
        # 偏好互不污染
        dialogue.sessions.update_preferences('tv1', {'max_episodes': 24})
        self.assertEqual(dialogue.sessions.preferences('tv1')['max_episodes'], 24)
        self.assertIsNone(dialogue.sessions.preferences('tv2')['max_episodes'])


# --------------------------------------------------------------------------- #
# 台账：把每条话术的真实观测落成一份机器可读清单（供 evidence 汇总用）
# --------------------------------------------------------------------------- #
class LedgerTests(DialogueHarness):
    """跑一遍 A–O，断言每轮都有可核验的事实来源与延迟。"""

    def test_every_utterance_reports_provenance_and_latency(self):
        script = ['现在资源库有什么？', '推荐三部好看的。', '第二个讲什么？',
                  '换一个类似的。', '我上次看到哪里了？', '电视现在在放什么？',
                  '最近新增了哪些资源？', '我想看电影。', '你能帮我做什么？']
        for text in script:
            with self.subTest(text=text):
                result = self.say(text)
                metrics = result['metrics']
                self.assertIn('total_ms', metrics)
                self.assertLess(metrics['total_ms'], LATENCY_BUDGET_MS)
                self.assertTrue(result['reply'], '每轮都必须有回复')
                self.assertIn(result['status'],
                              ('SUCCESS', 'NEEDS_CLARIFICATION', 'NEEDS_CONFIRMATION',
                               'UNAVAILABLE', 'UNSUPPORTED', 'NATIVE', 'IGNORED'))


if __name__ == '__main__':
    unittest.main()
