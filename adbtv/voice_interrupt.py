"""High-priority voice interruption for the existing task/Agent machinery.

Interrupts never travel through the normal bounded queue and never call the
model: the ASR reader recognises a small closed vocabulary and writes the
existing task control file, which the running Agent already checks at its
safety checkpoints. A slow normal task (including TTS) therefore cannot delay a
cancel by queueing.

This module deliberately separates two intents the user must be able to tell
apart:

* ``cancel_task`` / ``pause_task`` / ``resume_task`` — act on the Agent task.
* ``stop_media`` / ``pause_media`` / ``resume_media`` — act on the video itself.

A bare "停下/别做了" is ambiguous and asks the user which one they mean.
"""
from __future__ import annotations

import itertools
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

# Closed vocabulary; no model, no free-form parsing.
TASK_CANCEL = ("取消任务", "取消当前任务", "取消操作", "停止操作", "停止任务",
               "别做了", "不用做了", "中断任务", "取消刚才的任务")
TASK_PAUSE = ("暂停任务", "暂停执行", "先暂停一下任务", "暂停agent", "暂停 agent")
TASK_RESUME = ("继续任务", "恢复任务", "继续执行任务")
TAKEOVER = ("人工接管", "我来操作", "接管电视")
MEDIA_STOP = ("停止播放", "停止视频", "关掉视频", "别播了")
MEDIA_PAUSE = ("暂停播放", "暂停视频", "暂停")
MEDIA_RESUME = ("继续播放", "继续视频", "继续")
AMBIGUOUS_STOP = ("停下", "停止", "停", "别说了", "安静")


def classify_interrupt(text: str) -> tuple[str, str] | None:
    """Return (action, kind) for an interrupt utterance, else None.

    kind is ``task`` (Agent control) or ``media`` (video control) or ``ask``.
    Matching is exact-after-punctuation-strip to avoid treating a normal movie
    request containing these words as an interrupt.
    """
    if not isinstance(text, str):
        return None
    clean = re.sub(r"[\s，,。.!！?？、]+", "", text)
    if not clean or len(clean) > 40:
        return None
    lowered = clean.casefold()
    for phrase in TAKEOVER:
        if lowered == phrase.replace(" ", "").casefold():
            return ("takeover", "task")
    for phrase in TASK_CANCEL:
        if lowered == phrase.replace(" ", "").casefold():
            return ("cancel", "task")
    for phrase in TASK_PAUSE:
        if lowered == phrase.replace(" ", "").casefold():
            return ("pause", "task")
    for phrase in TASK_RESUME:
        if lowered == phrase.replace(" ", "").casefold():
            return ("resume", "task")
    for phrase in MEDIA_STOP:
        if lowered == phrase.replace(" ", "").casefold():
            return ("cancel", "media")
    for phrase in MEDIA_PAUSE:
        if lowered == phrase.replace(" ", "").casefold():
            return ("pause", "media")
    for phrase in MEDIA_RESUME:
        if lowered == phrase.replace(" ", "").casefold():
            return ("resume", "media")
    for phrase in AMBIGUOUS_STOP:
        if lowered == phrase.replace(" ", "").casefold():
            return ("ask", "ambiguous")
    return None


@dataclass(frozen=True)
class InterruptResult:
    action: str
    kind: str
    ok: bool
    status: str
    reply: str
    task_ids: tuple[str, ...] = ()
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "kind": self.kind, "ok": self.ok,
                "status": self.status, "reply": self.reply,
                "task_ids": list(self.task_ids),
                "elapsed_ms": round(self.elapsed_ms, 1)}


