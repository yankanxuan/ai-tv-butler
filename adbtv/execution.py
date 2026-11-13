"""Bounded task lifecycle and cross-process, per-device execution leases."""
from __future__ import annotations

import contextvars
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class ControlSignal(BaseException):
    """Never swallowed by an application/tool's generic recovery handler."""


class TaskCancelled(ControlSignal):
    pass


class TaskTimeout(ControlSignal):
    pass


class DeviceBusy(RuntimeError):
    pass


_current = contextvars.ContextVar("tv_task", default=None)
_leases = contextvars.ContextVar("tv_leases", default=())


def runtime_dir(cfg) -> Path:
    value = cfg.get("execution", {}).get("state_dir")
    if value:
        return Path(value).expanduser().resolve()
    data_dir = cfg.get("paths", {}).get("data_dir") or cfg.get("data_dir")
    if data_dir:
        return Path(data_dir).expanduser().resolve() / "runtime"
    fake_state = cfg.get("adb", {}).get("fake_state")
    if fake_state:
        return Path(fake_state).resolve().parent / "runtime"
    return Path(__file__).resolve().parent.parent / "state" / "runtime"


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, default=str)
            f.flush()
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def device_lease(cfg, *, blocking=False):
    serial = cfg.get("adb", {}).get("serial") or cfg.get("adb", {}).get("host") or "default"
    lock_dir = runtime_dir(cfg) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = str(lock_dir / (hashlib.sha256(serial.encode()).hexdigest()[:20] + ".lock"))
    if key in _leases.get():
        yield
        return
    with open(key, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as e:
            raise DeviceBusy("电视正在执行其他任务，请等待或取消当前任务") from e
        token = _leases.set(_leases.get() + (key,))
        try:
            yield
        finally:
            _leases.reset(token)
            fcntl.flock(f, fcntl.LOCK_UN)


class TaskControl:
    def __init__(self, cfg, instruction: str, task_id: str | None = None):
        self.cfg = cfg
        self.id = task_id or uuid.uuid4().hex
        if not self.id.isalnum() or len(self.id) > 64:
            raise ValueError("Invalid task id")
        self.path = runtime_dir(cfg) / "tasks" / (self.id + ".json")
        self.command_path = self.path.with_suffix(".control.json")
        self.started = time.time()
        self.deadline = time.monotonic() + float(cfg.get("execution", {}).get("timeout", 180))
        self.pause_timeout = float(cfg.get("execution", {}).get("pause_timeout", 900))
        self.absolute_deadline = self.deadline + self.pause_timeout
        self.max_actions = int(cfg.get("execution", {}).get("max_actions", 100))
        self.actions = 0
        self.recoveries = 0
        self.llm_calls = 0
        self.state = "QUEUED"
        self.instruction = instruction
        self.events: list[dict[str, Any]] = []
        self.result = None
        self._lock = threading.RLock()
        self._last_control = 0.0
        self._desired = "resume"
        self._previous_adb_hook = None
        self._context_token = None
        self.emit("created", goal=instruction[:2000])

    def emit(self, kind, **data):
        with self._lock:
            self.events.append({"at": time.time(), "kind": kind, **data})
            self.events = self.events[-200:]
            self.save()

    def save(self):
        atomic_json(self.path, self.to_dict())

    def to_dict(self):
        adb = self.cfg.get("adb", {}) or {}
        record = {"id": self.id, "instruction": self.instruction[:2000], "state": self.state,
                  "started": self.started, "updated": time.time(), "pid": os.getpid(), "actions": self.actions,
                  "recoveries": self.recoveries, "llm_calls": self.llm_calls,
                  # Device identity lets another container target the same TV
                  # without relying on the owning process id.
                  "device": adb.get("serial") or adb.get("host") or "",
                  "role": str(self.cfg.get("_service_role") or "cli")[:24],
                  "events": list(self.events), "result": self.result}
        if self.cfg.get("_server_instance_id"):
            record["server_instance_id"] = self.cfg["_server_instance_id"]
            record["owner_pid"] = self.cfg.get("_server_owner_pid", os.getpid())
        return record

    def checkpoint(self):
        now = time.monotonic()
        if now >= self.deadline:
            raise TaskTimeout("任务达到总时间上限")
        # Always read before a device action: a cancellation must not be hidden
        # behind a time-based cache while fast consecutive inputs are emitted.
        self._last_control = now
        try:
            self._desired = json.loads(self.command_path.read_text()).get("action", "resume")
        except FileNotFoundError:
            pass
        if self._desired == "cancel":
            raise TaskCancelled("任务已取消")
        if self._desired == "pause":
            paused_at = time.monotonic()
            if self.state != "PAUSED":
                self.state = "PAUSED"
                self.emit("paused")
            while self._desired == "pause":
                time.sleep(.1)
                self._last_control = 0
                # Recursive checkpoint immediately enters this loop again unless
                # we perform the control read directly here.
                if time.monotonic() >= min(paused_at + self.pause_timeout, self.absolute_deadline):
                    raise TaskTimeout("人工暂停达到时长上限")
                try:
                    self._desired = json.loads(self.command_path.read_text()).get("action", "resume")
                except FileNotFoundError:
                    self._desired = "resume"
                if self._desired == "cancel":
                    raise TaskCancelled("任务已取消")
            self.deadline = min(self.deadline + time.monotonic() - paused_at, self.absolute_deadline)
            self.state = "RUNNING"
            self.emit("resumed")

    def before_adb(self, args=None):
        self.checkpoint()
        parts = args or []
        if isinstance(parts, (list, tuple)):
            text = " ".join(str(x) for x in parts)
            if any(x in text for x in ("input ", "am start", "am force-stop", "monkey ", "am broadcast")):
                self.actions += 1
                if self.actions > self.max_actions:
                    raise TaskTimeout("达到最大设备操作数")

    @contextmanager
    def activate(self, adb=None):
        self.state = "RUNNING"
        self.emit("started")
        token = _current.set(self)
        previous = getattr(adb, "checkpoint", None) if adb is not None else None
        if adb is not None:
            adb.checkpoint = self.before_adb
        try:
            yield self
        finally:
            if adb is not None:
                adb.checkpoint = previous
            _current.reset(token)

    def finish(self, state, result):
        self.state = state
        self.result = result
        self.emit("finished", status=result.get("status", state))


def current_task() -> TaskControl | None:
    return _current.get()


def checkpoint():
    task = current_task()
    if task:
        task.checkpoint()


def control_task(cfg, task_id, action):
    if not isinstance(task_id, str) or not task_id.isalnum() or len(task_id) > 64:
        raise ValueError("Invalid task id")
    if action not in ("pause", "resume", "cancel"):
        raise ValueError("Invalid task action")
    path = runtime_dir(cfg) / "tasks" / (task_id + ".json")
    record = json.loads(path.read_text())
    if record["state"] not in ("QUEUED", "RUNNING", "PAUSED"):
        raise ValueError("任务已经结束")
    atomic_json(path.with_suffix(".control.json"), {"action": action})
    return {"id": task_id, "requested": action}


def list_tasks(cfg, limit=100):
    records = []
    directory = runtime_dir(cfg) / "tasks"
    for p in directory.glob("*.json"):
        if p.name.endswith(".control.json"):
            continue
        try:
            records.append(json.loads(p.read_text()))
        except (ValueError, OSError):
            continue
    return sorted(records, key=lambda r: r.get("started", 0), reverse=True)[:limit]
