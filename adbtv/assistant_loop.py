"""Natural-language assistant loop over the read-only library tools.

The loop is deliberately small and framework-free (standard library only):

    utterance -> bounded context -> model action -> validated tool -> observation
              -> (more tools) -> grounded answer | proposal | clarify

Safety properties:

* The model may only name tools from a fixed allowlist; arguments are validated
  against an explicit JSON-Schema subset before execution.
* No ADB, shell, SQL, URL or filesystem path can be produced: tool results are
  projected fields, and playback is only ever a *proposal* handed to the existing
  confirmation chain.
* A model answer is accepted only when every 《title》 and every count it states
  appears in an actual tool observation. Otherwise a deterministic renderer that
  reads only observations composes the reply.
* When the model is unavailable, times out or emits invalid JSON, a deterministic
  planner still answers library questions from real data.
"""
from __future__ import annotations

import json
import re
import time

MAX_STEPS = 4
DEFAULT_BUDGET_SECONDS = 14.0
_TITLE = re.compile(r'《([^》]{1,60})》')
_COUNT = re.compile(r'(\d+)\s*(部|集|个)')
_FORBIDDEN = re.compile(
    r'adb\s|shell|;\s*|\|\||`|\$\(|https?://|file:|/media/|/vol[0-9]|\.sqlite|select\s+\*|'
    r'insert\s+into|drop\s+table', re.I)

# ---------------------------------------------------------------------------
# Structured fact grounding (V4.3.1)
#
# The model never *is* the source of a fact: tool observations are. Everything
# below turns each observation into typed facts and then checks what the model
# text claims against that ledger. A claim is bound to a concrete field
# (movies / series / total / episodes / an observed episode or season number /
# a real title), so "2 部电影" can no longer be satisfied by a total of 2 or by
# 4 episodes that merely happen to contain the digit 2.
# ---------------------------------------------------------------------------
_CN_DIGIT = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNIT = {'十': 10, '百': 100, '千': 1000}
_NUM = r'[0-9]+|[零〇一二三四五六七八九十百千万两]+'
_NUM_TOKEN = re.compile(_NUM)
_TITLE_QUOTED = re.compile(r'「([^」]{1,40})」|『([^』]{1,40})』')

# Count words -> the exact ledger field they must match.
_KIND_WORDS = ('影视作品', '电视剧', '连续剧', '剧集', '短剧', '电影', '影片', '片子', '剧', '作品')
_KIND_FIELD = {
    '电影': 'movies', '影片': 'movies', '片子': 'movies',
    '剧集': 'series', '电视剧': 'series', '连续剧': 'series',
    '短剧': 'series', '剧': 'series',
    '作品': 'total', '影视作品': 'total',
}
_KIND_ALT = '|'.join(_KIND_WORDS)
_QUANT = r'部|个|条'
# "2 部电影" / "电影 2 部" / "电影有 100 个" / "共 2 部作品"; (?!第) keeps ordinals.
_COUNT_BEFORE = re.compile(
    r'(?<![第哪])(?P<num>' + _NUM + r')\s*(?:' + _QUANT + r')\s*(?P<word>' + _KIND_ALT + r')')
_COUNT_AFTER = re.compile(
    r'(?P<word>' + _KIND_ALT + r')\s*(?:数量)?\s*(?:有|共有|共|为|是)?\s*'
    r'(?P<num>' + _NUM + r')\s*(?:' + _QUANT + r')')
# Unit counts that are ambiguous with ordinals, so "第" is excluded.
_EPISODE_COUNT = re.compile(r'(?<![第哪这那])(?P<num>' + _NUM + r')\s*集')
_SEASON_COUNT = re.compile(r'(?<![第哪这那])(?P<num>' + _NUM + r')\s*季')
# Bare "N 部/个/条" (no kind word); cannot be satisfied by a page of list items.
_BARE_WORK = re.compile(
    r'(?<![第哪])(?P<num>' + _NUM + r')\s*(?:' + _QUANT + r')'
    r'(?!\s*(?:' + _KIND_ALT + r'|分|钟))')
_WORK_ORDINAL = re.compile(r'第(?P<num>' + _NUM + r')\s*部')
_ITEM_ORDINAL = re.compile(r'第(?P<num>' + _NUM + r')\s*(?:个|项)')
_EPISODE_ORDINAL = re.compile(r'第(?P<num>' + _NUM + r')\s*集')
_SEASON_ORDINAL = re.compile(r'第(?P<num>' + _NUM + r')\s*季')

# Verbs that introduce a work title in free text. Used only as a *candidate*
# extractor; the candidate must still be found in the tool ledger.
_TITLE_VERB = re.compile(
    r'(?:推荐|播放|名为|名叫|叫做|叫|片名是)'
    r'\s*(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,40})')
_CONNECTORS = ('以及', '还有', '和', '与', '及', '、', '，', ',', '/', '或', '再', '也')
# Everyday filler that can follow a verb without naming a work. Removed before
# deciding whether an unbracketed candidate still contains a real (fabricated)
# title. Conservative on purpose: a false positive only triggers the safe,
# deterministic renderer.
_FILLER = ('很好看', '挺好看', '值得一看', '值得看', '很精彩', '好看', '不错', '精彩',
           '推荐', '什么', '一下', '几部', '这部', '那部', '这个', '那个', '它们',
           '电影', '电视剧', '剧集', '作品', '片子', '内容', '本地', '片库',
           '影视库', '媒体库', '资源库', '资源', '里', '的', '了', '吧', '呢',
           '啊', '哦', '吗', '都', '也', '很', '挺', '个', '部', '些', '点',
           '你', '我', '您', '他', '她', '可以', '能够', '能', '会', '要', '想',
           '帮', '给', '来', '挑', '选', '看', '观看', '播放', '搜', '搜索', '找',
           '换', '再', '还', '有', '是', '和', '与', '及', '或', '一', '二', '三',
           '四', '五', '六', '七', '八', '九', '十', '几', '两', '些',
           '到', '哪', '哪里', '哪儿', '第', '记录', '历史', '状态', '观测', '设备',
           '正在', '最新', '更新', '显示', '以下', '这些', '那些', '信息', '元数据',
           '类型', '类型标签', '片名', '数量', '年份', '详情', '合计', '总计', '其中', '全部', '无法', '确认',
           '更早', '继续', '选择', '需要', '筛选', '完整', '列表', '收录', '目前',
           '加入', '最近', '当前', '最近一次', '还有', '以及',
           '结果', '清单', '条目', '索引', '地区', '时长', '大约', '约', '天',
           '剧名', '它', '结构', '季集', '集数',
           '悬疑', '科幻', '喜剧', '爱情', '动作', '犯罪', '历史剧', '战争', '恐怖',
           '家庭', '动画', '纪录片', '奇幻', '武侠', '都市', '古装')
_TIME_CLOCK = re.compile(r'\d{1,2}\s*[:：]\s*\d{2}')
# Only treat a Chinese clock reading as a time claim when it names minutes
# ("三点半" / "20点30分"), so ordinary phrases like "一点建议" are ignored.
_TIME_CN = re.compile(
    r'(?<!第)(?:\d{1,2}|[零〇一二三四五六七八九十两]{1,3})\s*点'
    r'(?:半|(?:\s*(?:\d{1,2}|[零〇一二三四五六七八九十]{1,3})\s*分))')

