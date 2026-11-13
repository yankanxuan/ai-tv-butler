"""Pluggable voice inputs sharing one session/dialogue policy.

Sources:

* ``xiaomi_logcat`` — existing ADB logcat ASR listener (implemented in voice.py).
* ``web_microphone`` — the authenticated console's browser microphone; the
  browser performs recognition and posts text, so this module only routes it.
* ``external_asr`` — reserved interface; it raises instead of pretending to work.

No microphone exists on the NAS, so continuous listening is never claimed: the
Xiaomi path still needs a wake word or the remote's voice key for every turn.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SOURCES = ("xiaomi_logcat", "web_microphone", "external_asr")
IMPLEMENTED = ("xiaomi_logcat", "web_microphone")


@dataclass(frozen=True)
class InputEvent:
    text: str
    source: str
    tv_id: str
    request_id: str = ""
    at: float = 0.0


class UnsupportedInput(RuntimeError):
    pass


class SharedSessionStore:
    """Cross-process session bridge for containers sharing /data.

    Each TV has one JSON file guarded by ``flock``; writes are atomic. Only the
    validated snapshot from ConversationManager is stored — never raw speech.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser()

    def _path(self, tv_id: str) -> Path:
        if not isinstance(tv_id, str) or not tv_id or len(tv_id) > 200:
            raise ValueError("Invalid tv id")
        import hashlib
        digest = hashlib.sha256(tv_id.encode("utf-8")).hexdigest()[:24]
        return self.directory / f"session-{digest}.json"

    @contextlib.contextmanager
    def _locked(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (path.parent / (path.name + ".lock")).open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @contextlib.contextmanager
    def transaction(self, tv_id: str):
        """Lock one TV for a full read-modify-write turn.

        Two containers confirming concurrently must not overwrite each other's
        pending action, so hydration and persistence share one lock scope.
        """
        path = self._path(tv_id)
        with self._locked(path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            state = data if isinstance(data, dict) else {}
            yield state
            payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
            fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)

    def load(self, tv_id: str) -> dict | None:
        path = self._path(tv_id)
        if not path.is_file():
            return None
        with self._locked(path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return data if isinstance(data, dict) else None

    def save(self, tv_id: str, state: dict) -> None:
        path = self._path(tv_id)
        payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
        with self._locked(path):
            fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)


class VoiceInputRegistry:
    """Route any supported input through one dialogue policy object."""

    def __init__(self, dialogue, *, store: SharedSessionStore | None = None,
                 tv_id: str = "default-tv", on_event: Callable[[dict], None] | None = None):
        self.dialogue = dialogue
        self.store = store
        self.tv_id = tv_id
        self.on_event = on_event or (lambda event: None)
        self.counts = {source: 0 for source in SOURCES}

    def _hydrate(self) -> None:
        if not self.store:
            return
        state = self.store.load(self.tv_id)
        if state:
            try:
                self.dialogue.sessions.hydrate(self.tv_id, state)
            except ValueError:
                pass  # Corrupt shared state must not crash a live turn.

    def _persist(self) -> None:
        if not self.store:
            return
        try:
            self.store.save(self.tv_id, self.dialogue.sessions.export(self.tv_id))
        except (OSError, ValueError) as exc:
            self.on_event({"kind": "session_store_error", "error": str(exc)[:160]})

    def submit(self, event: InputEvent) -> dict:
        if event.source not in SOURCES:
            raise UnsupportedInput(f"未知语音输入源: {event.source}")
        if event.source == "external_asr":
            # Reserved interface: refuse explicitly instead of faking support.
            raise UnsupportedInput("external_asr 尚未接入，请使用 xiaomi_logcat 或 web_microphone")
        if not isinstance(event.text, str) or not event.text.strip():
            return {"status": "IGNORED", "route": "ignore",
                    "reply": "没有识别到内容。", "source": event.source}
        self.counts[event.source] += 1
        if self.store is None:
            result = self.dialogue.handle(event.text, tv_id=self.tv_id, source=event.source)
        else:
            # One lock scope: hydrate -> handle -> persist, so a concurrent
            # confirmation in another container cannot clobber this write.
            with self.store.transaction(self.tv_id) as state:
                if state:
                    try:
                        self.dialogue.sessions.hydrate(self.tv_id, state)
                    except ValueError:
                        pass
                result = self.dialogue.handle(event.text, tv_id=self.tv_id, source=event.source)
                state.clear()
                state.update(self.dialogue.sessions.export(self.tv_id))
        result["source"] = event.source
        return result

    def submit_text(self, text: str, *, source: str, request_id: str = "") -> dict:
        return self.submit(InputEvent(text=text, source=source, tv_id=self.tv_id,
                                      request_id=request_id, at=time.time()))

    def status(self) -> dict[str, Any]:
        return {"sources": {source: {"implemented": source in IMPLEMENTED,
                                     "count": self.counts[source]} for source in SOURCES},
                "shared_store": str(self.store.directory) if self.store else None,
                "continuous_listening": False,
                "mic_note": "NAS 无麦克风；小米路径每轮仍需唤醒词或遥控器语音键。"}