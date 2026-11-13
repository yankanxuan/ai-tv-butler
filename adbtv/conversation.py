"""Ephemeral V4 voice context; no inference, persistence, execution or controller.

All manager operations hold an RLock. Public sessions and returned dictionaries
are detached copies. Trusted integrations alone supply factual results: the
``source`` label is a required trust contract, not authentication of a caller.
Selection indices are one-based spoken ordinals. Neither a missing selection nor
a rejection chooses the first available result. Reads refresh the idle session
TTL; question deadlines are independent and are never refreshed by reads.

V4.4 dialogue context (same trust rules) adds Chinese reference resolution
(``reference``), a stable preference view (``preferences``/``update_preferences``)
and an explicit focus on the last referenced candidate (``focus``/``note_played``).
Reference resolution is deterministic and offline: a spoken ordinal is mapped to
the candidate at that position, a demonstrative ("刚才那个"/"它") resolves to the
last candidate the user actually referred to, and a spoken title fragment is
matched only when it identifies exactly one candidate. Anything else returns
``None`` so the caller asks instead of silently reusing stale context.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import math
import re
import threading
import time
from typing import Callable, Optional
from uuid import uuid4


_MAX_RESULTS = 32
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_ID = re.compile(r"^[\w.-]{1,128}$", re.UNICODE)
_UNSAFE = re.compile(
    r"[/\\;|&$`<>\x00-\x1f\x7f]|(?:https?:|file:|bearer\s)|"
    r"\b(?:token|password|secret|authorization|api[_-]?key)\b\s*(?:[:=]|\s)\s*\S+|"
    r"\b(?:adb\s+shell|rm\s+-[rf]+|sh\s+-c|bash\s+-c|curl\s+|wget\s+)\b|"
    r"\b(?:sk-[\w-]{8,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", re.I)
_STATES = {"playing", "paused", "stopped", "buffering", "idle", "unknown"}
# Question kinds a shared snapshot may restore, so a one-word slot answer
# ("3", "第二集") still resolves after a cross-process hydrate.
_QUESTION_KINDS = {"selection", "episode", "confirmation", "clarify"}
_STATUSES = {"success", "error", "failed", "failure", "ok", "pending", "cancelled",
             "rejected", "unknown", "not_found", "timeout", "needs_clarification",
             "needs_confirmation", "partial", "unsupported", "native", "ignored"}
_REGIONS = {"国产": "中国大陆", "大陆": "中国大陆", "中国": "中国大陆",
            "内地": "中国大陆", "中国内地": "中国大陆", "mainland china": "中国大陆",
            "cn": "中国大陆", "china": "中国大陆"}

# ---------------------------------------------------------------------------
# V4.4 dialogue context: bounded reference resolution and stable preferences.
# ---------------------------------------------------------------------------
_MAX_REFERENCE_TEXT = 160
_MAX_GENRE_ITEMS = 16
_MAX_EXCLUDE_IDS = 32
# "不要太长的" narrows a work to this many episodes. It is a documented, fixed
# cap (never inferred from the user's library) so the preference view stays
# deterministic and exportable.
_SHORT_WORK_MAX_EPISODES = 40
_KIND_VALUES = ("any", "movie", "series", "short_drama", "unknown")
_KIND_LABELS = {"movie": "电影", "series": "电视剧", "short_drama": "短剧", "unknown": "未知"}
_PREFERENCE_KEYS = ("kind", "genres", "exclude_genres", "max_episodes",
                    "min_episodes", "mood", "exclude_ids", "last_query")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ORDINAL = re.compile(r"第\s*(?P<num>[0-9]{1,3}|[零〇一二两三四五六七八九十]{1,4})\s*"
                      r"(?P<unit>个|部|条|款|名|片|集|季)?")
_LAST_ORDINAL = re.compile(r"(?:最后|末)\s*(?:一)?\s*(?:个|部|条|款|名|片|集|季)?")
# "第二集/第二季" address an episode or season, never a candidate position.
_NON_WORK_UNITS = {"集", "季"}
_FOCUS_PHRASES = tuple(sorted({
    "刚才那个", "刚才那部", "刚才这部", "刚才的", "刚才说的", "刚才提的",
    "刚刚那个", "刚刚那部", "刚说的", "刚那个",
    "上一个", "上一部", "前一个", "前一部", "上面那个",
    "这个", "这部", "这部剧", "这个剧", "这本", "这一个", "这部片子", "这部片",
    "这片", "这片子", "这部戏",
    "那个", "那部", "那部剧", "那本", "那一部", "那部片子", "那部片",
    "那片", "那片子", "那部戏",
    "它",
}, key=len, reverse=True))

_PUNCT = re.compile(r"[\s\u3000，。！？、；：,.!?;:（）()【】\[\]《》<>\"'“”‘’…—~·]+")
_YEAR_TAG = re.compile(r"[（(]?(?:19|20)\d{2}[)）]?")
_DROP_CHARS = "的了吗呢吧啊呀嘛哦"
_QUERY_FILLERS = tuple(sorted({
    "给我推荐", "推荐一下", "推荐", "有没有", "类似的", "差不多", "这种", "这类",
    "好看", "看看", "看一下", "看下", "播放", "我想看", "我要看", "想看", "我想",
    "换一个", "换一部", "下一个", "再来一个", "不要太长", "太长了", "太长",
    "讲什么", "讲的是", "讲的啥", "剧情", "简介", "内容", "怎么样", "好不好",
    "这个", "那个", "这部", "那部", "它", "一下",
}, key=len, reverse=True))

_GENRE_WORDS = ("悬疑", "恐怖", "惊悚", "喜剧", "爱情", "动作", "科幻", "奇幻",
                "古装", "武侠", "仙侠", "都市", "家庭", "犯罪", "战争", "历史",
                "青春", "校园", "动画", "纪录", "冒险", "甜宠", "复仇", "逆袭",
                "脑洞", "真人秀", "综艺", "戏曲")
_NEGATION_WORDS = ("不要", "不想", "不看", "别", "排除", "去掉", "讨厌", "不喜欢",
                   "受不了", "怕", "拒绝", "除去")
_LONG_CUES = ("太长了", "太长", "集数太多", "太多集", "别太长", "不要长", "不要长的",
              "短一点", "短一些", "短的", "少一点", "集数少")
_SWAP_CUES = ("换一个", "换一部", "换一部剧", "换一个吧", "下一个", "再换",
              "不要这个", "不要这部", "换换")
_SIMILAR_CUES = ("类似的", "类似", "差不多", "同类型", "像这个", "这样的",
                 "这类", "这种类型", "同样类型")


def _text(value, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    value = value.strip()
    return None if _UNSAFE.search(value) else value


def _code(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) and not _UNSAFE.search(value) else None


def _number(value, maximum=10000):
    return value if type(value) is int and 1 <= value <= maximum else None


def _days(value):
    """Non-negative finite day count (library mtime fact), never a guess."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _kind(value):
    value = _code(value)
    return value if value in _KIND_VALUES else None