class InterruptChannel:
    """Serialise interrupt handling and apply it to live Agent tasks."""

    def __init__(self, cfg: dict, *, device=None, on_event=None, clock=time.monotonic):
        self.cfg = cfg
        self.device = device
        self.on_event = on_event or (lambda event: None)
        self.clock = clock
        self._lock = threading.RLock()
        self.handled = 0
        self.paused = False
        self.takeover = False
        options = (cfg.get("voice") or {}).get("interrupt") or {}
        self.allow_cross_container = bool(options.get("allow_cross_container", True))
        self.allowed_roles = set(options.get("roles") or ("console", "scheduler", "voice", "cli"))
        self.media_wait = float(options.get("media_wait_seconds", 3.0))
        adb = (cfg.get("adb") or {})
        self.device_id = adb.get("serial") or adb.get("host") or ""
        # Slow media ADB work runs here so the ASR reader is never blocked.
        self._media_queue: "queue.Queue" = queue.Queue(maxsize=4)
        self._media_worker: threading.Thread | None = None
        self._media_results: dict[int, InterruptResult] = {}
        self._media_seq = 0
        self._stop = threading.Event()

    # ---------- 任务发现 ----------
    def _live_tasks(self) -> list[dict]:
        """Tasks for *this TV*, not merely this process.

        Ownership is the shared device identity recorded with each task, so a
        voice entry can cancel a task started by the console or scheduler while
        still refusing tasks that belong to another TV or another user's role.
        """
        from .execution import list_tasks
        rows = []
        for record in list_tasks(self.cfg, limit=50):
            if record.get("state") not in ("RUNNING", "PAUSED", "QUEUED"):
                continue
            record_device = record.get("device")
            if record_device:
                if self.device_id:
                    if record_device != self.device_id:
                        continue      # another TV must never be touched
                elif record.get("pid") != os.getpid():
                    # Unknown local device identity: never touch other processes.
                    continue
            if not self.allow_cross_container and record.get("pid") != os.getpid():
                continue
            role = record.get("role") or "cli"
            if role not in self.allowed_roles:
                continue
            rows.append(record)
        return rows

    def _control(self, task_id: str, action: str) -> dict:
        from .execution import control_task
        return control_task(self.cfg, task_id, action)

    # ---------- 处理 ----------
    def _policy_text(self, text: str) -> str | None:
        """Apply the same prefix authorisation as normal voice commands."""
        prefixes = [p for p in ((self.cfg.get("voice") or {}).get("listener") or {})
                    .get("prefix_only") or [] if isinstance(p, str) and p]
        clean = (text or "").strip()
        if not prefixes:
            return clean
        for prefix in prefixes:
            if clean.startswith(prefix):
                return clean[len(prefix):].strip()
        return None

    def handle(self, text: str, *, wait: float | None = None) -> InterruptResult | None:
        policy_text = self._policy_text(text)
        if policy_text is None:
            # Unauthorised entry point: report it, never act.
            return InterruptResult("deny", "policy", False, "UNAUTHORIZED",
                                   "该语音入口没有打断授权（需要配置的前缀）。")
        classified = classify_interrupt(policy_text)
        if classified is None:
            return None
        action, kind = classified
        started = self.clock()
        with self._lock:
            self.handled += 1
            if kind == "ambiguous":
                reply = "你是指取消 Agent 当前操作，还是停止正在播放的视频？"
                result = InterruptResult(action, kind, True, "NEEDS_CLARIFICATION", reply)
            elif kind == "task":
                result = self._task_action(action, started)
            else:
                # Default is non-blocking: the ASR reader must never wait on ADB.
                budget = 0.0 if wait is None else max(0.0, float(wait))
                result = self._media_action(action, started, budget)
            self.on_event({"kind": "interrupt", **result.to_dict()})
            return result

    def _task_action(self, action: str, started: float) -> InterruptResult:
        tasks = self._live_tasks()
        if not tasks:
            # Cancel with nothing running is a truthful no-op.
            return InterruptResult(action, "task", True, "NO_TASK",
                                   "当前没有正在执行的 Agent 任务。" if action == "cancel"
                                   else "当前没有可操作的任务。",
                                   (), (self.clock() - started) * 1000)
        applied, ids = [], []
        for record in tasks:
            task_id = str(record.get("id") or "")
            if not task_id:
                continue
            try:
                if action == "takeover":
                    self._control(task_id, "cancel")
                else:
                    self._control(task_id, action)
            except (OSError, ValueError):
                continue
            applied.append(task_id)
            ids.append(task_id)
        if action == "takeover":
            self.takeover = True
        if action == "pause":
            self.paused = True
        elif action in ("resume", "cancel", "takeover"):
            self.paused = False
        if not applied:
            return InterruptResult(action, "task", False, "FAILED",
                                   "无法写入任务控制指令。", (), (self.clock() - started) * 1000)
        label = {"cancel": "已请求取消", "pause": "已暂停", "resume": "已继续",
                 "takeover": "已取消任务并交还人工接管"}[action]
        return InterruptResult(action, "task", True, "INTERRUPTED",
                               f"{label} {len(applied)} 个任务。", tuple(ids),
                               (self.clock() - started) * 1000)

    def _ensure_media_worker(self) -> None:
        if self._media_worker and self._media_worker.is_alive():
            return
        self._media_worker = threading.Thread(target=self._media_loop,
                                              name="voice-interrupt-media", daemon=True)
        self._media_worker.start()

    def _media_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sequence, action = self._media_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._media_results[sequence] = self._media_execute(action)
            except Exception as exc:  # noqa: BLE001
                self._media_results[sequence] = InterruptResult(
                    action, "media", False, "FAILED", f"电视操作失败：{str(exc)[:120]}")
            finally:
                self._media_queue.task_done()

    def _media_action(self, action: str, started: float, budget: float = 0.0) -> InterruptResult:
        """Submit to the media worker; wait only up to a bounded budget."""
        self._ensure_media_worker()
        self._media_seq += 1
        sequence = self._media_seq
        try:
            self._media_queue.put_nowait((sequence, action))
        except queue.Full:
            return InterruptResult(action, "media", False, "BUSY",
                                   "打断队列已满，请稍后重试。", (),
                                   (self.clock() - started) * 1000)
        deadline = self.clock() + budget
        while self.clock() < deadline:
            result = self._media_results.pop(sequence, None)
            if result is not None:
                return result
            time.sleep(0.02)
        # Still running: report honestly instead of blocking ASR or faking success.
        return InterruptResult(action, "media", True, "PENDING",
                               "正在执行电视操作，尚未确认完成。", (),
                               (self.clock() - started) * 1000)

    def _media_execute(self, action: str) -> InterruptResult:
        started = self.clock()
        if self.device is None:
            return InterruptResult(action, "media", False, "FAILED",
                                   "没有可用的电视连接。", (), (self.clock() - started) * 1000)
        try:
            if action == "cancel":
                self.device.key("home")
                reply = "已退出播放。"
            else:
                self.device.transport(action)
                reply = "已继续播放。" if action == "resume" else "已暂停播放。"
            state = self.device.playback_status(include_ui=False)
        except Exception as exc:  # noqa: BLE001 - report, never fake success
            return InterruptResult(action, "media", False, "FAILED",
                                   f"电视操作失败：{str(exc)[:120]}", (), (self.clock() - started) * 1000)
        ok = (state.get("playing") is True) if action == "resume" else (state.get("playing") is False)
        return InterruptResult(action, "media", bool(ok), "SUCCESS" if ok else "PARTIAL",
                               reply, (), (self.clock() - started) * 1000)