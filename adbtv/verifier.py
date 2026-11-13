"""Conservative goal verification using observations, never action return codes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import math
import re
from typing import Any

from .perception import ScreenState


class VerificationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GoalSpec:
    action: str
    package: str = ""
    title: str = ""
    season: int | None = None
    episode: int | None = None
    require_progress: bool = False
    query: str = ""
    seconds: float | None = None
    delta: int = 1
    level: int | None = None          # 绝对音量目标（volume_set）
    policy: str = "resume"
    steps: tuple["GoalSpec", ...] = ()
    reason: str = ""
    media_path: str = ""
    # Content evidence produced by a trusted navigation step (e.g. the episode
    # label actually clicked). Only the engine may fill this, never the model.
    confirmed_content: dict = field(default_factory=dict, compare=False)


@dataclass
class VerificationResult:
    status: VerificationStatus
    confidence: float
    conditions: dict[str, bool | None] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @property
    def ok(self) -> bool:
        return self.status == VerificationStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


def _normal(text: Any) -> str:
    return re.sub(r"[\s《》\"“”]", "", str(text or "")).casefold()


def _number(value: Any) -> float | None:
    # A bool, inferred clock, or string like 'playing' is not numeric progress.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _result(conditions: dict[str, bool | None], evidence=None) -> VerificationResult:
    values = list(conditions.values())
    if any(value is False for value in values):
        status, confidence = VerificationStatus.FAILED, .95
    elif values and all(value is True for value in values):
        status, confidence = VerificationStatus.SUCCESS, .95
    elif any(value is True for value in values):
        status, confidence = VerificationStatus.PARTIAL, .55
    else:
        status, confidence = VerificationStatus.UNKNOWN, .2
    details = "; ".join(f"{key}: {'confirmed' if value is True else 'not met' if value is False else 'unknown'}"
                        for key, value in conditions.items())
    return VerificationResult(status, confidence, conditions, evidence or {}, details)


class Verifier:
    """Pure verifier; no device access, LLM calls, sleeps, or key commands.

    Integration: ``verify(GoalSpec(...), before, after)``. Each component method
    has the same three arguments (before may be None except for progress).
    Media metadata belongs in ``ScreenState.playback['metadata']`` and must
    originate in an actual media observation, not an LLM-selected candidate.
    """

    @staticmethod
    def _owned(goal: GoalSpec, state: ScreenState) -> bool:
        owner = state.playback.get("package")
        return not (owner and owner != (goal.package or state.app))

    def verify_application(self, goal: GoalSpec, before: ScreenState | None,
                           after: ScreenState) -> VerificationResult:
        condition = ((after.app == goal.package) if after.app else None) if goal.package else None
        return _result({"application": condition}, {"foreground_package": after.app,
                                                   "expected_package": goal.package})

    def verify_content(self, goal: GoalSpec, before: ScreenState | None,
                       after: ScreenState) -> VerificationResult:
        conditions: dict[str, bool | None] = {}
        evidence: dict[str, Any] = {}
        playback = after.playback if self._owned(goal, after) else {}
        metadata = playback.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        # Never use state.vision, candidate/model_title/requested_title fields,
        # including candidates explicitly tagged inside a caller's metadata.
        candidate_sources = {"llm", "model", "vision", "candidate", "inferred"}
        unverified = (metadata.get("verified") is False or
                      str(metadata.get("source", "")).lower() in candidate_sources or
                      playback.get("content_verified") is False or
                      str(playback.get("content_source", "")).lower() in candidate_sources)
        if unverified:
            metadata, playback = {}, {}
        actual_title = metadata.get("title") or playback.get("media_title") or playback.get("title")
        # A title in a browse/search list doesn't identify the playing content.
        player_page = bool(re.search(r"player|playback|videoactivity", after.activity, re.I))
        texts = after.texts if player_page else []
        confirmed = getattr(goal, "confirmed_content", None) or {}
        if goal.title:
            if actual_title:
                conditions["title"] = _normal(actual_title) == _normal(goal.title)
                evidence["observed_title"] = actual_title
                evidence["title_source"] = "media_metadata"
            else:
                exact = next((text for text in texts if _normal(text) == _normal(goal.title)), None)
                conditions["title"] = True if exact else None
                if exact:
                    evidence.update(observed_title=exact, title_source="player_xml")
        # Navigation evidence: the engine clicked a label that names the episode.
        # This is a real observation from the app UI, not a model guess.
        if confirmed:
            # Navigation labels are real app observations, so they take
            # precedence over an unreadable (None) player UI.
            if "title" in confirmed and conditions.get("title") is not True:
                conditions["title"] = _normal(confirmed["title"]) == _normal(goal.title)
                evidence["observed_title"] = confirmed["title"]
                evidence["title_source"] = "navigation_label"
            for key in ("season", "episode"):
                expected = goal.__dict__.get(key)
                if expected is None or key not in confirmed:
                    continue
                if conditions.get(key) is not True:
                    conditions[key] = confirmed[key] == expected
                    evidence[f"observed_{key}"] = confirmed[key]
                    evidence[f"{key}_source"] = "navigation_label"
        for name, expected in (("season", goal.season), ("episode", goal.episode)):
            if expected is None:
                continue
            actual = metadata.get(name, playback.get(name))
            if _number(actual) is not None:
                conditions[name] = actual == expected
                evidence[f"observed_{name}"] = actual
            else:
                # Only a player page has current-content semantics. Numeric
                # boundaries prevent episode 1 matching episode 10.
                chinese = "季" if name == "season" else "集"
                latin = "S" if name == "season" else "E"
                pattern = rf"第\s*0*{expected}\s*{chinese}|(?<![A-Za-z]){latin}0*{expected}(?!\d)"
                if name == "episode":
                    pattern = rf"第\s*0*{expected}\s*集|(?:S\d+)?E0*{expected}(?!\d)"
                match = next((text for text in texts if re.search(pattern, text, re.I)), None)
                conditions[name] = True if match else None
                if match:
                    evidence[f"observed_{name}"] = match
        return _result(conditions or {"content": None}, evidence)

    def verify_playback(self, goal: GoalSpec, before: ScreenState | None,
                        after: ScreenState) -> VerificationResult:
        playback = after.playback
        evidence = {"playback": playback}
        if not self._owned(goal, after):
            return _result({"playback": False}, evidence)
        state = str(playback.get("state", "")).upper()
        details = playback.get("evidence", {})
        details = details if isinstance(details, dict) else {}
        action = goal.action.casefold()
        if action in ("pause", "paused"):
            condition = True if state == "PAUSED" else False if state in ("PLAYING", "STOPPED") else None
        elif action in ("stop", "stopped"):
            condition = True if state in ("STOPPED", "NONE") else False if state in ("PLAYING", "PAUSED") else None
        elif state in ("PAUSED", "STOPPED", "NONE", "ERROR") and not details.get("media_session_playing"):
            condition = False
        elif details.get("media_session_playing") is True:
            condition = True
        elif details.get("audio_track_started") and not details.get("ui_playing"):
            # A goal without title/episode constraints is satisfied by the
            # foreground player plus its own active audio. For named content,
            # audio is supporting evidence only: it may be an advert or another
            # track and cannot establish title identity.
            named = bool(goal.title or goal.season is not None or goal.episode is not None)
            identity = self.verify_content(goal, before, after) if named else None
            identity_confirmed = not named or identity.ok
            condition = True if identity_confirmed and details.get("activity_is_player") else None
        elif state == "PLAYING" and playback.get("playing") is True:
            condition = True
        else:
            condition = None
        return _result({"playback": condition}, evidence)

    @staticmethod
    def verify_home(goal: GoalSpec, before: ScreenState | None,
                    after: ScreenState) -> VerificationResult:
        home = (after.system or {}).get("home_package")
        activity = (after.activity or "").lower()
        looks_launcher = any(word in activity for word in ("launcher", "tvhome", "/.mainactivity")) and \
            any(word in (after.app or "").lower() for word in ("home", "launcher"))
        if home and after.app:
            return _result({"home": after.app == home}, {"home_package": home, "foreground": after.app})
        if looks_launcher:
            return _result({"home": True}, {"foreground": after.app, "activity": after.activity})
        return _result({"home": None}, {"home_package": home, "foreground": after.app})

    @staticmethod
    def verify_volume(goal: GoalSpec, before: ScreenState | None,
                      after: ScreenState) -> VerificationResult:
        def level(state):
            value = (state.system or {}).get("volume") if state is not None else None
            return value if isinstance(value, dict) and value.get("known") else None
        old, new = level(before), level(after)
        if not new:
            return _result({"volume": None}, {"before": old, "after": new})
        if goal.action == "volume_set":
            if goal.level is None:
                return _result({"volume": None}, {"after": new})
            # 设备会把超出范围的目标夹紧到 min/max，因此比较"期望值"而非原始请求。
            expected = max(new["min"], min(goal.level, new["max"]))
            return _result({"volume": new["index"] == expected},
                           {"expected": expected, "after": new})
        if not old:
            return _result({"volume": None}, {"before": old, "after": new})
        changed = new["index"] != old["index"]
        direction_ok = (new["index"] > old["index"]) if goal.action == "volume_up" else (new["index"] < old["index"])
        at_boundary = ((goal.action == "volume_up" and old["index"] >= old["max"]) or
                       (goal.action == "volume_down" and old["index"] <= old["min"]))
        return _result({"volume": True if (direction_ok or at_boundary) else False if changed else None},
                       {"before": old, "after": new, "at_boundary": at_boundary})

    @staticmethod
    def verify_search(goal: GoalSpec, before: ScreenState | None,
                      after: ScreenState) -> VerificationResult:
        needle = _normal(goal.query or goal.title)
        if not needle:
            return _result({"search": None}, {"query": goal.query})
        # Search-only completion: the query is present in the current screen's
        # text (search box or result list). Not proof that any item plays.
        hit = any(needle in _normal(text) for text in after.texts)
        return _result({"search": True if hit else None},
                       {"query": goal.query, "texts": after.texts[:20]})

    def verify_progress(self, goal: GoalSpec, before: ScreenState | None,
                        after: ScreenState) -> VerificationResult:
        if before is None or not self._owned(goal, before) or not self._owned(goal, after):
            return _result({"progress": None})
        if not before.app or before.app != after.app:
            return _result({"progress": None}, {"reason": "different or unknown application"})
        # Switching tracks can increase a position without proving advancement
        # of the same content. Reject conflicting observed identity.
        for name in ("title", "media_title", "media_id", "season", "episode"):
            old = before.playback.get("metadata", {}) or {}
            new = after.playback.get("metadata", {}) or {}
            a = old.get(name, before.playback.get(name)) if isinstance(old, dict) else None
            b = new.get(name, after.playback.get(name)) if isinstance(new, dict) else None
            if a is not None and b is not None and a != b:
                return _result({"progress": None}, {"reason": "content identity changed"})
        if after.timestamp <= before.timestamp:
            return _result({"progress": None}, {"reason": "nonsequential observations"})
        for key in ("position_ms", "position_seconds", "position", "progress_seconds", "progress"):
            old = _number(before.playback.get(key))
            new = _number(after.playback.get(key))
            if old is not None and new is not None:
                return _result({"progress": new > old},
                               {"progress_key": key, "before": old, "after": new,
                                "delta": new - old})
        return _result({"progress": None}, {"reason": "no observed numeric position pair"})

    def verify(self, goal: GoalSpec, before: ScreenState | None,
               after: ScreenState) -> VerificationResult:
        parts = []
        if goal.package:
            parts.append(self.verify_application(goal, before, after))
        if goal.title or goal.season is not None or goal.episode is not None:
            parts.append(self.verify_content(goal, before, after))
        action = goal.action.casefold()
        if action in ("play", "resume", "continue", "playback", "pause", "paused", "stop", "stopped"):
            parts.append(self.verify_playback(goal, before, after))
        if goal.require_progress:
            parts.append(self.verify_progress(goal, before, after))
        if action == "home":
            parts.append(self.verify_home(goal, before, after))
        if action in ("volume_up", "volume_down", "volume_set"):
            parts.append(self.verify_volume(goal, before, after))
        if action == "search":
            parts.append(self.verify_search(goal, before, after))
        # Input/key/tap/search command success does not establish semantic task
        # completion, even if the foreground package happens to be correct.
        known_actions = {"open", "launch", "application", "play", "resume", "continue",
                         "playback", "pause", "paused", "stop", "stopped", "home",
                         "volume_up", "volume_down", "volume_set", "search"}
        if action not in known_actions:
            parts.append(_result({"action_outcome": None}))
        conditions, evidence = {}, {}
        for part in parts:
            conditions.update(part.conditions)
            evidence.update(part.evidence)
        evidence["observation_errors"] = list(after.observation_errors)
        return _result(conditions or {"goal": None}, evidence)


def verify(goal: GoalSpec, before: ScreenState | None, after: ScreenState) -> VerificationResult:
    """Stateless convenience entry point."""
    return Verifier().verify(goal, before, after)