# ---------------------------------------------------------------------------
# Attribute claims (V4.3.2). Counts alone never prove a year, a media type, a
# genre, a runtime, a recency/reason or a live playback state; those must be
# present in the tool observation. When metadata is missing, the model may say
# so (negated forms below) but must not fill it in.
# ---------------------------------------------------------------------------
_YEAR_CLAIM = re.compile(r'(?<!\d)(\d{4})\s*年')
_KIND_CLAIM = re.compile(
    r'《(?P<title>[^》]{1,60})》[^。；，\n]{0,10}?'
    r'(?P<kind>电影|影片|片子|电视剧|剧集|连续剧|短剧)')
_KIND_EXPECT = {'电影': 'movie', '影片': 'movie', '片子': 'movie',
                '电视剧': 'series', '剧集': 'series', '连续剧': 'series',
                '短剧': 'series'}
_GENRE_WORDS = ('悬疑', '科幻', '喜剧', '爱情', '动作', '犯罪', '历史', '战争',
                '恐怖', '家庭', '动画', '纪录片', '奇幻', '武侠', '都市', '古装')
_GENRE_ALT = '|'.join(_GENRE_WORDS)
# Only treat a genre as *claimed* when it is explicitly framed as a genre ("题材是
# 科幻" / "科幻片" / "悬疑剧"), never when the word merely appears in prose.
_GENRE_CLAIM = re.compile(
    r'(?:题材|类型|风格)\s*(?:是|为|：|:)?\s*(?P<g1>' + _GENRE_ALT + r')|'
    r'(?P<g2>' + _GENRE_ALT + r')\s*(?:题材|类型|片|剧)')
_GENRE_NEGATION = re.compile(r'(?:没有|无|缺少|缺|未|不|非|无法|不能|未知|不详|暂无)')
_RUNTIME_CLAIM = re.compile(r'(?<!\d)(\d{1,3})\s*(分钟|小时|min|mins|hour|hours)', re.I)
_PLAYING_CLAIM = re.compile(r'正在播放|正在观看|正在看|播放中')
_PERCENT_CLAIM = re.compile(r'\d{1,3}\s*%')
# Attributes the tools never return: a model must not invent them.
_UNSUPPORTED_ATTR = re.compile(r'评分|豆瓣|票房|IMDb|imdb|获奖|口碑|热度|导演|主演|演员|编剧')


def cn_number(token):
    """Parse an Arabic or Chinese numeral token to ``int`` (``None`` if invalid)."""
    if not token:
        return None
    token = token.strip()
    if token.isdigit():
        return int(token)
    total = section = number = 0
    seen = False
    for char in token:
        if char in _CN_DIGIT:
            number = _CN_DIGIT[char]
            seen = True
        elif char in _CN_UNIT:
            unit = _CN_UNIT[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
            seen = True
        elif char == '万':
            section = (section + number) * 10000
            total += section
            section = number = 0
            seen = True
        else:
            return None
    return (total + section + number) if seen else None


def _strip_words(text, words):
    for word in sorted(words, key=len, reverse=True):
        text = text.replace(word, '')
    return text


def _residual_cjk(text):
    """Return a leftover CJK run (>=2 chars) not accounted for by known titles."""
    return bool(re.search(r'[\u4e00-\u9fff]{2,}', text))


class ToolError(ValueError):
    """Invalid tool name or arguments; never executed."""


TOOL_SCHEMAS = {
    'library_summary': {
        'description': '统计本地影视库：总作品数、电影数、剧集数、集数、元数据覆盖情况。',
        'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {}},
    },
    'library_browse': {
        'description': '按类型浏览本地影视库条目，可选排序。',
        'parameters': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'kind': {'type': 'string', 'enum': ['any', 'movie', 'series', 'unknown']},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'sort': {'type': 'string', 'enum': ['title', 'year', 'episodes', 'recent']},
            },
        },
    },
    'library_search': {
        'description': '按片名、类型、年份搜索本地影视库。缺元数据时会如实报告。',
        'parameters': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'query': {'type': 'string', 'maxLength': 60},
                'genre': {'type': 'string', 'maxLength': 20},
                'year': {'type': 'integer', 'minimum': 1900, 'maximum': 2100},
                'kind': {'type': 'string', 'enum': ['any', 'movie', 'series', 'unknown']},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
            },
        },
    },
    'library_recommend': {
        'description': '基于真实入库内容给出推荐候选，可带偏好与排除项。',
        'parameters': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'preferences': {'type': 'array', 'maxItems': 6,
                                'items': {'type': 'string', 'maxLength': 20}},
                'exclusions': {'type': 'array', 'maxItems': 20,
                               'items': {'type': 'string', 'maxLength': 60}},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'kind': {'type': 'string', 'enum': ['any', 'movie', 'series', 'unknown']},
                'max_episodes': {'type': 'integer', 'minimum': 1, 'maximum': 10000},
            },
        },
    },
    'library_recent': {
        'description': '列出最近加入/更新的本地影视作品。',
        'parameters': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'kind': {'type': 'string', 'enum': ['any', 'movie', 'series', 'unknown']},
            },
        },
    },
    'library_health': {
        'description': '检查媒体库是否可用：挂载、权限、索引时间、元数据覆盖。',
        'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {}},
    },
}

# Unified catalog naming (V4.3). ``library_*`` remain accepted aliases so older
# callers/tests keep working; every alias maps to exactly one implementation.
TOOL_ALIASES = {
    'library_summary': 'catalog_summary',
    'library_browse': 'catalog_list',
    'library_search': 'catalog_search',
    'library_recommend': 'catalog_recommend',
    'library_recent': 'catalog_list',
    'library_health': 'catalog_status',
}
_EPISODE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['item_id'],
    'properties': {
        'item_id': {'type': 'string', 'maxLength': 32},
        'season': {'type': 'integer', 'minimum': 1, 'maximum': 999},
    },
}
_PROGRESS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {}}
# Canonical entries reuse the reviewed schemas of the legacy names.
_CANONICAL_FROM = {'catalog_summary': 'library_summary',
                   'catalog_status': 'library_health',
                   'catalog_list': 'library_browse',
                   'catalog_search': 'library_search',
                   'catalog_recommend': 'library_recommend'}
for _canonical, _legacy in _CANONICAL_FROM.items():
    TOOL_SCHEMAS[_canonical] = TOOL_SCHEMAS[_legacy]
TOOL_SCHEMAS['catalog_get_episodes'] = {
    'description': '查询某部已入库作品的真实季/集结构。',
    'parameters': _EPISODE_SCHEMA,
}
TOOL_SCHEMAS['catalog_get_playback_progress'] = {
    'description': '查询当前/最近一次真实播放进度（来自设备观测与会话记录）。',
    'parameters': _PROGRESS_SCHEMA,
}

