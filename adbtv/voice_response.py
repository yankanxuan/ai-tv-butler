"""Response policy: decide *whether* to speak, then deliver safely.

Speech delivery to the TV is not implemented because it was measured to be
unavailable (no PCM access, WAV intents only reach Bluetooth OTP, and the video
player would take over the surface and interrupt playback). The delivered
channels are therefore:

* ``tv_text`` — always available; the reply is returned to the caller/console.
* ``web_audio`` — Piper WAV served by the authenticated console for a phone or
  browser; never injected into the TV audio path.

Rules keep interruptions low: nothing is spoken while video is playing except
an explicit failure, and confirmation questions are always delivered as text.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .tts import PiperTTS, Synthesis


@dataclass(frozen=True)
class ResponsePlan:
    speak: bool
    text: str
    channel: str            # tv_text | web_audio
    reason: str
    synthesis: Synthesis | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"speak": self.speak, "channel": self.channel, "reason": self.reason,
                "chars": len(self.text),
                "synthesis": self.synthesis.to_dict() if self.synthesis else None}


# Stages the dialogue can announce.
STAGES = ("start", "clarify", "success", "failure", "confirm")


class VoiceResponder:
    def __init__(self, cfg: dict, *, tts: PiperTTS | None = None,
                 base_url: str = ""):
        voice = (cfg.get("voice") or {})
        self.options = (voice.get("tts") or {})
        self.enabled = bool(self.options.get("enabled", False))
        self.ack_start = bool(self.options.get("ack_on_start", False))
        self.speak_during_playback = bool(self.options.get("speak_during_playback", False))
        self.include_url = bool(self.options.get("include_url", True))
        self.tts = tts if tts is not None else PiperTTS(cfg)
        self.base_url = base_url or self.options.get("audio_base_url", "")
        self.async_mode = bool(self.options.get("async", True))
        self._lock = threading.Lock()
        self.spoken = 0
        self.suppressed = 0
        self._overflow = 0
        self.queue_size = max(1, int(self.options.get("queue_size", 4)))
        self._queue: queue.Queue = queue.Queue(maxsize=self.queue_size)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready: dict[str, dict] = {}
        self._failures = 0
        self._synthesized = 0
        self._delivered = 0
        self._last_error = ""
        # Per-request TTS jobs: QUEUED -> SYNTHESIZING -> READY/FAILED.
        self._jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()

    # ---------- 策略 ----------
    def plan(self, stage: str, text: str, *, status: str = "",
             playback_state: str = "") -> ResponsePlan:
        clean = " ".join((text or "").split())[:200]
        if stage not in STAGES:
            return ResponsePlan(False, clean, "tv_text", "unknown_stage")
        if not self.enabled:
            return ResponsePlan(False, clean, "tv_text", "tts_disabled")
        if not clean:
            return ResponsePlan(False, clean, "tv_text", "empty_text")
        playing = playback_state.lower() == "playing"
        if stage == "start" and not self.ack_start:
            return ResponsePlan(False, clean, "tv_text", "ack_disabled")
        if playing and not self.speak_during_playback and stage != "failure":
            # Never disturb the film for a routine acknowledgement.
            return ResponsePlan(False, clean, "tv_text", "video_playing")
        if stage == "confirm":
            # A confirmation must be read deliberately; keep it on screen text.
            return ResponsePlan(False, clean, "tv_text", "confirmation_is_text")
        return ResponsePlan(True, clean, "web_audio", "policy_allows")

    # ---------- 交付 ----------
    def deliver(self, plan: ResponsePlan) -> dict[str, Any]:
        """Synthesise and, when a delivery URL is configured, expose the audio.

        States are explicit: synthesised (WAV exists), deliverable (a URL can be
        built), delivered (URL registered for the console), played (never true —
        the TV audio path is unavailable by measurement).
        """
        started = time.monotonic()
        if not plan.speak:
            self.suppressed += 1
            return {"speak": False, "channel": "tv_text", "reason": plan.reason,
                    "text": plan.text, "tts_ms": 0.0, "synthesized": False,
                    "deliverable": False, "delivered": False, "played": False}
        synthesis = self.tts.synthesize(plan.text)
        base = {"text": plan.text, "synthesis": synthesis.to_dict(),
                "synthesized": synthesis.ok, "played": False,
                "tts_ms": (time.monotonic() - started) * 1000}
        if not synthesis.ok:
            return {"speak": False, "channel": "tv_text", "reason":
                    (synthesis.error or "synthesis_failed"), "synthesis_failed": True, **base}
        url = synthesis.audio_url(self.base_url) if (self.include_url and self.base_url) else ""
        if not url:
            # Synthesised but not deliverable: no configured public audio address.
            return {"speak": False, "channel": "tv_text",
                    "reason": "audio_base_url_missing", "deliverable": False,
                    "delivered": False, "audio_url": "", **base}
        return {"speak": True, "channel": "web_audio", "reason": plan.reason,
                "deliverable": True, "delivered": True, "audio_url": url, **base}

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, name="voice-tts", daemon=True)
        self._worker.start()

    def _account(self, result: dict) -> None:
        """Single place for counters: synthesised, deliverable, delivered, failed."""
        with self._lock:
            if result.get("synthesized"):
                self._synthesized += 1
            if result.get("delivered") and result.get("audio_url"):
                self._ready[result["audio_url"]] = {
                    "at": time.time(),
                    "duration_ms": (result.get("synthesis") or {}).get("duration_ms", 0)}
                if len(self._ready) > 32:
                    self._ready.pop(next(iter(self._ready)))
                self._delivered += 1
                self.spoken = self._delivered
            elif result.get("synthesis_failed"):
                self._failures += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id, plan = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._set_job(job_id, "SYNTHESIZING")
            try:
                result = self.deliver(plan)
                self._account(result)
                if result.get("delivered") and result.get("audio_url"):
                    self._set_job(job_id, "READY", audio_url=result["audio_url"],
                                  duration_ms=(result.get("synthesis") or {}).get("duration_ms", 0))
                else:
                    self._set_job(job_id, "FAILED",
                                  reason=str(result.get("reason") or "synthesis_failed"))
            except Exception as exc:  # noqa: BLE001 - a bad turn must not end the worker
                with self._lock:
                    self._failures += 1
                    self._last_error = str(exc)[:200]
                self._set_job(job_id, "FAILED", reason="worker_error")
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def _set_job(self, job_id: str, state: str, **extra) -> None:
        from .schemas import SchemaError, validate_job
        try:
            validate_job({"id": job_id, "state": state, **extra})
        except SchemaError as exc:
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job.update(state="FAILED", reason=f"schema:{exc}"[:120])
            return
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(state=state, updated=time.time(), **extra)
            if len(self._jobs) > 200:      # bounded: drop the oldest entries
                for stale in sorted(self._jobs, key=lambda k: self._jobs[k]["created"])[:100]:
                    self._jobs.pop(stale, None)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def respond(self, stage: str, text: str, **kwargs) -> dict[str, Any]:
        """Plan immediately; synthesize on a bounded background worker.

        Piper reloads its model per call (~9-10 s measured on this NAS), so
        synchronous synthesis would delay later turns. Interrupts never enter
        this queue at all.
        """
        plan = self.plan(stage, text, **kwargs)
        if not plan.speak:
            self.suppressed += 1
            return {"speak": False, "channel": "tv_text", "reason": plan.reason,
                    "text": plan.text, "tts_ms": 0.0, "pending": False,
                    "delivered": False}
        if not self.async_mode:
            result = self.deliver(plan)
            self._account(result)
            return result
        self._ensure_worker()
        job_id = uuid.uuid4().hex
        with self._jobs_lock:
            self._jobs[job_id] = {"id": job_id, "state": "QUEUED", "created": time.time(),
                                  "text_chars": len(plan.text), "planned": True}
        try:
            self._queue.put_nowait((job_id, plan))
        except queue.Full:
            # Honest overflow: nothing was queued, so nothing will be spoken.
            # This is capacity, not a synthesis failure - count it separately.
            with self._lock:
                self._overflow += 1
            self._set_job(job_id, "FAILED", reason="tts_queue_full")
            return {"speak": False, "channel": "tv_text", "reason": "tts_queue_full",
                    "text": plan.text, "tts_ms": None, "pending": False,
                    "delivered": False, "job_id": job_id}
        return {"speak": True, "channel": "web_audio", "reason": plan.reason,
                "text": plan.text, "tts_ms": None, "pending": True,
                "queued": True, "delivered": False, "audio_url": None,
                "job_id": job_id, "job_state": "QUEUED"}

    def ready(self) -> list[str]:
        with self._lock:
            return list(self._ready)

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"spoken": self._delivered, "synthesized": self._synthesized,
                    "delivered": self._delivered, "suppressed": self.suppressed,
                    "failures": self._failures, "overflow": self._overflow,
                    "queue_size": self.queue_size, "queued": self._queue.qsize(),
                    "async": self.async_mode, "ready": len(self._ready),
                    "worker_alive": bool(self._worker and self._worker.is_alive()),
                    "last_error": self._last_error, "played": 0}

    def status(self) -> dict[str, Any]:
        available, reason = self.tts.available()
        return {"tts_enabled": self.enabled, "tts_available": available,
                "tts_reason": reason,
                "tv_audio": False,
                "tv_audio_note": "电视端音频实测不可用（tinyplay 无法打开 PCM，WAV 意图只到蓝牙）",
                "delivery": "web_audio" if self.base_url else "unavailable_no_base_url",
                "audio_base_url": self.base_url,
                "auth": "控制台令牌（X-Auth-Token）；/api/voice/audio 需认证",
                **self.stats()}