def _region(value):
    value = _text(value, 48)
    if not value or value.casefold() in {"unknown", "unknown_region", "n/a", "未知", "未知地区", "不详"}:
        return None
    return _REGIONS.get(value.casefold(), value)


def _result(value):
    """Only status and a machine reason code survive; errors are never copied."""
    if isinstance(value, str):
        value = {"status": value}
    if not isinstance(value, dict):
        return {}
    result = {}
    status = value.get("status")
    if isinstance(status, str) and status.lower() in _STATUSES:
        result["status"] = status
    reason = _code(value.get("reason_code"))
    if reason:
        result["reason_code"] = reason
    return result


def _candidate(value):
    if not isinstance(value, dict):
        raise ValueError("Each result must be a factual candidate dictionary")
    identifier, title = value.get("id"), _text(value.get("title"))
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or _UNSAFE.search(identifier) or not title:
        raise ValueError("Candidate requires a safe unique id and title")
    genres = value.get("genres", [])
    genres = genres if isinstance(genres, (list, tuple)) else []
    safe_genres = []
    for genre in genres[:16]:
        genre = _text(genre, 48)
        if genre and genre not in safe_genres:
            safe_genres.append(genre)
    kind = _kind(value.get("kind")) or "unknown"
    return {"id": identifier, "title": title, "year": _number(value.get("year"), 9999),
            "genres": safe_genres, "region": _region(value.get("region")),
            "season": _number(value.get("season")), "episode": _number(value.get("episode")),
            # V4.4 factual projection: kind evidence, episode count and NFO facts
            # that a follow-up ("第二部讲什么") can quote without re-reading files.
            "kind": kind, "kind_label": _KIND_LABELS[kind],
            "episodes": _number(value.get("episodes"), 10000),
            "studio": _text(value.get("studio"), 48),
            "runtime_minutes": _text(value.get("runtime_minutes"), 24),
            "added_days_ago": _days(value.get("added_days_ago"))}


_ASSISTANT_SCOPES = {"local", "online", "all"}
_ASSISTANT_KEYS = {"library_scope", "last_query", "last_tools", "last_status",
                   "preferences", "last_item", "last_intent"}