# Explicit, documented result schema for the playback-progress tool (P1). The
# three sources are deliberately distinct: a live device observation, a recent
# confirmed task record, and "nothing observed". A record that only knows the
# episode must not be presented as a precise playback time.
PLAYBACK_PROGRESS_RESULT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['source', 'available'],
    'properties': {
        'source': {'type': 'string', 'enum': ['device_status', 'recent_task', 'none']},
        'available': {'type': 'boolean'},
        'evidence': {'type': 'string',
                     'enum': ['device_observation', 'recent_play_record']},
        'title': {'type': ['string', 'null']},
        'season': {'type': ['integer', 'null']},
        'episode': {'type': ['integer', 'null']},
        'state': {'type': 'string'},
        'playing': {'type': 'boolean'},
        'updated_at': {'type': ['string', 'null']},
        'reason': {'type': 'string'},
    },
}
TOOL_SCHEMAS['catalog_get_playback_progress']['returns'] = PLAYBACK_PROGRESS_RESULT_SCHEMA

ACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['action'],
    'properties': {
        'action': {'type': 'string',
                   'enum': ['tool', 'answer', 'clarify', 'propose_play']},
        'tool': {'type': 'string', 'enum': sorted(TOOL_SCHEMAS)},
        'arguments': {'type': 'object'},
        'text': {'type': 'string', 'maxLength': 600},
        'title': {'type': 'string', 'maxLength': 60},
        'season': {'type': 'integer', 'minimum': 1, 'maximum': 999},
        'episode': {'type': 'integer', 'minimum': 1, 'maximum': 9999},
        'reason': {'type': 'string', 'maxLength': 120},
    },
}

SYSTEM_PROMPT_TEMPLATE = '''你是电视管家里的“影视库助理”，只通过给定工具查询本地 NAS 影视库，然后用简体中文自然回答。

规则（必须遵守）：
1. 只能输出一个 JSON 对象，不要 Markdown、不要解释。形如：
   {"action":"tool","tool":"library_summary","arguments":{}}
   {"action":"answer","text":"……"}
   {"action":"clarify","text":"……"}
   {"action":"propose_play","title":"真实片名","season":1,"episode":1}
2. 工具只能从下列清单选择，参数必须符合其 schema，不得编造参数：
{tools}
3. 需要事实（数量、片名、年份、集数、元数据）时必须先调用工具；没有工具结果就不要说出具体片名或数字。
4. 只能引用工具返回过的片名；不得虚构未入库的作品、类型或时长。
5. 数量必须与工具返回的字段一一对应：电影数用 movies，剧集数用 series，作品总数用 total，总集数用 episodes；不得用总数冒充电影数、用集数冒充作品数。
6. 元数据缺失（例如没有类型标签）就如实说明，不要猜类型。
7. 用户想播放时用 propose_play，并且必须使用工具返回过的真实片名；系统会再向用户确认。
8. 只有确实缺少关键条件时才 clarify，且一次只问一个问题。
9. 与影视库无关的闲聊可直接 answer，简短自然，不假装查询过电视或网络。
10. 禁止输出 ADB、shell、SQL、URL 或任何文件路径。
'''

# Concatenated, not .format(): the example JSON above contains braces.
SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.replace('{tools}', '{tool_list}')

CAPABILITY_REPLY = ('我可以查询本地影视库（总数、按类型浏览、按片名/年份搜索、推荐、最近添加），'
                    '也可以打开应用、暂停/继续、查当前播放；播放前会先请你确认。')


def extract_json_object(text):
    """Return the last balanced JSON object in *text* (models often reason first)."""
    if not isinstance(text, str) or not text.strip():
        return None
    stripped = text.strip()
    if stripped.startswith('{'):
        try:
            value = json.loads(stripped)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    found = None
    depth = 0
    start = None
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            if depth == 0:
                start = index
            depth += 1
        elif char == '}' and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:index + 1]
                try:
                    value = json.loads(candidate)
                except ValueError:
                    value = None
                if isinstance(value, dict):
                    found = value
                start = None
    return found


def _check_type(value, schema, name):
    expected = schema.get('type')
    if expected == 'string':
        if not isinstance(value, str):
            raise ToolError(f'{name} must be a string')
        limit = schema.get('maxLength')
        if limit and len(value) > limit:
            raise ToolError(f'{name} is too long')
        if _FORBIDDEN.search(value):
            raise ToolError(f'{name} contains forbidden content')
        if 'enum' in schema and value not in schema['enum']:
            raise ToolError(f'{name} must be one of {schema["enum"]}')
        return value
    if expected == 'integer':
        if type(value) is not int:
            raise ToolError(f'{name} must be an integer')
        low, high = schema.get('minimum'), schema.get('maximum')
        if low is not None and value < low:
            raise ToolError(f'{name} is below minimum')
        if high is not None and value > high:
            raise ToolError(f'{name} is above maximum')
        return value
    if expected == 'array':
        if not isinstance(value, list):
            raise ToolError(f'{name} must be a list')
        limit = schema.get('maxItems')
        if limit and len(value) > limit:
            raise ToolError(f'{name} has too many items')
        items = schema.get('items') or {}
        return [_check_type(item, items, f'{name}[]') for item in value]
    if expected == 'object':
        if not isinstance(value, dict):
            raise ToolError(f'{name} must be an object')
        return value
    raise ToolError(f'{name} has an unsupported schema')


def validate_arguments(tool, arguments):
    """Validate and normalize tool arguments against the declared schema."""
    if tool not in TOOL_SCHEMAS:
        raise ToolError('unknown tool')
    schema = TOOL_SCHEMAS[tool]['parameters']
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError('arguments must be an object')
    extra = set(arguments) - set(schema.get('properties') or {})
    if extra:
        raise ToolError('unknown argument: ' + ','.join(sorted(extra)))
    cleaned = {}
    for key, value in arguments.items():
        cleaned[key] = _check_type(value, schema['properties'][key], key)
    return cleaned


def validate_action(action):
    """Validate the model action envelope."""
    if not isinstance(action, dict):
        raise ToolError('action must be an object')
    extra = set(action) - set(ACTION_SCHEMA['properties'])
    if extra:
        raise ToolError('unknown action field: ' + ','.join(sorted(extra)))
    kind = action.get('action')
    if kind not in ACTION_SCHEMA['properties']['action']['enum']:
        raise ToolError('unsupported action')
    if kind == 'tool':
        if action.get('tool') not in TOOL_SCHEMAS:
            raise ToolError('unknown tool')
        return {'action': 'tool', 'tool': action['tool'],
                'arguments': validate_arguments(action['tool'], action.get('arguments'))}
    if kind in ('answer', 'clarify'):
        text = action.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 600:
            raise ToolError('text is required and bounded')
        if _FORBIDDEN.search(text):
            raise ToolError('text contains forbidden content')
        return {'action': kind, 'text': text.strip()}
    title = action.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 60:
        raise ToolError('title is required')
    if _FORBIDDEN.search(title):
        raise ToolError('title contains forbidden content')
    proposal = {'action': 'propose_play', 'title': title.strip()}
    for key in ('season', 'episode'):
        if action.get(key) is not None:
            proposal[key] = _check_type(action[key],
                                        ACTION_SCHEMA['properties'][key], key)
    return proposal


