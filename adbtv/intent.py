"""Conservative local routing for frequent commands; ambiguous requests stay with the LLM."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Intent:
    action: str
    app: str = ""
    title: str = ""
    season: int | None = None
    episode: int | None = None
    key: str = ""
    drama: bool = False


def number(text: str) -> int:
    """Parse an explicit Arabic integer or standard Chinese integer below 1000.

    Invalid or ambiguous text raises ValueError; it never silently becomes zero.
    """
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"两": 2, "〇": 0})
    if text in digits:
        return digits[text]
    unit = "一二两三四五六七八九"
    valid = (rf"(?:[{unit}]百(?:零[{unit}]|[{unit}]十[{unit}]?)?|"
             rf"[{unit}]?十[{unit}]?)")
    if not re.fullmatch(valid, text):
        raise ValueError(f"无法识别的数字: {text}")
    total, pending = 0, 0
    for char in text:
        if char in digits:
            pending = digits[char]
        else:
            total += (pending or 1) * {"百": 100, "十": 10}[char]
            pending = 0
    return total + pending


def parse_fast(instruction: str) -> Intent | None:
    text = re.sub(r"[\s，,。！!？?]", "", instruction or "")
    # Full matches avoid turning a complex instruction into one isolated key press.
    keys = {"暂停": "pause", "暂停播放": "pause", "继续": "play", "继续播放": "play",
            "恢复播放": "play", "返回": "back", "返回上一级": "back", "返回桌面": "home",
            "回到桌面": "home", "音量加": "volume_up", "调大音量": "volume_up",
            "声音大一点": "volume_up", "音量减": "volume_down", "调小音量": "volume_down",
            "声音小一点": "volume_down"}
    if text in keys:
        return Intent("key", key=keys[text])
    if not ("飞牛" in text or "feiniu" in text.casefold()):
        return None
    if any(w in text for w in ("红果应用", "红果app", "哔哩哔哩", "爱奇艺", "腾讯视频")):
        return None
    if re.fullmatch(r"(?:请|帮我)?(?:打开|启动|进入)(?:飞牛tv|飞牛|feiniu)", text, re.IGNORECASE):
        return Intent("open", app="feiniu_tv")
    m = re.fullmatch(r"(?:请|帮我)?(?:打开|启动)?(?:飞牛tv|飞牛|feiniu)(?:然后)?(?:播放|放|继续播放|继续观看|续播)(.*)", text, re.IGNORECASE)
    if not m:
        return None
    content = m.group(1)
    if content in ("", "一个视频", "一部视频"):
        return Intent("play", app="feiniu_tv")
    if content in ("短剧", "一个短剧", "一部短剧", "个短剧", "部短剧"):
        return Intent("play", app="feiniu_tv", drama=True)
    # Do not flatten multi-action or recommendation requests into a title.
    if any(w in content for w in ("然后", "并且", "之后", "分钟", "定时", "随便", "找个", "推荐")):
        return None
    numeral = r"[0-9零〇一二三四五六七八九十百两]+"
    season_m = re.search(rf"第({numeral})(?:季|部)", content)
    episode_m = re.search(rf"第({numeral})集", content)
    title = re.sub(rf"第{numeral}(?:季|部|集)", "", content).strip("《》\"“”")
    if not title:
        return None
    try:
        season = number(season_m.group(1)) if season_m else None
        episode = number(episode_m.group(1)) if episode_m else None
    except ValueError:
        return None
    return Intent("play", app="feiniu_tv", title=title,
                  season=season, episode=episode, drama=True)


# Request plans deliberately use a finite grammar. Unknown clauses invalidate the
# whole plan so callers cannot accidentally execute only a recognized prefix.
from .verifier import GoalSpec

_NUM = r"[0-9零〇一二三四五六七八九十百两]+"
# 语音输入常带语气词与口语动词；这些不携带语义。
_FILLERS = "吧啊呢呀哦嘛啦哈呗哇咯"
_SOFT_VERBS = (("看一下", "看"), ("看一看", "看"), ("看看", "看"),
               ("瞧瞧", "看"), ("瞅瞅", "看"), ("放一下", "放"),
               ("播一下", "播放"), ("搜一下", "搜索"))


def _normalize_speech(text: str) -> str:
    """Drop trailing fillers and map colloquial verbs to their plain form."""
    for src, dst in _SOFT_VERBS:
        text = text.replace(src, dst)
    text = re.sub(rf"[{_FILLERS}]+$", "", text)
    text = re.sub(rf"[{_FILLERS}]+(?=[，,。.!！?？]|$)", "", text)
    return text
_DURATION = rf"({_NUM})(小时|分钟|秒钟|秒|分)"
_ACTION_START = r"(?:打开|启动|进入|播放|放|搜索|查找|暂停|继续|恢复|返回|回|音量|声音|调|快进|快退|后退|跳|下一集|上一集|从头)"
_KNOWN_APPS = {
    "feiniu_tv": ("飞牛TV", "飞牛", "feiniu"),
    "feiniu_drama": ("飞牛TV", "飞牛", "feiniu"),
    "bilibili": ("B站", "哔哩哔哩", "哔哩哔哩TV", "云视听小电视", "小电视"),
    "bilibili_hd": ("B站", "哔哩哔哩"),
    "iqiyi": ("爱奇艺", "银河奇异果"),
    "tencent": ("腾讯视频", "云视听极光"),
    "youku": ("优酷", "酷喵"),
    "mgtv": ("芒果TV", "芒果"),
    "douyin_tv": ("抖音TV", "抖音", "鲜时光"),
    "hongguo": ("红果", "红果短剧", "红果免费短剧"),
}


def _unknown(reason: str) -> GoalSpec:
    return GoalSpec("unknown", reason=reason)


def _app_names(cfg: dict) -> list[tuple[str, str]]:
    names = {}
    for alias, app in cfg.get("apps", {}).items():
        if not isinstance(app, dict) or not app.get("package"):
            continue
        labels = re.split(r"[/（(]", str(app.get("label", "")))
        known = _KNOWN_APPS.get(alias, ())
        for name in (alias, *labels, *known):
            name = re.sub(r"\s+", "", name).strip()
            if name:
                names.setdefault(name.casefold(), (name, app["package"]))
    return sorted(names.values(), key=lambda item: len(item[0]), reverse=True)


def _take_app(text: str, cfg: dict) -> tuple[str, str]:
    """Consume only a leading named app; never match inside a quoted title."""
    candidate = re.sub(r"^(?:在|用)", "", text)
    for name, package in _app_names(cfg):
        if candidate[:len(name)].casefold() == name.casefold():
            rest = candidate[len(name):]
            rest = re.sub(r"^(?:里面|里|上|中|的)", "", rest)
            return package, rest
    return "", text


def _seconds(value: str, unit: str) -> float:
    amount = number(value)
    if amount <= 0:
        raise ValueError("时间必须大于零")
    return float(amount * ({"小时": 3600, "分钟": 60, "分": 60}.get(unit, 1)))


def _mask_titles(text: str) -> tuple[str, dict[str, str]]:
    titles = {}
    def replace(match):
        token = f"\ue000{chr(0xE100 + len(titles))}\ue001"
        titles[token] = match.group(1)
        return token
    masked = re.sub(r"《([^《》]+)》", replace, text)
    if "《" in masked or "》" in masked:
        raise ValueError("书名号未配对或标题为空")
    # Whitespace outside titles has no semantic meaning in this finite grammar.
    return re.sub(r"\s+", "", masked), titles


def _restore(text: str, titles: dict[str, str]) -> str:
    for token, title in titles.items():
        text = text.replace(token, title)
    return text


def _one(text: str, cfg: dict, inherited: str, titles: dict[str, str]) -> GoalSpec:
    text = re.sub(r"^(?:请帮我|请|帮我|麻烦你|麻烦)", "", text)
    text = text.strip("，,。.!！?？;；")
    # Time syntax is processed before controls so delay can never become pause.
    delayed = re.fullmatch(rf"(?:等|过)?{_DURATION}(?:以后|之后|后)(.+)", text)
    if delayed:
        child = _one(delayed.group(3), cfg, inherited, titles)
        if child.action == "unknown":
            return child
        return GoalSpec("delay", package=child.package,
                        seconds=_seconds(delayed.group(1), delayed.group(2)), steps=(child,))

    package, rest = _take_app(text, cfg)
    if package:
        text = rest
    else:
        package = inherited
    # "打开飞牛播放X" carries one play goal whose package requires opening it.
    opening = re.match(r"^(?:打开|启动|进入)(.*)$", text)
    if opening:
        target_package, tail = _take_app(opening.group(1), cfg)
        if not target_package:
            return _unknown("打开应用需要明确且已配置的应用名")
        package = target_package
        # "打开飞牛看看" / "打开B站" 都是打开应用；裸浏览动词不构成额外要求。
        if not tail or re.fullmatch(r"(?:看|浏览|看看|瞧瞧|瞅瞅)+", tail):
            return GoalSpec("open", package=package)
        text = tail

    exact = {
        "暂停": "pause", "暂停播放": "pause", "继续": "resume", "继续播放": "resume",
        "恢复播放": "resume", "继续观看": "resume", "续播": "resume", "继续刚才那部": "resume",
        "继续刚才的": "resume", "返回": "back", "返回上一级": "back", "返回上一页": "back",
        "返回桌面": "home", "回到桌面": "home", "回桌面": "home", "回首页": "home",
        "返回首页": "home", "回到首页": "home", "下一集": "next", "播放下一集": "next",
        "上一集": "previous", "播放上一集": "previous", "从头播放": "play", "从头开始播放": "play",
    }
    if text in exact:
        return GoalSpec(exact[text], package=package, policy="restart" if "从头" in text else "resume")
    if re.fullmatch(r"(?:现在|当前)?(?:在|正)?(?:哪个|什么)应用(?:里)?", text) or text in (
            "当前在哪个应用", "现在在哪个应用", "当前应用是什么", "现在打开的是什么应用"):
        return GoalSpec("query_app", package=package)
    if text in ("现在播放的是什么", "当前播放的是什么", "正在播放什么", "现在在播放什么",
                "播放的是什么", "这是什么片", "现在放的是什么", "当前在播什么", "查询当前播放"):
        return GoalSpec("query_media", package=package)

    # 绝对音量："音量调节到1" / "把音量调到5" / "音量设为3"
    volume_set = re.fullmatch(
        rf"(?:把|将)?(?:音量|声音)(?:调节|调整|调|设置|设)?(?:到|成|为|至)({_NUM})(?:格|级|档)?", text)
    if volume_set:
        level = number(volume_set.group(1))
        if not 0 <= level <= 100:
            return _unknown("目标音量超出支持范围")
        return GoalSpec("volume_set", package=package, level=level)

    volume = re.fullmatch(rf"(?:音量|声音)(加|减|增加|减少|调大|调小)(?:({_NUM})(?:格|级|档)?)?", text)
    if volume:
        delta = number(volume.group(2)) if volume.group(2) else 1
        if not 1 <= delta <= 100:
            return _unknown("音量调整量超出支持范围")
        return GoalSpec("volume_up" if volume.group(1) in ("加", "增加", "调大") else "volume_down",
                        package=package, delta=delta)
    volume_phrases = {
        "调大音量": "volume_up", "调小音量": "volume_down", "声音大一点": "volume_up",
        "声音小一点": "volume_down", "调大一点声音": "volume_up", "调小一点声音": "volume_down",
        "音量大一点": "volume_up", "音量小一点": "volume_down", "调大一点音量": "volume_up",
        "调小一点音量": "volume_down", "调大一点": "volume_up", "调小一点": "volume_down",
    }
    if text in volume_phrases:
        return GoalSpec(volume_phrases[text], package=package)

    seek = re.fullmatch(rf"(快进|快退|后退){_DURATION}", text)
    if seek:
        seconds = _seconds(seek.group(2), seek.group(3))
        return GoalSpec("seek", package=package, seconds=seconds if seek.group(1) == "快进" else -seconds)
    absolute = re.fullmatch(rf"(?:跳到|跳转到|播放到)第?{_DURATION}", text)
    if absolute:
        return GoalSpec("seek_to", package=package, seconds=_seconds(absolute.group(1), absolute.group(2)))

    episode = re.fullmatch(rf"(?:播放|切换到|跳到|选)?第({_NUM})集", text)
    if episode:
        value = number(episode.group(1))
        return (GoalSpec("episode", package=package, episode=value) if 1 <= value <= 999
                else _unknown("集数必须在1至999之间"))

    # Negation is recognized only as an explicit search-only policy. Other
    # negative commands cannot be safely approximated by a positive action.
    search_only = bool(re.search(r"(?:不要播放|不播放|只搜索|只查找)", text))
    if search_only:
        text = re.sub(r"(?:但是|但)?(?:不要播放|不播放)$", "", text)
        text = re.sub(r"^(?:只搜索|只查找)", "搜索", text)
        text = re.sub(r"(?:只搜索|只查找)$", "", text)
        # "不要播放X，只搜索" means search X without executing play.
        text = re.sub(r"^(?:不要播放|不播放)", "搜索", text)
    action_match = re.match(r"^(从头播放|播放|放|搜索|查找|找个|找一部|找部|找一个|继续播放|续播)(.*)$", text)
    if not action_match:
        return _unknown("无法识别的操作或仍有未支持的要求")
    verb, content = action_match.groups()
    # Existing demo phrasing places the playback verb after a find request.
    if verb in ("找个", "找一部", "找部", "找一个") and content.endswith("播放"):
        content = content[:-len("播放")]
    explicit_package, remaining = _take_app(content, cfg)
    if explicit_package:
        package, content = explicit_package, remaining
    if re.match(r"^(?:在|用)", content):
        return _unknown("应用未配置或应用位置不受支持")
    action = "search" if search_only or verb in ("搜索", "查找") else "play"
    policy = "restart" if verb == "从头播放" else "resume"
    if content.startswith("从头播放"):
        policy, content = "restart", content[len("从头播放"):]
    elif content.endswith("从头播放"):
        policy, content = "restart", content[:-len("从头播放")]

    # Parse only suffix season/episode markers; quoted title content is opaque.
    season = episode_number = None
    suffix = re.search(rf"((?:第{_NUM}(?:季|部|集))+)$", content)
    if suffix:
        content = content[:suffix.start()]
        for value, unit in re.findall(rf"第({_NUM})(季|部|集)", suffix.group(1)):
            value = number(value)
            if not 1 <= value <= 999:
                return _unknown("季集编号必须在1至999之间")
            if unit == "集":
                if episode_number is not None:
                    return _unknown("重复或冲突的集数")
                episode_number = value
            else:
                if season is not None:
                    return _unknown("重复或冲突的季数")
                season = value
    # These words indicate an unparsed constraint/action, never part of an
    # unquoted exact title. A quoted title can contain all of them safely.
    if re.search(r"然后|并且|之后|定时|每天|明天|今天|分钟|秒|小时|不要|不能|不许|但是|不过|除非|直到|搜索|暂停|返回|音量|下载|推荐|随便|调大|调小|打开|关闭|快进|后退|跳到|停止|关机|关电视|关掉|取消|删除|保存|设置|切换|倍速|字幕|timer|等待|同时|以及|并|第.+[季部集]|" + _NUM + r"点", content, re.IGNORECASE):
        return _unknown("存在未支持的附加动作、否定条件或时间要求")
    # A comma may legitimately belong to a title ("我，史前霸王龙"). Only treat
    # it as a separator when another action follows it.
    if re.search(r"[，,]\s*(?=" + _ACTION_START + r")", content):
        return _unknown("存在无法解释的附加动作")
    if any(mark in content for mark in (";", "；", "。", "!", "！", "?", "？")):
        return _unknown("存在无法解释的附加内容")
    quoted = [token for token in titles if token in content]
    if quoted and (len(quoted) != 1 or content != quoted[0]):
        return _unknown("片名之外仍有未识别的要求")
    generic = re.fullmatch(r"(?:一?[个部])?(?:短剧|视频|电影|电视剧|科技视频|科技|综艺)", content)
    if generic:
        # Genre requests are not exact titles. Keep the genre as a query hint.
        genre = re.sub(r"^一?[个部]", "", content)
        if genre == "科技视频":
            genre = "科技"
        return GoalSpec(action, package=package, query=genre, season=season, episode=episode_number,
                        policy=policy)
    title = _restore(content, titles).strip('“”"')
    if not title:
        if action == "search":
            return _unknown("搜索缺少关键词")
        return GoalSpec("play", package=package, season=season, episode=episode_number, policy=policy)
    if verb in ("找个", "找一部", "找部", "找一个") and not any(token in content for token in titles):
        # Unquoted "找个科技视频" is handled above; do not invent precision for
        # arbitrary recommendation descriptions that do not name a title.
        return _unknown("推荐式描述未明确片名，请使用书名号或明确播放指令")
    return GoalSpec(action, package=package, title=title, query=title if action == "search" else "",
                    season=season, episode=episode_number, policy=policy)


def parse_request(instruction: str, cfg: dict) -> GoalSpec:
    """Return a complete finite request plan, or an inert unknown goal.

    No models, I/O, default application guesses or partial-prefix execution.
    App names come from cfg.apps; explicitly named apps propagate to later steps.
    Titles inside 《》 preserve spelling/case and are opaque to splitting.
    """
    if not isinstance(instruction, str) or not instruction.strip():
        return _unknown("指令为空")
    try:
        masked, titles = _mask_titles(_normalize_speech(instruction.strip()))
        masked = masked.strip("，,。.!！?？;；")
        # Search-only qualifiers belong to their preceding clause, not a new step.
        masked = re.sub(r"(?:[，,]?(?:然后|并且)|[，,])(?=(?:不要播放|不播放|只搜索|只查找)(?:$|[，,]))", "", masked)
        masked = re.sub(r"[，,;；。](?=(?:然后|并且|接着|随后|再|同时|并|和|顺便))", "", masked)
        parts = re.split(r"(?:然后|并且|接着|随后|同时|(?:再|并|和|顺便)(?=" + _ACTION_START + r"))|[，,;；。]", masked)
        if not parts or any(not part for part in parts):
            return _unknown("复合指令含空步骤")
        # Real titles may contain a comma ("我，史前霸王龙"). Prefer reading the
        # whole clause as one request when that succeeds; only fall back to
        # splitting when the single-clause reading is not a valid request.
        if len(parts) > 1:
            single = _one(masked, cfg, "", titles)
            if single.action != "unknown":
                return single
        expanded = []
        for part in parts:
            # Implicit timed second action: 播放X十分钟后暂停. Quoted titles
            # have been masked, so a title cannot accidentally become a timer.
            timed = re.search(rf"{_DURATION}(?:以后|之后|后)(.+)$", part)
            if timed and timed.start() > 0 and part[:timed.start()] not in ("等", "过"):
                expanded.extend((part[:timed.start()], part[timed.start():]))
            else:
                expanded.append(part)
        steps = []
        inherited = ""
        for part in expanded:
            goal = _one(part, cfg, inherited, titles)
            if goal.action == "unknown":
                return _unknown(goal.reason)
            steps.append(goal)
            if goal.package:
                inherited = goal.package
        return steps[0] if len(steps) == 1 else GoalSpec("sequence", steps=tuple(steps))
    except (ValueError, TypeError, OverflowError) as exc:
        return _unknown(str(exc))
