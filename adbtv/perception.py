"""Offline-friendly observation facade for Agent integrations.

``observe`` returns XML/system evidence. Vision is opt-in and its output is kept
as unverified candidates, never silently promoted into observed XML text.
Call ``after_action`` after actions performed outside this facade.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Callable

from .device import TVDevice, UIElement


@dataclass
class ScreenState:
    app: str = ""
    activity: str = ""
    focused: UIElement | None = None
    elements: list[UIElement] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    playback: dict[str, Any] = field(default_factory=dict)
    screenshot_hash: str = ""
    screenshot_path: str = ""
    # not_requested | unknown | black | available | unavailable. PNG decoding is
    # intentionally not claimed here: bytes alone cannot establish a black frame.
    screenshot_status: str = "not_requested"
    page_fingerprint: str = ""
    changed: bool = False
    observation_errors: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    vision: dict[str, Any] = field(default_factory=dict)
    xml_available: bool = False
    system: dict[str, Any] = field(default_factory=dict)

    @property
    def package(self) -> str:
        return self.app

    @property
    def errors(self) -> list[str]:
        return self.observation_errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Perception:
    """Read-only observation plus explicitly requested, freshly located clicks.

    Existing TVDevice/LLMClient objects are accepted unchanged. ``include_image``
    captures in memory; no screenshot is persisted. ``allow_vision`` only invokes
    a configured vision client when XML has no readable text. Path-only clients
    receive a private temporary file which is removed even on failure.
    """

    def __init__(self, device, llm=None, cfg=None, *, cache_ttl: float = .3,
                 vision_cache_size: int = 16):
        self.device = device
        self.llm = llm
        self.cfg = cfg or getattr(device, "cfg", {}) or {}
        self.cache_ttl = max(0., float(cache_ttl))
        self.vision_cache_size = max(0, int(vision_cache_size))
        self._cache: dict[tuple[bool, bool, bool], tuple[float, ScreenState]] = {}
        self._vision_cache: OrderedDict[tuple[str, str], tuple[dict, str]] = OrderedDict()
        self._last_fingerprint = ""
        self._generation = 0
        self._issued: OrderedDict[int, tuple[int, str, UIElement]] = OrderedDict()

    def invalidate(self) -> None:
        """Expire observations and element leases after any action."""
        self._cache.clear()
        self._generation += 1
        # Retain bounded element provenance across actions so old references
        # cannot accidentally rebind to an identically labelled different app.

    after_action = invalidate

    def observe(self, *, fresh: bool = False, include_playback: bool = True,
                include_image: bool = False, allow_vision: bool = False,
                include_ui: bool = True) -> ScreenState:
        key = (include_playback, include_image, allow_vision, include_ui)
        cached = self._cache.get(key)
        if not fresh and cached and time.monotonic() - cached[0] < self.cache_ttl:
            return copy.deepcopy(cached[1])
        state = ScreenState()
        focus = ""
        try:
            focus = self.device.current_focus()
            match = re.search(r"([a-zA-Z][\w.]+)/([\w.$]+)", focus or "")
            if match:
                state.app, state.activity = match.groups()
            else:
                state.activity = focus or ""
        except Exception as exc:
            state.observation_errors.append(f"system: {exc}")
        if include_ui:
            try:
                capture = getattr(self.device, "capture_ui", None) or self.device.elements
                state.elements = list(capture())
                state.xml_available = True
                state.texts = list(dict.fromkeys(t for e in state.elements
                                                for t in (e.text, e.desc) if t))
                state.focused = next((e for e in state.elements if e.focused), None)
            except Exception as exc:
                state.observation_errors.append(f"xml: {exc}")
        if include_playback:
            try:
                if isinstance(self.device, TVDevice):
                    state.playback = self.device.playback_status(
                        include_ui=False, expected_package=state.app, focus=focus,
                        include_raw_media=True)
                else:
                    state.playback = self.device.playback_status(
                        include_ui=False, expected_package=state.app)
                raw_media = state.playback.pop("_raw_media_session", None)
                # XML may fail; independent system observations remain useful.
                if not state.app:
                    state.app = state.playback.get("package", "")
                if not state.activity:
                    state.activity = state.playback.get("activity", "")
                self._enrich_media(state, raw=raw_media)
            except Exception as exc:
                state.observation_errors.append(f"playback: {exc}")
        structure = [(e.node_id, e.parent_id, e.resource_id, e.text, e.desc,
                      e.bounds, e.focused, e.clickable, e.focusable)
                     for e in state.elements]
        xml_fingerprint = self._hash([state.app, state.activity, structure])
        needs_vision = bool(allow_vision and not state.texts and self.llm is not None)
        image = b""
        if include_image or needs_vision:
            try:
                image = self.device.adb.screenshot_bytes()
                if not isinstance(image, bytes) or not image:
                    raise ValueError("empty or invalid screenshot bytes")
                state.screenshot_hash = hashlib.sha256(image).hexdigest()
                # Explicit unknown: no image decoder is used and DRM blackness
                # must never be guessed from compressed byte count.
                state.screenshot_status = "unknown"
            except Exception as exc:
                state.screenshot_status = "unavailable"
                state.observation_errors.append(f"screenshot: {exc}")
        if needs_vision and image:
            state.vision, error = self._vision(xml_fingerprint, state.screenshot_hash, image)
            if error:
                state.observation_errors.append(error)
        state.page_fingerprint = self._hash([xml_fingerprint, state.screenshot_hash])
        state.changed = bool(self._last_fingerprint and
                             state.page_fingerprint != self._last_fingerprint)
        self._last_fingerprint = state.page_fingerprint
        for element in state.elements:
            # Device UIElement remains backward compatible; this private lease
            # survives copying but is intentionally absent from dataclass output.
            element._perception_scope = state.app + "/" + state.activity
        self._cache[key] = (time.monotonic(), copy.deepcopy(state))
        return state

    snapshot = observe

    def observe_for(self, goal, *, fresh=False) -> ScreenState:
        """Collect only evidence needed by this goal; full observe stays public."""
        action = goal.action
        # direct_play/transport goals never need the UI tree, and uiautomator
        # cannot produce a stable snapshot while a video surface is rendering.
        light = action in ("open", "launch", "home", "pause", "resume", "continue",
                           "volume_up", "volume_down", "volume_set", "query_app", "direct_play",
                           "next", "previous", "seek", "seek_to")
        include_playback = action in ("play", "pause", "resume", "continue", "query_media",
                                      "next", "previous", "seek", "seek_to", "direct_play")
        state = self.observe(fresh=fresh, include_ui=not light,
                             include_playback=include_playback)
        if action == "home" and callable(getattr(self.device, "home_package", None)):
            try:
                state.system["home_package"] = self.device.home_package()
            except Exception as exc:
                state.observation_errors.append(f"home: {exc}")
        if action in ("volume_up", "volume_down", "volume_set") and callable(getattr(self.device, "volume", None)):
            try:
                state.system["volume"] = self.device.volume()
            except Exception as exc:
                state.observation_errors.append(f"volume: {exc}")
        return state

    def _enrich_media(self, state: ScreenState, raw: str | None = None) -> None:
        """Read actual, explicitly owned session positions; never extrapolate.

        TVDevice's compatible playback_status discards positions. This optional
        independent system query fills the missing evidence without a UI dump.
        Multiple sessions for one package are ambiguous unless exactly one is
        PLAYING. Unknown dumpsys layouts simply leave metadata absent.
        """
        shell = getattr(getattr(self.device, "adb", None), "shell", None)
        if (raw is None and not callable(shell)) or not state.app:
            return
        if "position_ms" in state.playback and "metadata" in state.playback:
            return
        try:
            if raw is None:
                result = shell(["dumpsys", "media_session"])
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout).strip())
                raw = result.stdout
            records: list[dict[str, Any]] = []
            current = None
            for line in raw.splitlines():
                header = re.match(r"\s*([\w]+(?:\.[\w]+)+)/[^\s]+", line)
                if not header and "SessionRecord{" in line:
                    header = re.search(r"\b([\w]+(?:\.[\w]+)+)/[^\s}]+", line)
                explicit = re.search(r"\bpackage(?:Name)?\s*[=:]\s*([\w.]+)", line)
                owner = (explicit or header).group(1) if explicit or header else ""
                if header or (explicit and (current is None or current["package"] != owner)):
                    current = {"package": owner, "metadata": {}}
                    records.append(current)
                elif re.match(r"\s*(?:SessionRecord\{|Session \S+)", line):
                    current = None
                if current is None:
                    continue
                match = re.search(r"\bstate\s*[=:]\s*(\d+|PLAYING|PAUSED)\b", line)
                if match:
                    current["state"] = match.group(1)
                position = re.search(r"(?<!buffered )\bposition\s*[=:]\s*(\d+)\b", line)
                if position and "PlaybackState" in line:
                    current["position_ms"] = int(position.group(1))
                title = re.search(r"android\.media\.metadata\.TITLE\s*[=:]\s*(.+)", line)
                if not title and "metadata" in line.lower():
                    title = re.search(r"\btitle\s*[=:]\s*([^,}]+)", line, re.I)
                if title:
                    current["metadata"]["title"] = title.group(1).strip().strip('"')
            owned = [record for record in records if record["package"] == state.app]
            active = [record for record in owned if record.get("state") in ("3", "PLAYING")]
            selected = active[0] if len(active) == 1 else owned[0] if len(owned) == 1 else None
            if selected:
                if "position_ms" in selected:
                    state.playback.setdefault("position_ms", selected["position_ms"])
                if selected["metadata"]:
                    state.playback.setdefault("metadata", selected["metadata"])
        except Exception as exc:
            state.observation_errors.append(f"media_metadata: {exc}")

    @staticmethod
    def _hash(value) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()

    def _vision(self, fingerprint: str, image_hash: str, image: bytes):
        key = (fingerprint, image_hash)
        if key in self._vision_cache:
            self._vision_cache.move_to_end(key)
            return copy.deepcopy(self._vision_cache[key])
        prompt = ('Describe only visible UI. Return JSON with texts (array of strings) '
                  'and summary. Do not infer the requested title or playback progress.')
        try:
            if callable(getattr(self.llm, "analyze_image_bytes", None)):
                raw = self.llm.analyze_image_bytes(image, prompt)
            else:
                # mkstemp is private (0600); the path is never returned as a live
                # screenshot_path after its contents have been removed.
                descriptor, path = tempfile.mkstemp(prefix="adbtv-vision-", suffix=".png")
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(image)
                    raw = self.llm.analyze_image(path, prompt)
                finally:
                    os.unlink(path)
            if isinstance(raw, str):
                clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                raw = json.loads(clean)
            if not isinstance(raw, dict):
                raise ValueError("vision must return a JSON object")
            result = ({"source": "vision", "verified": False, "candidates": raw}, "")
        except Exception as exc:
            result = ({}, f"vision: {exc}")
        # Failures are deduplicated too; bounded LRU prevents unbounded retention.
        if self.vision_cache_size:
            self._vision_cache[key] = copy.deepcopy(result)
            while len(self._vision_cache) > self.vision_cache_size:
                self._vision_cache.popitem(last=False)
        return result

    def wait_for(self, predicate: Callable[[ScreenState], bool], timeout: float,
                 cancelcheck: Callable[[], bool] | None = None,
                 interval: float = .2, **observe_kwargs) -> ScreenState | None:
        """Poll fresh observations until predicate, timeout, or cancellation.

        A true cancelcheck cancels; exceptions (including agent cancellation)
        propagate. No fixed startup sleep and one observation even at timeout=0.
        """
        end = time.monotonic() + max(0., timeout)
        observe_kwargs.pop("fresh", None)
        while True:
            if cancelcheck and cancelcheck():
                return None
            state = self.observe(fresh=True, **observe_kwargs)
            if cancelcheck and cancelcheck():
                return None
            if predicate(state):
                return state
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(max(.001, interval), remaining))

    @staticmethod
    def _contains(element: UIElement, coords: tuple[int, int]) -> bool:
        bounds = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]",
                              element.bounds or "")
        if not bounds:
            return False
        x1, y1, x2, y2 = map(int, bounds.groups())
        return x1 <= coords[0] < x2 and y1 <= coords[1] < y2

    def locate(self, id: str | None = None, text: str | None = None,
               coords: tuple[int, int] | None = None, *, state: ScreenState | None = None,
               fresh: bool = True) -> UIElement | None:
        """Exact, unique matching; snapshot-local node IDs require explicit state.

        Coordinates resolve a currently visible actionable element, never an
        unchecked blind tap. Ambiguous selectors intentionally return None.
        """
        if id is None and text is None and coords is None:
            return None
        explicit_state = state is not None
        state = state if state is not None else self.observe(fresh=fresh, include_playback=False)
        matches = [e for e in state.elements
                   if (id is None or e.resource_id == id or (explicit_state and e.node_id == id))
                   and (text is None or text in (e.text, e.desc))
                   and (coords is None or ((e.clickable or e.focusable) and self._contains(e, coords)))]
        if len(matches) != 1:
            return None
        found = matches[0]
        self._issued[id_builtin(found)] = (self._generation, state.app + "/" + state.activity, found)
        self._issued.move_to_end(id_builtin(found))
        while len(self._issued) > 256:
            self._issued.popitem(last=False)
        return found

    def validate(self, element: UIElement) -> UIElement | None:
        """Rebind stable identity; snapshot node IDs/old bounds are not identity."""
        state = self.observe(fresh=True, include_playback=False)
        return self._rebind(element, state)

    def _rebind(self, element: UIElement, state: ScreenState) -> UIElement | None:
        lease = self._issued.get(id_builtin(element))
        scope = getattr(element, "_perception_scope", lease[1] if lease else None)
        if scope is not None and scope != state.app + "/" + state.activity:
            return None
        # A recycled resource ID must also retain its observed label.
        if not (element.resource_id or element.text or element.desc):
            return None
        matches = [e for e in state.elements
                   if (not element.resource_id or e.resource_id == element.resource_id)
                   and (not element.text or e.text == element.text)
                   and (not element.desc or e.desc == element.desc)]
        return matches[0] if len(matches) == 1 else None

    def click(self, element: UIElement | None = None, *, id: str | None = None,
              text: str | None = None, coords: tuple[int, int] | None = None):
        """Freshly locate and validate, tap once, then expire all observations.

        A tap result reports command execution only; Verifier decides completion.
        """
        state = self.observe(fresh=True, include_playback=False)
        if element is not None:
            target = self._rebind(element, state)
        else:
            target = self.locate(id=id, text=text, coords=coords, state=state)
        if target is None:
            raise ValueError("目标已失效或无法唯一重新定位")
        by_id = {e.node_id: e for e in state.elements}
        seen = set()
        while not (target.clickable or target.focusable):
            if not target.parent_id or target.parent_id in seen:
                raise ValueError("目标没有可验证的可操作控件")
            seen.add(target.parent_id)
            target = by_id.get(target.parent_id)
            if target is None:
                raise ValueError("目标父控件不可用")
        if not self._contains(target, (target.x, target.y)):
            raise ValueError("目标缺少有效坐标")
        try:
            return self.device.tap(target.x, target.y)
        finally:
            self.invalidate()


# Avoid shadowing Python's id() in the public selector API.
id_builtin = id