class AssistantLoop:
    """Tool-calling loop; every fact in a reply comes from a tool observation."""

    def __init__(self, cfg, tools, llm=None, *, max_steps=MAX_STEPS,
                 budget_seconds=DEFAULT_BUDGET_SECONDS, clock=time.monotonic):
        self.cfg = cfg
        self.tools = tools
        self.llm = llm
        self.max_steps = max(1, min(6, int(max_steps)))
        self.budget = max(1.0, min(30.0, float(budget_seconds)))
        self.clock = clock

    # ---------- execution ----------
    def _run_tool(self, name, arguments, state=None):
        started = self.clock()
        canonical = TOOL_ALIASES.get(name, name)
        arguments = dict(arguments or {})
        if canonical == 'catalog_summary':
            data = self.tools.summary()
        elif canonical == 'catalog_status':
            data = self.tools.status()
        elif canonical == 'catalog_list':
            if name == 'library_recent' and 'sort' not in arguments:
                arguments['sort'] = 'recent'
            data = self.tools.browse(**arguments)
        elif canonical == 'catalog_search':
            data = self.tools.search(**arguments)
        elif canonical == 'catalog_recommend':
            data = self.tools.recommend(**arguments)
        elif canonical == 'catalog_get_episodes':
            data = self.tools.episodes(**arguments)
        elif canonical == 'catalog_get_playback_progress':
            data = self._progress_from_state(state or {})
        else:
            raise ToolError('unknown tool')
        elapsed = (self.clock() - started) * 1000
        return data, elapsed

    @staticmethod
    def _progress_from_state(state):
        """Playback progress from real evidence only, never invented.

        Result schema (mirrored by PLAYBACK_PROGRESS_RESULT_SCHEMA):
          source    : 'device_status' (live observation) | 'recent_task'
                      (recent confirmed play record) | 'none'
          available : bool
          title     : str (when available)
          season    : int | null
          episode   : int | null
          state     : str (device state, '' when only a record exists)
          playing   : bool
          updated_at: str | null (only when a real timestamp was observed)
          evidence  : 'device_observation' | 'recent_play_record'
        """
        tv = state.get('tv') or {}
        history = state.get('played_history') or []
        last = history[0] if history else {}
        if tv.get('title'):
            return {'source': 'device_status', 'available': True,
                    'evidence': 'device_observation',
                    'title': tv.get('title'),
                    'season': tv.get('season'),
                    'episode': tv.get('episode'),
                    'state': tv.get('state') or '',
                    'playing': bool(tv.get('playing')),
                    'updated_at': tv.get('updated_at')}
        if last.get('title'):
            return {'source': 'recent_task', 'available': True,
                    'evidence': 'recent_play_record',
                    'title': last.get('title'),
                    'season': last.get('season'),
                    'episode': last.get('episode'),
                    'state': '',
                    'playing': False,
                    'updated_at': last.get('updated_at')}
        return {'source': 'none', 'available': False,
                'reason': 'no_playback_record', 'playing': bool(tv.get('playing'))}

    @staticmethod
    def _items_of(observation):
        payload = observation.get('result') or {}
        return payload.get('items') or []

    @staticmethod
    def _ledger_add(ledger, field, value):
        if type(value) is int and value >= 0:
            ledger[field].add(value)

    def _ledger_absorb_item(self, ledger, item):
        """Record one observed work plus its verifiable attributes."""
        title = item.get('title')
        kind = item.get('kind')
        if title:
            ledger['titles'].add(title)
            if kind in ('movie', 'series', 'unknown'):
                ledger['title_kinds'][title] = kind
        year = item.get('year')
        if type(year) is int:
            ledger['years'].add(year)
        genres = item.get('genres') or []
        if isinstance(genres, (list, tuple)):
            for genre in genres:
                if isinstance(genre, str) and genre.strip():
                    ledger['genres'].add(genre.strip())
                    if title:
                        ledger['genres_by_title'].setdefault(title, set()).add(genre.strip())
        runtime = item.get('runtime_minutes')
        if type(runtime) is int:
            ledger['runtimes'].add(runtime)
        elif isinstance(runtime, str) and runtime.isdigit():
            ledger['runtimes'].add(int(runtime))
        self._ledger_add(ledger, 'item_episodes', item.get('episodes'))
        item_seasons = item.get('seasons') or []
        if item_seasons:
            self._ledger_add(ledger, 'seasons', len(item_seasons))
            for number in item_seasons:
                if type(number) is int:
                    ledger['season_numbers'].add(number)

    def _build_ledger(self, observations):
        """Collapse every successful observation into typed, field-bound facts.

        Counts live under the field they were observed as: ``movies``, ``series``,
        ``total``, ``episodes``, ``seasons``, an episode/season number or a title.
        Attributes (year, kind, genre, runtime, playback) live beside the title
        that carried them. Per-list kind composition is kept so a claim such as
        "推荐了三部电影" can be checked against what those three rows actually are.
        """
        ledger = {
            'titles': set(), 'movies': set(), 'series': set(), 'unknown': set(),
            'total': set(), 'episodes': set(), 'item_episodes': set(),
            'seasons': set(), 'matched': set(),
            'episode_numbers': set(), 'season_numbers': set(),
            'list_len': 0, 'lists': [], 'title_kinds': {},
            'years': set(), 'genres': set(), 'genres_by_title': {},
            'runtimes': set(), 'recommend_reasons': set(),
            'playback': {}, 'has_updated_at': False,
        }
        for observation in observations:
            result = observation.get('result') or {}
            canonical = self._canon(observation.get('tool'))
            if canonical in ('catalog_summary', 'catalog_status'):
                for field, key in (('total', 'total'), ('movies', 'movies'),
                                   ('series', 'series'), ('unknown', 'unknown_kind'),
                                   ('episodes', 'episodes')):
                    self._ledger_add(ledger, field, result.get(key))
                self._ledger_add(ledger, 'total', result.get('entries'))
                kind_support = result.get('kind_support') or {}
                for field, key in (('movies', 'movie'), ('series', 'series'),
                                   ('unknown', 'unknown')):
                    self._ledger_add(ledger, field, kind_support.get(key))
            elif canonical == 'catalog_get_episodes':
                item = result.get('item') or {}
                self._ledger_absorb_item(ledger, item)
                seasons = result.get('seasons') or []
                if seasons:
                    self._ledger_add(ledger, 'seasons', len(seasons))
                for number in seasons:
                    if type(number) is int:
                        ledger['season_numbers'].add(number)
                for detail in result.get('episodes') or []:
                    self._ledger_add(ledger, 'episodes', detail.get('episode_count'))
                    self._ledger_add(ledger, 'item_episodes', detail.get('episode_count'))
            elif canonical in ('catalog_list', 'catalog_search', 'catalog_recommend'):
                # ``total`` here is the count of *matching* rows, never the size of
                # the returned page, so it can stand for a "found N" claim.
                self._ledger_add(ledger, 'matched', result.get('total'))
                raw_tool = observation.get('tool')
                # An unfiltered browse reports the true count of its kind, so its
                # total may stand for the library total / movie / series count.
                # Filtered search, recommendations and recent subsets may not.
                if raw_tool in ('catalog_list', 'library_browse') \
                        and type(result.get('total')) is int:
                    kind = result.get('kind') or 'any'
                    if kind == 'movie':
                        ledger['movies'].add(result['total'])
                    elif kind == 'series':
                        ledger['series'].add(result['total'])
                    else:
                        ledger['total'].add(result['total'])
                items = result.get('items') or []
                ledger['list_len'] = max(ledger['list_len'], len(items))
                kinds = []
                for item in items:
                    self._ledger_absorb_item(ledger, item)
                    item_kind = item.get('kind')
                    kinds.append(item_kind if item_kind in ('movie', 'series', 'unknown')
                                 else 'unknown')
                    for reason in item.get('reasons') or []:
                        if isinstance(reason, str) and reason.strip():
                            ledger['recommend_reasons'].add(reason.strip())
                ledger['lists'].append({
                    'kind': result.get('kind') or 'any',
                    'items': kinds,
                    'len': len(items),
                    'total': result.get('total'),
                })
            elif canonical == 'catalog_get_playback_progress':
                ledger['playback'] = result
                if result.get('updated_at'):
                    ledger['has_updated_at'] = True
                if result.get('available') and result.get('title'):
                    ledger['titles'].add(result['title'])
                    self._ledger_add(ledger, 'episode_numbers', result.get('episode'))
                    self._ledger_add(ledger, 'season_numbers', result.get('season'))
        return ledger

    def _count_supported(self, field, value, ledger):
        if field == 'movies':
            return value in ledger['movies']
        if field == 'series':
            return value in ledger['series']
        if field == 'total':
            return value in ledger['total']
        if field == 'episodes':
            return value in (ledger['episodes'] or ledger['item_episodes'])
        if field == 'seasons':
            # A season number in an enumerated list ("第1、5-6、8-11季") is a real
            # observed season, not a season *count*; accept either reading.
            return value in ledger['seasons'] or value in ledger['season_numbers']
        if field == 'bare':
            # A bare "N 部" is only trustworthy as the library total from a
            # summary; it may never be satisfied by a page of list results.
            return value in ledger['total']
        return False

    @staticmethod
    def _group_count_ok(ledger, field, value):
        """Validate a "recommended/searched N <kind>" claim against row kinds.

        The count must not merely fit the page length: at least *value* of the
        returned rows have to be of the claimed kind. "N 部作品" may count any
        work, but "N 部电影" may only count rows whose kind is ``movie``.
        """
        want = {'movies': 'movie', 'series': 'series', 'unknown': 'unknown'}.get(field)
        for listing in ledger['lists']:
            if value > listing['len']:
                continue
            if field in ('total', 'bare'):
                return True
            if want and len([k for k in listing['items'] if k == want]) >= value:
                return True
        return False

    def _attributes_grounded(self, text, ledger):
        """Verify year / media type / genre / runtime / playback claims.

        A statement may only use metadata the tools actually returned. Absence is
        allowed to be *stated* ("没有元数据/类型未知"), but not filled in.
        """
        for match in _YEAR_CLAIM.finditer(text):
            if int(match.group(1)) not in ledger['years']:
                return False
        for match in _KIND_CLAIM.finditer(text):
            expected = _KIND_EXPECT.get(match.group('kind'))
            if expected is None:
                continue
            actual = ledger['title_kinds'].get(match.group('title'))
            if actual is None or actual != expected:
                return False
        scan = _TITLE.sub('', text)
        for match in _GENRE_CLAIM.finditer(scan):
            genre = match.group(1) or match.group(2)
            prefix = scan[max(0, match.start() - 6):match.start()]
            if _GENRE_NEGATION.search(prefix):
                continue
            if genre not in ledger['genres']:
                return False
        for match in _RUNTIME_CLAIM.finditer(text):
            amount = int(match.group(1))
            unit = match.group(2).lower()
            minutes = amount * 60 if unit.startswith(('小时', 'hour')) else amount
            if minutes not in ledger['runtimes']:
                return False
        if _PLAYING_CLAIM.search(text):
            playback = ledger['playback'] or {}
            if not (playback.get('available') and playback.get('playing')):
                return False
        if _PERCENT_CLAIM.search(text) and not (ledger['playback'] or {}).get('available'):
            return False
        if _UNSUPPORTED_ATTR.search(_TITLE.sub('', text)):
            return False
        return True

    def _unverified_title(self, text, known_titles):
        """Return a leftover name fragment when a title is written without 《》.

        The regex only proposes candidates; the ledger is the decider. After real
        titles, connectors and everyday filler are removed, any Chinese run of two
        or more characters left over cannot be proven and counts as fabricated.
        """
        for match in _TITLE_VERB.finditer(text):
            residual = match.group('name')
            for title in sorted(known_titles, key=len, reverse=True):
                residual = residual.replace(title, '')
            residual = _strip_words(residual, _CONNECTORS)
            residual = _strip_words(residual, _FILLER)
            if _residual_cjk(residual):
                return residual
        return None

    def grounded(self, text, observations):
        """Accept an answer only if every fact is provable from tool data.

        V4.3.1 binds each count to the field it names (movies / series / total /
        episodes / a real episode or season number) instead of accepting "any
        number that appeared somewhere". With no successful observation, no
        title or number may be spoken at all. When the text cannot be proven the
        caller renders the raw tool result instead.
        """
        if not isinstance(text, str) or not text.strip():
            return False
        ledger = self._build_ledger(observations)

        # ---- no evidence at all: only pure, fact-free chat is allowed ----
        if not observations:
            has_number = bool(_NUM_TOKEN.search(text))
            has_bracket = bool(_TITLE.search(text)) or bool(_TITLE_QUOTED.search(text))
            has_named = bool(self._unverified_title(text, ledger['titles']))
            return not (has_number or has_bracket or has_named)

        # ---- bracketed titles must be real observed works ----
        cited = [m.group(1) for m in _TITLE.finditer(text) if m.group(1)]
        for match in _TITLE_QUOTED.finditer(text):
            cited.extend(group for group in match.groups() if group)
        for title in cited:
            if title not in ledger['titles']:
                return False

        # ---- "N 部 <kind>" / "<kind> N 部" bound to the named field ----
        def references_returned_group(start):
            """True when "N 部" refers to the returned rows (前20部/这三部/挑了N部)."""
            prefix = text[max(0, start - 14):start]
            return bool(re.search(
                r'(?:前|后|这|那|该|上述|以下|其中|显示|列出|显示最新|最新|最近|排序|'
                r'挑了|推荐了|选了|列了)[^，。；\n]{0,10}$', prefix))

        covered = []
        for regex in (_COUNT_BEFORE, _COUNT_AFTER):
            for match in regex.finditer(text):
                covered.append(match.span())
                value = cn_number(match.group('num'))
                field = _KIND_FIELD.get(match.group('word'))
                if value is None or field is None:
                    return False
                if self._count_supported(field, value, ledger):
                    continue
                # A returned group may only prove a kind-qualified count if the
                # returned rows really are that kind (V4.3.2): "推荐了三部电影"
                # cannot be satisfied by three series.
                if references_returned_group(match.start()) \
                        and self._group_count_ok(ledger, field, value):
                    continue
                return False

        # ---- ambiguous "N 部/个/条" (no kind) may not use a page size ----
        for match in _BARE_WORK.finditer(text):
            # Skip "N 部" that is already part of a "<kind> N 部" claim above.
            if any(start <= match.start() < end for start, end in covered):
                continue
            value = cn_number(match.group('num'))
            if value is None:
                return False
            if self._count_supported('bare', value, ledger):
                continue
            if references_returned_group(match.start()) \
                    and self._group_count_ok(ledger, 'bare', value):
                continue
            return False

        for match in _EPISODE_COUNT.finditer(text):
            value = cn_number(match.group('num'))
            if value is None or not self._count_supported('episodes', value, ledger):
                return False
        for match in _SEASON_COUNT.finditer(text):
            value = cn_number(match.group('num'))
            if value is None or not self._count_supported('seasons', value, ledger):
                return False

        # ---- ordinals must index something this run actually produced ----
        for match in _WORK_ORDINAL.finditer(text):
            value = cn_number(match.group('num'))
            if value is None or not (1 <= value <= ledger['list_len']):
                return False
        for match in _ITEM_ORDINAL.finditer(text):
            value = cn_number(match.group('num'))
            if value is None or not (1 <= value <= ledger['list_len']):
                return False
        for match in _EPISODE_ORDINAL.finditer(text):
            value = cn_number(match.group('num'))
            if value is None:
                return False
            allowed = set(ledger['episode_numbers'])
            if not allowed and ledger['item_episodes']:
                allowed = set(range(1, max(ledger['item_episodes']) + 1))
            if value not in allowed:
                return False
        for match in _SEASON_ORDINAL.finditer(text):
            value = cn_number(match.group('num'))
            if value is None:
                return False
            allowed = set(ledger['season_numbers'])
            if not allowed and ledger['seasons']:
                allowed = set(range(1, max(ledger['seasons']) + 1))
            if value not in allowed:
                return False

        # ---- a precise clock time needs a real observed timestamp ----
        if not ledger['has_updated_at']:
            if _TIME_CLOCK.search(text) or _TIME_CN.search(text):
                return False

        # ---- year / media type / genre / runtime / playback need real metadata ----
        if not self._attributes_grounded(text, ledger):
            return False

        # ---- names written without 《》 must resolve to real titles ----
        if self._unverified_title(text, ledger['titles']):
            return False

        return True

    @staticmethod
    def answer_source(observations, trace):
        """Provenance label that only credits *successful* tool observations."""
        used = {TOOL_ALIASES.get(o.get('tool'), o.get('tool'))
                for o in observations or [] if o.get('result') is not None}
        if not used:
            return 'unverified'
        if used & {'catalog_summary', 'catalog_status', 'catalog_list',
                   'catalog_search', 'catalog_recommend', 'catalog_get_episodes'}:
            return 'local_catalog'
        if 'catalog_get_playback_progress' in used:
            return 'device_status'
        return 'unverified'

    # ---------- rendering (deterministic, observation-only) ----------
    @staticmethod
    def _canon(name):
        return TOOL_ALIASES.get(name, name)

    def _result_of(self, observations, *names):
        wanted = {self._canon(n) for n in names}
        for observation in reversed(observations):
            if self._canon(observation.get('tool')) in wanted:
                return observation.get('result') or {}
        return {}

    @staticmethod
    def _label(kind):
        return {'movie': '电影', 'series': '剧集', 'unknown': '类型未知的作品',
                'any': '影视作品'}.get(kind, '影视作品')

    def _render_items(self, items, intro):
        if not items:
            return intro
        lines = [f'{index + 1}.《{item["title"]}》'
                 + (f'（{item["year"]}）' if item.get('year') else '')
                 + (f' · {item["episodes"]} 集' if item.get('episodes') else '')
                 for index, item in enumerate(items)]
        return intro + '：' + '；'.join(lines) + '。可以说“第几部”继续选择。'

    def _render_progress(self, observations):
        """Deterministic playback-progress reply; honest when no record exists."""
        data = self._result_of(observations, 'catalog_get_playback_progress')
        if not data or not data.get('available') or not data.get('title'):
            return '我还没有记录到你上次播放到哪一集。可以说片名让我查，或说“随便推荐一部”。'
        title = data['title']
        season, episode = data.get('season'), data.get('episode')
        if data.get('source') == 'device_status' and data.get('playing'):
            parts = [f'现在正在播放《{title}》']
        else:
            parts = [f'上次记录到的是《{title}》']
        if season:
            parts.append(f'第{season}季')
        if episode:
            parts.append(f'第{episode}集')
        text = '，'.join(parts) + '。'
        if data.get('source') == 'device_status' and data.get('playing'):
            text += '可以说“暂停”或“接着看”。'
        else:
            text += '可以说“接着看”继续。'
        return text

    def _render_summary(self, summary):
        if not isinstance(summary, dict) or 'total' not in summary:
            return '这次没有取到影视库统计。' + CAPABILITY_REPLY
        if not summary.get('available'):
            return ('现在读不到本地影视库索引。' + CAPABILITY_REPLY)
        parts = [f'本地影视库共 {summary["total"]} 部作品（{summary["episodes"]} 集）',
                 f'其中电影 {summary["movies"]} 部、剧集 {summary["series"]} 部']
        if summary.get('unknown_kind'):
            parts.append(f'另有 {summary["unknown_kind"]} 部类型无法确定')
        text = '，'.join(parts) + '。'
        if summary.get('metadata_missing'):
            text += (f'其中 {summary["metadata_missing"]} 部缺少类型/地区元数据'
                     '（没有 NFO 信息），所以我不会猜测它们的类型。')
        return text

    # ---------- deterministic planner ----------
    GENRE_WORDS = ('悬疑', '科幻', '喜剧', '爱情', '动作', '犯罪', '历史', '战争',
                   '恐怖', '家庭', '动画', '纪录片', '奇幻', '武侠', '都市', '古装')

    def _plan(self, text, state):
        """Rule-based tool choice used without a model and to render fallbacks."""
        clean = (text or '').strip()
        compact = re.sub(r'[\s，,。？?！!]', '', clean)
        state = state or {}
        if re.search(r'上次看到|看到哪|上回看到|上次看哪|播放进度|看到第几集|看到哪儿|上次看的|看到第几', compact):
            return [('catalog_get_playback_progress', {})], 'progress'
        if re.search(r'不可用|还能做什么|能做什么|坏了|连不上|怎么回事|健康', compact):
            return [('library_health', {})], 'health'
        if re.search(r'最近|新加|新增|刚加|更新了', compact):
            return [('library_recent', {'limit': 5})], 'recent'
        short = bool(re.search(r'不要太长|别太长|短一点|短些|集数少|少一点|太长', compact))
        if re.search(r'类似|差不多|换一部|换一个|再来一部', compact):
            previous = state.get('last_item') or {}
            arguments = {'limit': 5}
            if previous.get('id'):
                arguments['exclusions'] = [previous['id']]
            return [('library_recommend', arguments)], 'recommend'
        if re.search(r'推荐|随便|挑一部|挑个|好看的|来一部|来点|有什么好看', compact):
            kind = 'movie' if '电影' in compact else ('series' if re.search(r'剧|短剧', compact) else 'any')
            arguments = {'limit': 5, 'kind': kind}
            if short:
                arguments['max_episodes'] = (state.get('last_query') or {}).get('max_episodes') or 120
            return [('library_recommend', arguments)], 'recommend'
        if short:
            previous = state.get('last_query') or {}
            arguments = {'limit': 5,
                         'max_episodes': previous.get('max_episodes') or 120}
            if previous.get('kind'):
                arguments['kind'] = previous['kind']
            return [('library_recommend', arguments)], 'recommend'
        kind = 'movie' if '电影' in compact else (
            'series' if re.search(r'电视剧|剧集|短剧', compact) else 'any')
        genre = next((g for g in self.GENRE_WORDS if g in compact), None)
        # An explicit quoted/short title still wins over a genre guess.
        lookup = re.fullmatch(r'(?:帮我|给我)?(?:搜索|找一下|搜一下|找|搜|查)\s*(?:一下)?\s*(.+)', compact)
        payload = None
        if lookup:
            candidate = re.sub(r'^(?:个|部|点|些|一部|一个|几部)', '', lookup.group(1)).strip('的了')
            if candidate and len(candidate) >= 2 and candidate not in self.GENRE_WORDS \
                    and not re.fullmatch(r'(?:电影|电视剧|短剧|动画|纪录片)', candidate):
                payload = candidate
        if genre and (payload is None or genre in payload):
            arguments = {'kind': kind, 'limit': 8, 'genre': genre}
            return [('library_search', arguments)], 'search'
        if payload:
            return [('library_search', {'query': payload, 'limit': 8})], 'search'
        wants_count = bool(re.search(r'多少|几部|一共|总数|统计', compact))
        if wants_count or re.search(r'有哪些|有什么|资源库|影视库|媒体库|片库|资源', compact):
            calls = [('library_summary', {})]
            if kind != 'any' or re.search(r'有哪些|有什么', compact):
                calls.append(('library_browse', {'kind': kind, 'limit': 8}))
            return calls, 'summary'
        if kind != 'any':
            return [('library_browse', {'kind': kind, 'limit': 8})], 'search'
        return [], None

    def _render_plan(self, text, observations, plan_kind):
        if plan_kind == 'progress':
            return self._render_progress(observations)
        if plan_kind == 'health':
            health = self._result_of(observations, 'catalog_status', 'library_health')
            if not health.get('available'):
                missing = [r['root'] for r in health.get('roots') or [] if not r.get('exists')]
                detail = ('挂载不存在：' + '、'.join(missing)) if missing else '索引为空或无法读取'
                return '本地影视库当前不可用（' + detail + '）。' + CAPABILITY_REPLY
            coverage = health.get('metadata_coverage') or {}
            return (f'本地影视库可用：{health.get("entries")} 部作品，'
                    f'索引更新于 {round((health.get("index_age_seconds") or 0) / 60)} 分钟前；'
                    f'{coverage.get("missing", 0)} 部缺少类型元数据。' + CAPABILITY_REPLY)
        if plan_kind == 'recent':
            items = self._items_of({'result': self._result_of(observations, 'catalog_list', 'library_recent')})
            return self._render_items(items, '最近加入/更新的本地作品是')
        if plan_kind == 'recommend':
            result = self._result_of(observations, 'catalog_recommend', 'library_recommend')
            items = result.get('items') or []
            if not items:
                summary = self.tools.summary()
                wanted = result.get('kind', 'any')
                if wanted == 'movie' and not summary.get('movies'):
                    return (f'本地库里目前没有电影，有 {summary["series"]} 部剧集'
                            f'（共 {summary["episodes"]} 集）。要我列几部剧集，或者按片名找吗？')
                if result.get('max_episodes'):
                    return (f'没有 {result["max_episodes"]} 集以内的作品。'
                            '可以放宽集数限制，或者换个条件。')
                return '本地库里没有可推荐的作品。' + CAPABILITY_REPLY
            return self._render_items(items, '从本地已入库内容里给你挑了这几部')
        if plan_kind == 'search':
            result = self._result_of(observations, 'catalog_search', 'catalog_list',
                                     'library_search', 'library_browse')
            items = result.get('items') or []
            kind = result.get('kind', 'any')
            if result.get('genre') and not items and not result.get('total'):
                summary = self.tools.summary()
                if summary.get('metadata_missing'):
                    return (f'本地库没有类型标签（{summary["metadata_missing"]}/{summary["total"]} 部缺元数据），'
                            f'所以无法按“{result["genre"]}”筛选。可以按片名搜索，或直接浏览最近作品。')
                labelled = summary['total'] - summary['metadata_missing']
                return (f'本地库里有类型标签的 {labelled} 部作品中没有“{result["genre"]}”。'
                        '可以按片名搜索，或让我列几部带类型标签的作品。')
            if not items:
                summary = self.tools.summary()
                if kind == 'movie' and not summary.get('movies'):
                    return (f'本地库里目前没有电影，有 {summary["series"]} 部剧集（共 {summary["episodes"]} 集）。'
                            '要我列几部剧集，或者按片名找吗？')
                return '本地库里没有符合条件的作品。可以换个片名或去掉筛选条件再试。'
            intro = f'找到 {result.get("total", len(items))} 部' + self._label(kind)
            return self._render_items(items, intro)
        # summary
        summary = self._result_of(observations, 'catalog_summary', 'library_summary')
        if not summary:
            browse_only = self._result_of(observations, 'catalog_list', 'library_browse') or None
            if browse_only and browse_only.get('items'):
                return self._render_items(browse_only['items'],
                                          f'本地影视库里这些{self._label(browse_only.get("kind", "any"))}是')
            return '这次没有取到影视库统计。' + CAPABILITY_REPLY
        text_out = self._render_summary(summary)
        browse = next((o['result'] for o in observations if o['tool'] == 'library_browse'), None)
        if browse and browse.get('items'):
            items = browse['items']
            text_out += (' 例如：' + '；'.join(
                f'{i + 1}.《{item["title"]}》' for i, item in enumerate(items)) + '。')
        elif browse is not None and browse.get('kind') == 'movie' and not browse.get('items'):
            text_out += ' 目前没有电影条目，只有剧集。'
        return text_out

    # ---------- model ----------
    def _model_available(self):
        return bool(getattr(self.llm, 'enabled', False))

    def _call_model(self, messages, deadline=None):
        """One model call bounded by the task's real remaining budget."""
        from .llm import LLMHTTPError, LLMTimeout

        started = self.clock()
        remaining = None if deadline is None else max(0.0, deadline - self.clock())
        if remaining is not None and remaining <= 0.2:
            return None, 0.0, 'budget_exhausted'
        system = SYSTEM_PROMPT.replace('{tool_list}',
                                       json.dumps(TOOL_SCHEMAS, ensure_ascii=False))
        attempts = 0
        while True:
            attempts += 1
            try:
                raw = self.llm.chat(messages, system=system, timeout=remaining)
                break
            except LLMTimeout:
                return None, (self.clock() - started) * 1000, 'timeout'
            except LLMHTTPError as exc:
                # Read-only model calls may retry once on 429/5xx, but only if the
                # task budget still allows a second attempt.
                left = None if deadline is None else deadline - self.clock()
                if exc.retryable and attempts < 2 and (left is None or left > 1.0):
                    remaining = left
                    continue
                return None, (self.clock() - started) * 1000, f'http_{exc.status}'
            except Exception as exc:  # noqa: BLE001 - connection/protocol failure
                return None, (self.clock() - started) * 1000, type(exc).__name__
        elapsed = (self.clock() - started) * 1000
        if not isinstance(raw, str) or len(raw) > 20000:
            return None, elapsed, 'invalid_response'
        action = extract_json_object(raw)
        if action is None:
            return None, elapsed, 'invalid_json'
        return action, elapsed, None

    # ---------- public entry ----------
    def run(self, text, *, state=None, budget=None):
        """Run the loop; returns a reply dict with diagnostics and tool trace."""
        started = self.clock()
        state = state or {}
        budget = self.budget if budget is None else max(0.5, float(budget))
        deadline = started + budget
        diagnostics = {'model_name': getattr(self.llm, 'model', '') or '',
                       'decision_source': 'deterministic',
                       'tool_name': None, 'tool_status': None,
                       'llm_latency_ms': 0.0, 'tool_latency_ms': 0.0,
                       'total_latency_ms': 0.0, 'fallback_reason': None,
                       'steps': 0, 'budget_seconds': round(budget, 3),
                       'failed_stage': None, 'answer_source': 'unverified'}
        observations, trace = [], []
        model_failure = None
        if self._model_available():
            context = {'library_scope': state.get('library_scope') or 'local',
                       'last_query': state.get('last_query') or {},
                       'preferences': state.get('preferences') or {},
                       'candidates': [{'id': c.get('id'), 'title': c.get('title')}
                                      for c in (state.get('candidates') or [])][:8],
                       'tv': state.get('tv') or {}}
            messages = [{'role': 'user', 'content': json.dumps(
                {'utterance': text, 'context': context,
                 'tools': sorted(TOOL_SCHEMAS)}, ensure_ascii=False)}]
            for step in range(self.max_steps):
                if self.clock() > deadline:
                    model_failure = 'budget_exhausted'
                    diagnostics['failed_stage'] = 'model'
                    break
                action, llm_ms, failure = self._call_model(messages, deadline=deadline)
                diagnostics['llm_latency_ms'] += llm_ms
                diagnostics['steps'] = step + 1
                if failure:
                    model_failure = failure
                    diagnostics['failed_stage'] = 'model'
                    break
                try:
                    validated = validate_action(action)
                except ToolError:
                    model_failure = 'invalid_action'
                    break
                if validated['action'] == 'tool':
                    if self.clock() > deadline:
                        model_failure = 'budget_exhausted'
                        diagnostics['failed_stage'] = 'tool'
                        break
                    try:
                        result, tool_ms = self._run_tool(validated['tool'],
                                                         validated['arguments'], state)
                    except (ToolError, ValueError, TypeError, KeyError, RuntimeError,
                            OSError) as exc:
                        model_failure = 'tool_error'
                        diagnostics['failed_stage'] = 'tool'
                        trace.append({'tool': validated['tool'], 'status': 'error',
                                      'error': type(exc).__name__})
                        break
                    diagnostics['tool_latency_ms'] += tool_ms
                    observation = {'tool': validated['tool'],
                                   'arguments': validated['arguments'],
                                   'result': result}
                    observations.append(observation)
                    trace.append({'tool': validated['tool'], 'status': 'ok',
                                  'arguments': validated['arguments']})
                    messages.append({'role': 'assistant', 'content': json.dumps(
                        validated, ensure_ascii=False)})
                    messages.append({'role': 'user', 'content': json.dumps(
                        {'observation': observation}, ensure_ascii=False)})
                    continue
                if validated['action'] == 'propose_play':
                    item = self.tools.find(validated['title'])
                    if item is None:
                        model_failure = 'proposed_unknown_title'
                        break
                    diagnostics.update(decision_source='llm',
                                       tool_name='catalog_search', tool_status='ok',
                                       answer_source='local_catalog')
                    diagnostics['total_latency_ms'] = (self.clock() - started) * 1000
                    return {'status': 'NEEDS_CONFIRMATION', 'proposal': {
                                'item': item,
                                'season': validated.get('season'),
                                'episode': validated.get('episode')},
                            'reply': '', 'candidates': [], 'tool_trace': trace,
                            'diagnostics': diagnostics}
                answer = validated['text']
                if self.grounded(answer, observations):
                    diagnostics.update(decision_source='llm',
                                       tool_name=(trace[-1]['tool'] if trace else None),
                                       tool_status=('ok' if trace else 'none'),
                                       answer_source=self.answer_source(observations, trace))
                    diagnostics['total_latency_ms'] = (self.clock() - started) * 1000
                    return {'status': 'SUCCESS' if validated['action'] == 'answer'
                            else 'NEEDS_CLARIFICATION',
                            'reply': answer, 'candidates': [], 'tool_trace': trace,
                            'answer_source': diagnostics.get('answer_source', 'unverified'),
                            'diagnostics': diagnostics}
                model_failure = 'ungrounded_answer'
                break
        # ---- deterministic fallback ----
        calls, plan_kind = self._plan(text, state)
        if observations:
            # Reuse whatever the model already fetched instead of re-running a
            # different tool set for the fallback rendering.
            plan_kind = {'catalog_summary': 'summary', 'catalog_status': 'health',
                         'catalog_list': 'search', 'catalog_search': 'search',
                         'catalog_recommend': 'recommend',
                         'catalog_get_episodes': 'search',
                         'catalog_get_playback_progress': 'progress'}.get(
                             self._canon(observations[-1].get('tool')), plan_kind)
        if not observations:
            for name, arguments in calls:
                try:
                    result, tool_ms = self._run_tool(name, arguments, state)
                except (ToolError, ValueError, TypeError, KeyError, RuntimeError,
                        OSError) as exc:
                    diagnostics['failed_stage'] = 'tool'
                    trace.append({'tool': name, 'status': 'error',
                                  'error': type(exc).__name__})
                    continue
                diagnostics['tool_latency_ms'] += tool_ms
                observations.append({'tool': name, 'arguments': arguments,
                                     'result': result})
                trace.append({'tool': name, 'status': 'ok', 'arguments': arguments})
        if plan_kind is None:
            diagnostics.update(fallback_reason=model_failure or 'no_plan',
                               decision_source='deterministic')
            diagnostics['total_latency_ms'] = (self.clock() - started) * 1000
            return {'status': 'NEEDS_CLARIFICATION',
                    'reply': '我还不能确定你要做什么。' + CAPABILITY_REPLY,
                    'candidates': [], 'tool_trace': trace,
                    'diagnostics': diagnostics}
        reply = self._render_plan(text, observations, plan_kind)
        if model_failure and model_failure not in ('invalid_json',):
            reply = reply  # facts unchanged; the failure reason is in diagnostics
        diagnostics.update(decision_source='deterministic',
                           tool_name=(trace[-1]['tool'] if trace else None),
                           tool_status=('ok' if trace else 'none'),
                           fallback_reason=model_failure,
                           answer_source=self.answer_source(observations, trace))
        diagnostics['total_latency_ms'] = (self.clock() - started) * 1000
        candidates = []
        for observation in observations:
            candidates.extend(self._items_of(observation))
        return {'status': 'SUCCESS' if observations else 'NEEDS_CLARIFICATION',
                'reply': reply, 'candidates': candidates, 'tool_trace': trace,
                'answer_source': diagnostics.get('answer_source', 'unverified'),
                'diagnostics': diagnostics}