def _assistant_query(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    query = _text(value.get("query"), 60)
    if query:
        out["query"] = query
    genre = _text(value.get("genre"), 20)
    if genre:
        out["genre"] = genre
    kind = _code(value.get("kind"))
    if kind in ("any", "movie", "series", "unknown"):
        out["kind"] = kind
    year = _number(value.get("year"), 9999)
    if year:
        out["year"] = year
    episodes = _number(value.get("max_episodes"), 10000)
    if episodes:
        out["max_episodes"] = episodes
    return out


def _assistant_preferences(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    genres = value.get("genres")
    if isinstance(genres, (list, tuple)):
        cleaned = []
        for genre in genres[:8]:
            genre = _text(genre, 20)
            if genre and genre not in cleaned:
                cleaned.append(genre)
        if cleaned:
            out["genres"] = cleaned
    kind = _code(value.get("kind"))
    if kind in ("any", "movie", "series"):
        out["kind"] = kind
    episodes = _number(value.get("max_episodes"), 10000)
    if episodes:
        out["max_episodes"] = episodes
    return out


def _assistant_state(value):
    """Validate the bounded structured assistant context; unknown keys dropped."""
    if not isinstance(value, dict):
        return {}
    out = {}
    scope = _code(value.get("library_scope"))
    if scope in _ASSISTANT_SCOPES:
        out["library_scope"] = scope
    query = _assistant_query(value.get("last_query"))
    if query:
        out["last_query"] = query
    tools = value.get("last_tools")
    if isinstance(tools, (list, tuple)):
        cleaned = []
        for name in tools[:8]:
            name = _code(name)
            if name and name not in cleaned:
                cleaned.append(name)
        if cleaned:
            out["last_tools"] = cleaned
    status = _code(value.get("last_status"))
    if status:
        out["last_status"] = status
    preferences = _assistant_preferences(value.get("preferences"))
    if preferences:
        out["preferences"] = preferences
    item = value.get("last_item")
    if isinstance(item, dict):
        identifier = item.get("id")
        title = _text(item.get("title"))
        if (isinstance(identifier, str) and _ID.fullmatch(identifier)
                and not _UNSAFE.search(identifier) and title):
            out["last_item"] = {"id": identifier, "title": title}
    intent = _code(value.get("last_intent"))
    if intent:
        out["last_intent"] = intent
    return out


def _preference_query(value):
    """``last_query`` keeps the bounded structured query shape of V4.2."""
    if isinstance(value, str):
        value = {"query": value}
    return _assistant_query(value)


def _preferences_state(value):
    """Stable preference view with every contract key present and bounded."""
    base = {"kind": None, "genres": [], "exclude_genres": [], "max_episodes": None,
            "min_episodes": None, "mood": None, "exclude_ids": [], "last_query": {}}
    if not isinstance(value, dict):
        return base
    kind = _kind(value.get("kind"))
    if kind:
        base["kind"] = kind
    for key in ("genres", "exclude_genres"):
        raw = value.get(key)
        cleaned = []
        if isinstance(raw, (list, tuple)):
            for genre in raw[:_MAX_GENRE_ITEMS]:
                genre = _text(genre, 24)
                if genre and genre not in cleaned:
                    cleaned.append(genre)
        base[key] = cleaned
    for key in ("max_episodes", "min_episodes"):
        number = _number(value.get(key), 10000)
        if number:
            base[key] = number
    mood = _text(value.get("mood"), 24)
    if mood:
        base["mood"] = mood
    identifiers = []
    raw_ids = value.get("exclude_ids")
    if isinstance(raw_ids, (list, tuple)):
        for identifier in raw_ids:
            if (isinstance(identifier, str) and _ID.fullmatch(identifier)
                    and not _UNSAFE.search(identifier) and identifier not in identifiers):
                identifiers.append(identifier)
            if len(identifiers) >= _MAX_EXCLUDE_IDS:
                break
    base["exclude_ids"] = identifiers
    base["last_query"] = _preference_query(value.get("last_query"))
    return base


def _merge_preferences(current, delta):
    """Merge ``delta`` into ``current``; never overwrite a stricter constraint.

    ``exclude_ids``/``genres``/``exclude_genres`` grow by set union,
    ``max_episodes`` keeps the smaller cap and ``min_episodes`` the larger floor,
    so "不要恐怖" after "悬疑" narrows instead of replacing the earlier wish.
    """
    merged = _preferences_state(current)
    if not isinstance(delta, dict):
        return merged
    kind = _kind(delta.get("kind"))
    if kind:
        merged["kind"] = kind
    for key in ("genres", "exclude_genres"):
        raw = delta.get(key)
        if isinstance(raw, (list, tuple)):
            for genre in raw[:_MAX_GENRE_ITEMS]:
                genre = _text(genre, 24)
                if genre and genre not in merged[key]:
                    merged[key].append(genre)
            merged[key] = merged[key][:_MAX_GENRE_ITEMS]
    for key, stricter in (("max_episodes", min), ("min_episodes", max)):
        number = _number(delta.get(key), 10000)
        if number:
            current_value = merged[key]
            merged[key] = number if current_value is None else stricter(current_value, number)
    mood = _text(delta.get("mood"), 24)
    if mood:
        merged["mood"] = mood
    raw_ids = delta.get("exclude_ids")
    if isinstance(raw_ids, (list, tuple)):
        for identifier in raw_ids:
            if (isinstance(identifier, str) and _ID.fullmatch(identifier)
                    and not _UNSAFE.search(identifier)
                    and identifier not in merged["exclude_ids"]):
                merged["exclude_ids"].append(identifier)
        merged["exclude_ids"] = merged["exclude_ids"][:_MAX_EXCLUDE_IDS]
    if isinstance(delta.get("last_query"), (dict, str)):
        merged["last_query"] = _preference_query(delta["last_query"])
    return merged


def _focus_preferences(focus):
    """Preference delta inherited from the referenced work ("有没有类似的？")."""
    if not isinstance(focus, dict):
        return {}
    delta = {}
    kind = _kind(focus.get("kind"))
    if kind in ("movie", "series", "short_drama"):
        delta["kind"] = kind
    genres = focus.get("genres") or []
    if genres:
        delta["genres"] = list(genres)
    return delta


def _text_preferences(text, focus=None):
    """Deterministic cues only: negation, length, swap and similarity.

    Nothing here invents a fact: a genre is excluded only when it is named and
    negated in the same breath, and a length wish becomes the fixed documented
    cap rather than a value read out of the utterance.
    """
    delta = {}
    if not isinstance(text, str) or not text.strip():
        return delta
    text = text.strip()[:_MAX_REFERENCE_TEXT]
    wanted, excluded = [], []
    for genre in _GENRE_WORDS:
        start = 0
        while True:
            index = text.find(genre, start)
            if index < 0:
                break
            window = text[max(0, index - 8):index]
            if any(word in window for word in _NEGATION_WORDS):
                if genre not in excluded:
                    excluded.append(genre)
            elif genre not in wanted:
                wanted.append(genre)
            start = index + len(genre)
    if wanted:
        delta["genres"] = wanted
    if excluded:
        delta["exclude_genres"] = excluded
    if any(cue in text for cue in _LONG_CUES):
        delta["max_episodes"] = _SHORT_WORK_MAX_EPISODES
    if isinstance(focus, dict):
        if any(cue in text for cue in _SWAP_CUES):
            delta["exclude_ids"] = [focus["id"]]
        if any(cue in text for cue in _SIMILAR_CUES):
            delta.update(_focus_preferences(focus))
    return delta


def _cn_int(value):
    """Parse a small Chinese or ASCII ordinal number; None when unreadable."""
    if not isinstance(value, str) or not value:
        return None
    if value.isdigit():
        number = int(value)
        return number if 1 <= number <= 999 else None
    if "十" in value:
        head, _, tail = value.partition("十")
        if (head and (len(head) != 1 or head not in _CN_DIGITS)):
            return None
        if tail and (len(tail) != 1 or tail not in _CN_DIGITS):
            return None
        tens = _CN_DIGITS[head] if head else 1
        units = _CN_DIGITS[tail] if tail else 0
        number = tens * 10 + units
        return number if 1 <= number <= 999 else None
    if len(value) == 1 and value in _CN_DIGITS:
        number = _CN_DIGITS[value]
        return number or None
    return None


def _ordinal_reference(text):
    """Classify a spoken ordinal: "last", ("index", n) or "not_a_work"."""
    if _LAST_ORDINAL.search(text):
        return "last"
    match = _ORDINAL.search(text)
    if not match:
        return None
    if match.group("unit") in _NON_WORK_UNITS:
        return "not_a_work"
    number = _cn_int(match.group("num"))
    return ("index", number) if number else None


def _demonstrative(text):
    """True when the utterance points back at the last referenced candidate."""
    return any(phrase in text for phrase in _FOCUS_PHRASES)


def _normalize(value):
    if not isinstance(value, str):
        return ""
    text = _PUNCT.sub("", value)
    text = _YEAR_TAG.sub("", text)
    for char in _DROP_CHARS:
        text = text.replace(char, "")
    return text.casefold()


def _normalize_query(value):
    text = _normalize(value)
    for filler in _QUERY_FILLERS:
        text = text.replace(filler, "")
    return text


def _lcs_length(left, right):
    """Longest common substring length (bounded inputs, linear memory)."""
    if not left or not right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = [0] * (len(right) + 1)
    best = 0
    for left_char in left:
        current = [0] * (len(right) + 1)
        for index, right_char in enumerate(right, 1):
            if left_char == right_char:
                value = previous[index - 1] + 1
                current[index] = value
                if value > best:
                    best = value
        previous = current
    return best


def _match_title(text, candidates, containment_only=False):
    """Match a spoken title fragment; an ambiguous fragment matches nothing."""
    query = _normalize_query(text)
    if len(query) < 2:
        return None
    scored = []
    for candidate in candidates:
        title = _normalize(candidate.get("title"))
        if len(title) < 2:
            continue
        if title in query:
            score = len(title)
        elif containment_only:
            continue
        else:
            score = _lcs_length(query, title)
        if score >= 2:
            scored.append((score, candidate))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    winners = [candidate for score, candidate in scored if score == best]
    # Two different works share the spoken fragment: ask, never pick one.
    return winners[0] if len(winners) == 1 else None


@dataclass
class Session:
    """Public detached view; mutate state through ConversationManager methods."""
    session_id: str
    current_app: Optional[str] = None
    selected_content: Optional[dict] = None
    recent_results: list[dict] = field(default_factory=list)
    current_playback: Optional[dict] = None
    waiting_question: Optional[dict] = None
    # Cross-process, one-shot confirmation proposal (structured, never shell text).
    pending_action: Optional[dict] = None
    # Bounded factual playback history so "上一部/刚才那集" has a real referent.
    played_history: list[dict] = field(default_factory=list)
    last_result: Optional[dict] = None
    recent_turns: list[dict] = field(default_factory=list)
    # V4.2 structured assistant state (short-term only; bounded allowlist).
    assistant: dict = field(default_factory=dict)
    # V4.4 dialogue context: merged preference view and the work the user last
    # referred to ("刚才那个" must resolve to it, not to the first candidate).
    preferences: dict = field(default_factory=dict)
    focus_item: Optional[dict] = None


class ConversationManager:
    def __init__(self, ttl_seconds: float = 180, max_turns: int = 8,
                 clock: Callable[[], float] = time.monotonic):
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be finite and positive")
        if type(max_turns) is not int or not 1 <= max_turns <= 128:
            raise ValueError("max_turns must be between 1 and 128")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._expires: dict[str, float] = {}
        self._questions: dict[str, float] = {}

    def _session(self, tv_id):
        if not isinstance(tv_id, str) or not tv_id or len(tv_id) > 256:
            raise ValueError("tv_id must be a nonempty bounded string")
        now = self._clock()
        for key in list(self._expires):
            if now >= self._expires[key]:
                self.clear(key)
        if tv_id not in self._sessions:
            self._sessions[tv_id] = Session(session_id=uuid4().hex)
        self._expires[tv_id] = now + self.ttl_seconds
        session = self._sessions[tv_id]
        if tv_id in self._questions and now >= self._questions[tv_id]:
            session.waiting_question = None
            del self._questions[tv_id]
        return session

    def get(self, tv_id: str) -> Session:
        with self._lock:
            return deepcopy(self._session(tv_id))

    def clear(self, tv_id: str) -> None:
        with self._lock:
            self._sessions.pop(tv_id, None)
            self._expires.pop(tv_id, None)
            self._questions.pop(tv_id, None)

    def hydrate(self, tv_id: str, state: dict) -> bool:
        """Replace a session from a validated snapshot (shared-store boundary).

        Only allowlisted fields are restored; candidate validation is delegated to
        set_results-equivalent rules so an external file cannot inject free text.
        """
        if not isinstance(state, dict):
            raise ValueError("Hydration state must be a dictionary")
        with self._lock:
            # Idle deadline travels with the snapshot: an expired one must never
            # resurrect old context in another container or after a restart.
            deadline = state.get("expires_at")
            now = self._clock()
            expired = False
            if deadline is not None:
                try:
                    expired = now >= float(deadline)
                except (TypeError, ValueError):
                    expired = True          # unreadable deadline is not trustworthy
            if expired:
                self.clear(tv_id)
                return False
            session = self._session(tv_id)
            incoming_id = state.get("session_id")
            if isinstance(incoming_id, str) and re.fullmatch(r"[0-9a-f]{32}", incoming_id):
                # Both containers must expose the same session identity.
                session.session_id = incoming_id
            app = _code(state.get("current_app"))
            # Full replacement: nothing from the previous snapshot may survive…
            session.current_app = app
            # …except a validated pending question. The web/voice console shares
            # sessions through a JSON snapshot per turn; without restoring it, a
            # bare "3" after "请选择第几部" would lose the question and fall to the
            # model. Only a bounded kind and integer details are accepted.
            session.waiting_question = None
            self._questions.pop(tv_id, None)
            pending_question = state.get("waiting_question")
            if isinstance(pending_question, dict):
                kind = _code(pending_question.get("kind"))
                if kind in _QUESTION_KINDS:
                    details = {}
                    raw_details = pending_question.get("details")
                    if isinstance(raw_details, dict):
                        for key in ("count", "season", "episode"):
                            value = raw_details.get(key)
                            if type(value) is int and 0 <= value <= 10000:
                                details[key] = value
                    session.waiting_question = {"kind": kind,
                                                "prompt": "请选择明确的候选项",
                                                "details": details}
                    self._questions[tv_id] = now + min(60, self.ttl_seconds)
            results = []
            raw = state.get("recent_results") or []
            if isinstance(raw, list):
                for item in raw[:_MAX_RESULTS]:
                    try:
                        results.append(_candidate(item))
                    except ValueError:
                        continue
            session.recent_results = results
            selected = None
            if isinstance(state.get("selected_content"), dict):
                try:
                    selected = _candidate(state["selected_content"])
                except ValueError:
                    selected = None
            # Assign unconditionally: an absent/invalid selection clears the old one.
            session.selected_content = (selected
                                        if selected and any(r["id"] == selected["id"]
                                                            for r in results) else None)
            playback = state.get("current_playback")
            session.current_playback = None
            if isinstance(playback, dict):
                safe = {}
                title = _text(playback.get("title"))
                if title:
                    safe["title"] = title
                for key in ("season", "episode"):
                    value = _number(playback.get(key))
                    if value is not None:
                        safe[key] = value
                state_text = playback.get("state")
                if isinstance(state_text, str) and state_text.lower() in _STATES:
                    safe["state"] = state_text.lower()
                session.current_playback = safe or None
            try:
                session.pending_action = self._pending(state.get("pending_action"))
            except ValueError:
                session.pending_action = None
            history = []
            for item in (state.get("played_history") or [])[:4]:
                try:
                    history.append(_candidate(item))
                except ValueError:
                    continue
            session.played_history = history
            session.assistant = _assistant_state(state.get("assistant"))
            # V4.4: an older snapshot simply lacks these keys; defaults apply and
            # nothing is inferred from a missing field.
            session.preferences = _preferences_state(state.get("preferences"))
            session.focus_item = None
            raw_focus = state.get("focus_item")
            if isinstance(raw_focus, dict):
                try:
                    session.focus_item = _candidate(raw_focus)
                except ValueError:
                    session.focus_item = None
            if deadline is not None:
                try:
                    self._expires[tv_id] = float(deadline)
                except (TypeError, ValueError):
                    self._expires[tv_id] = now + self.ttl_seconds
            return True

    def export(self, tv_id: str) -> dict:
        """Snapshot for the shared file store, including the idle deadline."""
        with self._lock:
            self._session(tv_id)
            data = self.snapshot(tv_id)
            data["expires_at"] = self._expires.get(tv_id, self._clock() + self.ttl_seconds)
            return data

    @staticmethod
    def _pending(value):
        """Validate a pending confirmation; refuses anything shell-like."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("Pending action must be a dictionary")
        nonce = value.get("nonce")
        command = _text(value.get("command"), 400)
        intent = _code(value.get("intent")) or "unknown"
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise ValueError("Pending action needs a 32-hex nonce")
        if not command:
            raise ValueError("Pending action needs a canonical command")
        lowered = command.casefold()
        if any(bad in lowered for bad in ("adb", "shell", ";", "|", "&", "$", "`",
                                          ">", "<", "\n")):
            raise ValueError("Pending command must be a canonical media/app command")
        candidate_id = value.get("candidate_id")
        if candidate_id is not None and (not isinstance(candidate_id, str)
                                         or not _ID.fullmatch(candidate_id)
                                         or _UNSAFE.search(candidate_id)):
            raise ValueError("Invalid pending candidate")
        try:
            created = float(value.get("created", 0))
            expires = float(value.get("expires", 0))
        except (TypeError, ValueError):
            raise ValueError("Pending action needs numeric timestamps")
        if not (0 < created <= expires):
            raise ValueError("Pending action lifetime is invalid")
        validated = {"nonce": nonce, "command": command, "intent": intent,
                     "candidate_id": candidate_id, "created": created, "expires": expires}
        from .schemas import SchemaError, validate_pending
        try:
            return validate_pending(validated)
        except SchemaError as exc:
            raise ValueError(str(exc)) from exc

    def set_pending(self, tv_id: str, *, nonce: str, command: str, intent: str = "unknown",
                    candidate_id=None, ttl_seconds: float = 60) -> dict:
        """Store a one-shot confirmation; replaces any previous proposal."""
        now = self._clock()
        pending = self._pending({"nonce": nonce, "command": command, "intent": intent,
                                 "candidate_id": candidate_id, "created": now,
                                 "expires": now + min(ttl_seconds, self.ttl_seconds)})
        with self._lock:
            self._session(tv_id).pending_action = pending
            return deepcopy(pending)

    def take_pending(self, tv_id: str) -> Optional[dict]:
        """Consume the pending action exactly once; expired proposals are dropped."""
        with self._lock:
            session = self._session(tv_id)
            pending = session.pending_action
            session.pending_action = None
            if not pending:
                return None
            if self._clock() >= float(pending.get("expires", 0)):
                return None
            return deepcopy(pending)

    def clear_pending(self, tv_id: str) -> None:
        with self._lock:
            if tv_id in self._sessions:
                self._sessions[tv_id].pending_action = None

    def snapshot(self, tv_id: str) -> dict:
        """Bounded model context; excludes TV identifiers, clocks and raw data."""
        with self._lock:
            return asdict(self._session(tv_id))

    def set_results(self, tv_id: str, results: list[dict], source: str = "library") -> list[dict]:
        """Replace candidates atomically, keeping at most 32 in caller order.

        Validate all supplied IDs/titles, including overflow, before truncation.
        Only ``library`` and ``observed_ui`` provenance are permitted. Unknown
        fields (including paths, prompts, commands and tokens) are discarded.
        """
        if source not in ("library", "observed_ui"):
            raise ValueError("Results require library or observed_ui provenance")
        if not isinstance(results, (list, tuple)):
            raise ValueError("results must be a list of factual candidates")
        candidates, seen = [], set()
        for item in results:
            candidate = _candidate(item)
            if candidate["id"] in seen:
                raise ValueError("Candidate ids must be unique")
            seen.add(candidate["id"])
            if len(candidates) < _MAX_RESULTS:
                candidates.append(candidate)
        with self._lock:
            session = self._session(tv_id)
            session.recent_results = candidates
            session.selected_content = None
            session.waiting_question = None
            self._questions.pop(tv_id, None)
            return deepcopy(candidates)

    def select(self, tv_id: str, index=None, candidate_id=None) -> Optional[dict]:
        """Select explicitly by ONE-based index OR id; invalid input returns None."""
        with self._lock:
            session = self._session(tv_id)
            candidate = None
            if index is not None and candidate_id is None:
                if type(index) is int and 1 <= index <= len(session.recent_results):
                    candidate = session.recent_results[index - 1]
            elif candidate_id is not None and index is None:
                candidate = next((r for r in session.recent_results if r["id"] == candidate_id), None)
            if candidate is None:
                return None
            session.selected_content = deepcopy(candidate)
            # An explicit selection is also the referent of a later "它/刚才那个".
            session.focus_item = deepcopy(candidate)
            session.waiting_question = None
            self._questions.pop(tv_id, None)
            return deepcopy(candidate)

    def reject_selected(self, tv_id: str) -> list[dict]:
        """Remove the selection and return remaining candidates; never play/select."""
        with self._lock:
            session = self._session(tv_id)
            if session.selected_content:
                identifier = session.selected_content["id"]
                session.recent_results = [r for r in session.recent_results if r["id"] != identifier]
                if session.focus_item and session.focus_item["id"] == identifier:
                    # A rejected work is no longer a valid referent.
                    session.focus_item = None
            session.selected_content = None
            session.waiting_question = None
            self._questions.pop(tv_id, None)
            return deepcopy(session.recent_results)

    def refine(self, tv_id: str, genre=None, region=None) -> list[dict]:
        """Narrow current results only by confirmed metadata, never title guesses.

        A small explicit mainland-China alias table supports ``国产``. Unknown
        regions compare literally; missing metadata cannot satisfy any filter.
        """
        with self._lock:
            session = self._session(tv_id)
            wanted_genre = _text(genre, 48) if genre is not None else None
            wanted_region = _region(region) if region is not None else None
            results = [r for r in session.recent_results
                       if (genre is None or (wanted_genre and wanted_genre in r["genres"]))
                       and (region is None or (wanted_region and wanted_region == r["region"]))]
            session.recent_results = results
            session.selected_content = None
            session.waiting_question = None
            self._questions.pop(tv_id, None)
            return deepcopy(results)

    def ask(self, tv_id: str, kind: str, prompt: str, details=None, *, ttl_seconds=None) -> dict:
        """Record pending selection; prompt text is never retained.

        ``prompt`` belongs to the caller's response layer. Stored prompt is a
        fixed label; details allow only integer count/season/episode metadata.
        Question lifetime defaults to min(60, session TTL), independently of
        session access, and cannot exceed the session TTL.
        """
        kind = _code(kind)
        if not kind:
            raise ValueError("Question kind must be a bounded machine code")
        ttl = min(60, self.ttl_seconds) if ttl_seconds is None else ttl_seconds
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Question TTL must be finite and positive")
        safe_details = {}
        if isinstance(details, dict):
            for key in ("count", "season", "episode"):
                value = details.get(key)
                if type(value) is int and 0 <= value <= 10000:
                    safe_details[key] = value
        with self._lock:
            session = self._session(tv_id)
            session.waiting_question = {"kind": kind, "prompt": "请选择明确的候选项", "details": safe_details}
            self._questions[tv_id] = self._clock() + min(ttl, self.ttl_seconds)
            return deepcopy(session.waiting_question)

    def resolve(self, tv_id: str, index=None, candidate_id=None) -> Optional[dict]:
        """Resolve a live question with explicit candidate selection only."""
        with self._lock:
            if self._session(tv_id).waiting_question is None:
                return None
            return self.select(tv_id, index=index, candidate_id=candidate_id)

    def remember_turn(self, tv_id: str, route, intent, outcome) -> dict:
        """Keep bounded machine route/intent/status metadata, never utterances."""
        summary = {"route": _code(route) or "unknown", "intent": _code(intent) or "unknown",
                   "outcome": _result(outcome)}
        with self._lock:
            session = self._session(tv_id)
            session.recent_turns.append(summary)
            del session.recent_turns[:-self.max_turns]
            return deepcopy(summary)

    def update_observation(self, tv_id: str, app=None, playback=None) -> None:
        """Replace app/playback observation with allowlisted factual metadata."""
        app = _code(app)
        safe_playback = {}
        if isinstance(playback, dict):
            title = _text(playback.get("title"))
            if title:
                safe_playback["title"] = title
            for key in ("season", "episode"):
                value = _number(playback.get(key))
                if value is not None:
                    safe_playback[key] = value
            state = playback.get("state")
            if isinstance(state, str) and state.lower() in _STATES:
                safe_playback["state"] = state.lower()
        with self._lock:
            session = self._session(tv_id)
            # Full replacement: nothing from the previous snapshot may survive.
            session.current_app = app
            session.waiting_question = None
            self._questions.pop(tv_id, None)
            session.current_playback = safe_playback or None

    def remember_play(self, tv_id: str, candidate: dict) -> list[dict]:
        """Record a factually played item (bounded, deduplicated by id)."""
        with self._lock:
            session = self._session(tv_id)
            safe = _candidate(candidate)
            session.played_history = [safe] + [
                item for item in session.played_history if item.get("id") != safe["id"]][:4]
            return deepcopy(session.played_history)

    def update_result(self, tv_id: str, result) -> dict:
        with self._lock:
            session = self._session(tv_id)
            session.last_result = _result(result)
            return deepcopy(session.last_result)

    def update_assistant(self, tv_id: str, **fields) -> dict:
        """Merge validated structured assistant state (short-term only).

        Only allowlisted keys survive; unrecognized fields are ignored rather
        than stored, so a caller cannot smuggle free text into the context.
        """
        unknown = set(fields) - _ASSISTANT_KEYS
        if unknown:
            raise ValueError("unknown assistant state field: " + ",".join(sorted(unknown)))
        with self._lock:
            session = self._session(tv_id)
            merged = dict(session.assistant)
            for key, value in fields.items():
                validated = _assistant_state({key: value})
                if key in validated:
                    merged[key] = validated[key]
                else:
                    merged.pop(key, None)
            session.assistant = merged
            return deepcopy(merged)

    def assistant_state(self, tv_id: str) -> dict:
        with self._lock:
            return deepcopy(self._session(tv_id).assistant)

    # -- V4.4 dialogue context ------------------------------------------------
    def set_candidates(self, tv_id: str, rows: list[dict], source: str = "library") -> list[dict]:
        """Alias of ``set_results``: replace the candidate list atomically.

        The focus survives a recomputed candidate list, because "就看刚才那个"
        must restore the work the user actually discussed even when a new
        preference changed the rows. It is only dropped by expiry, ``clear``,
        rejection or a newer reference.
        """
        return self.set_results(tv_id, rows, source=source)

    def _refer(self, session: Session, candidate):
        """Record ``candidate`` as the focus and return a detached copy."""
        if candidate is None:
            return None
        item = deepcopy(candidate)
        session.focus_item = item
        if not any(row["id"] == item["id"] for row in session.recent_results):
            # Keep the referenced work addressable for a follow-up selection
            # without shifting one-based positions (append, never prepend).
            if len(session.recent_results) < _MAX_RESULTS:
                session.recent_results.append(deepcopy(item))
        return deepcopy(item)

    def focus_on(self, tv_id: str, candidate_id: str) -> Optional[dict]:
        """Make an already-shown candidate the conversational focus.

        Used when the assistant answers about a work ("第二个讲什么"): the next
        "就看刚才那个" must resolve to *that* work, without re-selecting it.
        """
        if not isinstance(candidate_id, str) or not candidate_id:
            return None
        with self._lock:
            session = self._session(tv_id)
            candidate = next((row for row in session.recent_results
                              if row["id"] == candidate_id), None)
            if candidate is None:
                return None
            return self._refer(session, candidate)

    def reference(self, tv_id: str, text: str) -> Optional[dict]:
        """Resolve a spoken reference to one candidate, or return None.

        Ordinals ("第二个"/"第2部"/"最后一个") address the current candidate list
        by one-based position and never clamp to the nearest edge. Demonstratives
        ("刚才那个"/"上一个"/"这个"/"它"/"那部"/"这片") resolve to the work the
        user last referred to. A spoken title fragment resolves only when exactly
        one candidate matches it. A failed resolution leaves the focus untouched,
        so a topic switch can never silently reuse stale context.
        """
        if not isinstance(text, str):
            return None
        text = text.strip()[:_MAX_REFERENCE_TEXT]
        if not text:
            return None
        with self._lock:
            session = self._session(tv_id)
            ordinal = _ordinal_reference(text)
            if ordinal == "not_a_work":
                # "第二集/第二季" is an episode or season, not a work position.
                return None
            if ordinal == "last":
                if not session.recent_results:
                    return None
                return self._refer(session, session.recent_results[-1])
            if isinstance(ordinal, tuple):
                index = ordinal[1]
                if not index or index > len(session.recent_results):
                    return None
                return self._refer(session, session.recent_results[index - 1])
            # A full title beats a pronoun: "这个杀手不太冷" names a work.
            match = _match_title(text, session.recent_results, containment_only=True)
            if match is None and _demonstrative(text):
                return self._refer(session, session.focus_item)
            if match is None:
                match = _match_title(text, session.recent_results)
            return self._refer(session, match)

    def preferences(self, tv_id: str) -> dict:
        """Stable preference view; every contract key is always present."""
        with self._lock:
            return _preferences_state(self._session(tv_id).preferences)

    def update_preferences(self, tv_id: str, delta) -> dict:
        """Merge a preference delta; a stricter earlier wish is never relaxed.

        ``delta`` is a mapping of the stable preference keys, optionally carrying
        a spoken ``text``/``utterance`` whose explicit cues ("不要恐怖的",
        "太长了", "换一个", "有没有类似的") are parsed deterministically, plus the
        flags ``similar_to_focus``/``inherit_focus`` and ``exclude_focus``. A bare
        string is treated as spoken text. Unknown keys are dropped, not stored.
        """
        with self._lock:
            session = self._session(tv_id)
            merged = _preferences_state(session.preferences)
            text = None
            if isinstance(delta, str):
                text = delta
            elif isinstance(delta, dict):
                spoken = delta.get("text", delta.get("utterance"))
                text = spoken if isinstance(spoken, str) else None
                # Only the stable preference keys are mergeable; a caller cannot
                # smuggle free text or paths into the context through a delta.
                merged = _merge_preferences(
                    merged, {key: value for key, value in delta.items()
                             if key in _PREFERENCE_KEYS})
                if delta.get("similar_to_focus") or delta.get("inherit_focus"):
                    merged = _merge_preferences(merged, _focus_preferences(session.focus_item))
                if delta.get("exclude_focus") and isinstance(session.focus_item, dict):
                    merged = _merge_preferences(merged, {"exclude_ids": [session.focus_item["id"]]})
            if text:
                merged = _merge_preferences(merged, _text_preferences(text, session.focus_item))
            session.preferences = merged
            return deepcopy(merged)

    def note_played(self, tv_id: str, item_id: str, season=None, episode=None) -> None:
        """Record a session-local play of a work already known to this session.

        An id that never appeared as a candidate, focus or earlier play is a
        no-op: a played-history entry is never fabricated from an id alone.
        """
        if not isinstance(item_id, str) or not _ID.fullmatch(item_id) or _UNSAFE.search(item_id):
            raise ValueError("item_id must be a safe identifier")
        with self._lock:
            session = self._session(tv_id)
            pools = [session.recent_results]
            if isinstance(session.focus_item, dict):
                pools.append([session.focus_item])
            pools.append(session.played_history)
            base = None
            for pool in pools:
                base = next((item for item in pool if item.get("id") == item_id), None)
                if base is not None:
                    break
            if base is None:
                return None
            item = deepcopy(base)
            for key, value in (("season", season), ("episode", episode)):
                number = _number(value)
                if number is not None:
                    item[key] = number
            session.played_history = [item] + [
                row for row in session.played_history if row.get("id") != item_id][:4]
            return None

    def focus(self, tv_id: str) -> Optional[dict]:
        """The candidate the user last referred to, or None."""
        with self._lock:
            item = self._session(tv_id).focus_item
            return deepcopy(item) if isinstance(item, dict) else None
