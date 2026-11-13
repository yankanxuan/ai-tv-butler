"""Ephemeral V4 voice context; no inference, persistence, execution or controller.

All manager operations hold an RLock. Public sessions and returned dictionaries
are detached copies. Trusted integrations alone supply factual results: the
``source`` label is a required trust contract, not authentication of a caller.
Selection indices are one-based spoken ordinals. Neither a missing selection nor
a rejection chooses the first available result. Reads refresh the idle session
TTL; question deadlines are independent and are never refreshed by reads.
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


def _text(value, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    value = value.strip()
    return None if _UNSAFE.search(value) else value


def _code(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) and not _UNSAFE.search(value) else None


def _number(value, maximum=10000):
    return value if type(value) is int and 1 <= value <= maximum else None


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
    return {"id": identifier, "title": title, "year": _number(value.get("year"), 9999),
            "genres": safe_genres, "region": _region(value.get("region")),
            "season": _number(value.get("season")), "episode": _number(value.get("episode"))}


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
