"""JSON task scheduler with cross-process claims and bounded failure recovery.

The sidecar file lock protects only storage transactions, never task execution.
A crashed worker's expired lease becomes an explicit failure before any retry.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _now() -> datetime:
    return datetime.now()


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无法解析时间: {s}（支持 YYYY-MM-DD HH:MM[:SS] 或 ISO 时区）") from exc


def _zone(name: str):
    if name == "local":
        return None  # Legacy naive timestamps retain the host's local timezone.
    if not isinstance(name, str) or not name:
        raise ValueError("timezone 需要 local 或 IANA 时区名")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知时区: {name}") from exc


def _in_zone(value: datetime, name: str) -> datetime:
    zone = _zone(name)
    if zone is None:
        return value.astimezone() if value.tzinfo is not None else value
    # A naive _now() denotes the system's local instant, not the target wall time.
    return value.astimezone(zone)


def _stamp(value: datetime, name: str = "local") -> float:
    if value.tzinfo is None and name != "local":
        value = value.replace(tzinfo=_zone(name))
    return value.timestamp()


def _field_values(expr: str, lo: int, hi: int) -> set[int]:
    if not isinstance(expr, str) or not expr:
        raise ValueError("cron 字段不能为空")
    values = set()
    for part in expr.split(","):
        match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
        if not match:
            raise ValueError(f"cron 字段无效: {expr}")
        base, raw_step = match.groups()
        step = int(raw_step) if raw_step is not None else 1
        if step <= 0:
            raise ValueError("cron step 必须大于 0")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start, end = map(int, base.split("-"))
        else:
            start = int(base)
            end = hi if raw_step is not None else start
        if not lo <= start <= end <= hi:
            raise ValueError(f"cron 范围必须位于 {lo}..{hi}: {expr}")
        values.update(range(start, end + 1, step))
    return values


def _match_field(expr: str, value: int, lo: int, hi: int) -> bool:
    return value in _field_values(expr, lo, hi)


def _compile_cron(expr: str):
    if not isinstance(expr, str) or len(expr.split()) != 5:
        raise ValueError("cron 需为 5 段：分 时 日 月 周")
    fields = expr.split()
    minute, hour, dom, month, dow = (
        _field_values(field, low, high)
        for field, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))
    )
    dow = {day % 7 for day in dow}  # Both 0 and 7 mean Sunday.
    return minute, hour, dom, month, dow, fields[2].startswith("*"), fields[4].startswith("*")


def _day_match(compiled, day: datetime) -> bool:
    _, _, dom, month, dow, dom_star, dow_star = compiled
    if day.month not in month:
        return False
    dom_ok = day.day in dom
    dow_ok = (day.weekday() + 1) % 7 in dow
    # Vixie cron: when both fields are restricted, either may match.
    return dom_ok and dow_ok if dom_star or dow_star else dom_ok or dow_ok


def _cron_match(expr: str, dt: datetime) -> bool:
    compiled = _compile_cron(expr)
    return dt.minute in compiled[0] and dt.hour in compiled[1] and _day_match(compiled, dt)


def _next_cron(expr: str, after: datetime) -> datetime:
    compiled = _compile_cron(expr)
    # Iterate dates and allowed times rather than reparsing every minute. Eight
    # years includes the leap-day gap across a non-leap century and is bounded.
    start_day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    after_stamp = after.timestamp()
    for offset in range(366 * 8 + 1):
        day = start_day + timedelta(days=offset)
        if not _day_match(compiled, day):
            continue
        for hour in sorted(compiled[1]):
            for minute in sorted(compiled[0]):
                candidate = day.replace(hour=hour, minute=minute, fold=0)
                if candidate.tzinfo is not None:
                    roundtrip = candidate.astimezone(dt_timezone.utc).astimezone(candidate.tzinfo)
                    if roundtrip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
                        continue  # A nonexistent wall-clock time during DST.
                if candidate.timestamp() > after_stamp:
                    return candidate
    raise ValueError(f"未来八年内找不到匹配的 cron 时间: {expr}")


_UNSET = object()


class Scheduler:
    def __init__(self, store_path: str):
        self.path = os.path.abspath(os.fspath(store_path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.tasks: list[dict[str, Any]] = []
        self._snapshot: list[dict[str, Any]] = []
        self.load()

    @contextmanager
    def _locked(self):
        with open(self.path + ".lock", "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"tasks": []}
        try:
            with open(self.path, "r", encoding="utf-8") as source:
                def reject_constant(value):
                    raise ValueError(f"非法 JSON 数字: {value}")
                document = json.load(source, parse_constant=reject_constant)
            if not isinstance(document, dict) or not isinstance(document.get("tasks"), list):
                raise ValueError("需要包含 tasks 数组的 JSON 对象")
            ids = set()
            for task in document["tasks"]:
                if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
                    raise ValueError("任务缺少有效 id")
                if task["id"] in ids:
                    raise ValueError("任务 id 重复")
                ids.add(task["id"])
                if not isinstance(task.get("command"), str) or task.get("kind") not in ("once", "interval", "cron"):
                    raise ValueError("任务 command/kind 无效")
                if "enabled" in task and type(task["enabled"]) is not bool:
                    raise ValueError("任务 enabled 必须为布尔值")
                _zone(task.get("timezone", "local"))
                for key in ("next_run", "lease_until"):
                    if task.get(key) is not None:
                        _parse_dt(task[key])
                if task["kind"] == "interval":
                    self._integer(task.get("interval"), "interval", 1)
                if task["kind"] == "cron":
                    _compile_cron(task.get("cron"))
                self._integer(task.get("max_retries", 2), "max_retries", 0, 100)
                self._integer(task.get("attempts", 0), "attempts", 0)
                self._integer(task.get("lease_seconds", 300), "lease_seconds", 1, 86400)
                if task.get("status") == "RUNNING" and (not task.get("claim_token") or not task.get("lease_until")):
                    raise ValueError("RUNNING 任务缺少租约信息")
            return document
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"调度存储损坏，保留原文件 {self.path}: {exc}") from exc

    @staticmethod
    def _integer(value, label, minimum, maximum=None):
        if type(value) is not int or value < minimum or maximum is not None and value > maximum:
            raise ValueError(f"{label} 必须为 {minimum}..{maximum or '∞'} 的整数")
        return value

    def _write(self, document: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".tasks-", suffix=".tmp", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(document, output, ensure_ascii=False, indent=2, default=str, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(os.path.dirname(self.path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _remember(self, document):
        self.tasks = copy.deepcopy(document["tasks"])
        self._snapshot = copy.deepcopy(self.tasks)

    def load(self) -> None:
        with self._locked():
            self._remember(self._read())

    def save(self) -> None:
        """Merge explicit local edits, never write a stale whole-file snapshot.

        Conflicting edits fail visibly. Normal add/remove/run_due operations use
        their own transactions and do not depend on this legacy save helper.
        """
        with self._locked():
            document = self._read()
            baseline = {task["id"]: task for task in self._snapshot}
            local = {task["id"]: task for task in self.tasks}
            current = {task["id"]: task for task in document["tasks"]}
            for task_id, old in baseline.items():
                if task_id not in local:
                    if task_id in current and current[task_id] != old:
                        raise ValueError(f"任务 {task_id} 已被其他进程修改，请先 load")
                    current.pop(task_id, None)
            for task_id, task in local.items():
                old = baseline.get(task_id)
                if old is None:
                    if task_id in current:
                        raise ValueError(f"任务 id 冲突: {task_id}")
                    current[task_id] = copy.deepcopy(task)
                    continue
                changes = {key for key in set(task) | set(old) if task.get(key) != old.get(key) or (key in task) != (key in old)}
                if not changes:
                    continue
                if task_id not in current:
                    raise ValueError(f"任务 {task_id} 已被其他进程删除，请先 load")
                for key in changes:
                    if current[task_id].get(key) != old.get(key):
                        raise ValueError(f"任务 {task_id}.{key} 并发修改冲突，请先 load")
                    if key in task:
                        current[task_id][key] = copy.deepcopy(task[key])
                    else:
                        current[task_id].pop(key, None)
            document["tasks"] = list(current.values())
            self._write(document)
            self._remember(document)

    def add(self, command: str, kind: str, when: str | None = None,
            interval: int | None = None, cron: str | None = None,
            delay: int | None = None, max_retries: int = 2,
            timezone: str = "local", lease_seconds: int = 300) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("任务 command 不能为空")
        self._integer(max_retries, "max_retries", 0, 100)
        self._integer(lease_seconds, "lease_seconds", 1, 86400)
        now = _in_zone(_now(), timezone)
        task = {"id": uuid.uuid4().hex[:8], "command": command, "kind": kind,
                "enabled": True, "created": now.isoformat(timespec="seconds"),
                "last_run": None, "next_run": None, "timezone": timezone,
                "status": "PENDING", "max_retries": max_retries, "attempts": 0,
                "last_error": None, "last_result": None, "retry_pending": False,
                "lease_seconds": lease_seconds}
        if kind == "once":
            if delay is not None:
                self._integer(delay, "delay", 0)
                next_run = now + timedelta(seconds=delay)
            elif when:
                next_run = _parse_dt(when)
                if timezone != "local":
                    next_run = (next_run.replace(tzinfo=_zone(timezone)) if next_run.tzinfo is None
                                else next_run.astimezone(_zone(timezone)))
            else:
                raise ValueError("once 任务需要 --at 或 --in")
        elif kind == "interval":
            self._integer(interval, "interval", 1)
            task["interval"] = interval
            next_run = now + timedelta(seconds=interval)
        elif kind == "cron":
            task["cron"] = cron
            next_run = _next_cron(cron, now)
        else:
            raise ValueError(f"未知任务类型: {kind}")
        task["next_run"] = next_run.isoformat(timespec="seconds")
        with self._locked():
            document = self._read()
            document["tasks"].append(task)
            self._write(document)
            self._remember(document)
        return copy.deepcopy(task)

    def update(self, task_id: str, *, enabled=_UNSET, command=_UNSET) -> dict[str, Any] | None:
        """Merge future-execution settings under the storage lock.

        Disabling a schedule stops future claims, not an already running callback.
        A running callback keeps its original command/claim; a command edit applies
        to its next occurrence. Task identity, lease and result fields are untouched.
        Re-enabling a completed/cancelled one-shot does not recreate its next_run.
        Unsupported schedule edits are rejected by this deliberately small API.
        """
        changes = {}
        if enabled is not _UNSET:
            if type(enabled) is not bool:
                raise ValueError("enabled 必须为布尔值")
            changes["enabled"] = enabled
        if command is not _UNSET:
            if not isinstance(command, str) or not command.strip():
                raise ValueError("任务 command 不能为空")
            changes["command"] = command
        with self._locked():
            document = self._read()
            updated = None
            for task in document["tasks"]:
                if task["id"] == task_id:
                    task.update(changes)
                    updated = copy.deepcopy(task)
                    if changes:
                        self._write(document)
                    break
            self._remember(document)
            return updated

    def remove(self, task_id: str) -> bool:
        with self._locked():
            document = self._read()
            before = len(document["tasks"])
            document["tasks"] = [task for task in document["tasks"] if task["id"] != task_id]
            changed = len(document["tasks"]) != before
            if changed:
                self._write(document)
            self._remember(document)
            return changed

    def _advance(self, task: dict[str, Any], now: datetime) -> None:
        now = _in_zone(now, task.get("timezone", "local"))
        task["last_run"] = now.isoformat(timespec="seconds")
        task["retry_pending"] = False
        if task["kind"] == "once":
            task.update(enabled=False, next_run=None)
        elif task["kind"] == "interval":
            task["next_run"] = (now + timedelta(seconds=task["interval"])).isoformat(timespec="seconds")
        else:
            task["next_run"] = _next_cron(task["cron"], now).isoformat(timespec="seconds")

    def _fail(self, task, now, error, result=None):
        now = _in_zone(now, task.get("timezone", "local"))
        task.update(status="FAILED", last_error=str(error), last_result=result,
                    last_run=now.isoformat(timespec="seconds"))
        task.pop("claim_token", None)
        task.pop("lease_until", None)
        attempts = task.get("attempts", 1)
        if attempts <= task.get("max_retries", 2):
            seconds = min(30 * 2 ** min(max(attempts - 1, 0), 4), 300)
            task["next_run"] = (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
            task["retry_pending"] = True
        else:
            self._advance(task, now)

    def _recover(self, document, now):
        changed = False
        for task in document["tasks"]:
            if task.get("status") == "RUNNING" and _stamp(_parse_dt(task["lease_until"]), task.get("timezone", "local")) <= now.timestamp():
                task["lease_expirations"] = task.get("lease_expirations", 0) + 1
                self._fail(task, now, "执行租约已过期：前一进程可能崩溃，结果未知；按有限重试策略恢复")
                changed = True
        return changed

    @staticmethod
    def _is_due(task, now):
        return bool(task.get("enabled", True) and task.get("next_run")
                    and task.get("status") != "RUNNING"
                    and _stamp(_parse_dt(task["next_run"]), task.get("timezone", "local")) <= now.timestamp())

    def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or _now()
        with self._locked():
            document = self._read()
            if self._recover(document, now):
                self._write(document)
            self._remember(document)
            return copy.deepcopy([task for task in self.tasks if self._is_due(task, now)])

    def _claim(self, task_id):
        now = _now()
        with self._locked():
            document = self._read()
            changed = self._recover(document, now)
            claimed = None
            for task in document["tasks"]:
                if task["id"] != task_id or not self._is_due(task, now):
                    continue
                task["attempts"] = task.get("attempts", 0) + 1 if task.get("retry_pending") else 1
                task.update(status="RUNNING", claim_token=uuid.uuid4().hex,
                            lease_until=(now.astimezone(dt_timezone.utc) + timedelta(seconds=task.get("lease_seconds", 300))).isoformat(),
                            last_run=now.isoformat(timespec="seconds"))
                claimed = copy.deepcopy(task)
                changed = True
                break
            if changed:
                self._write(document)
            self._remember(document)
            return claimed

    def run_due(self, execute: Callable[[str], Any]) -> list[dict[str, Any]]:
        ran = []
        # Snapshot only ids. Each callback gets its own claim and fresh merge;
        # additions/deletions during execution cannot be lost to stale writes.
        for candidate in self.due():
            claimed = self._claim(candidate["id"])
            if claimed is None:
                continue
            result = None
            error = None
            try:
                result = execute(claimed["command"])
                if result is False:
                    error = "执行返回 false"
                elif isinstance(result, dict) and (result.get("ok") is False or result.get("status") in ("PARTIAL", "FAILED")):
                    error = str(result.get("error") or result.get("summary") or f"执行未成功: {result.get('status') or 'ok:false'}")
            except Exception as exc:  # control signals derived from BaseException propagate.
                error = str(exc) or type(exc).__name__
            cancelled = isinstance(result, dict) and result.get("status") == "CANCELLED"
            if cancelled:
                error = "任务已取消"
            now = _now()
            final = copy.deepcopy(claimed)
            committed = False
            with self._locked():
                document = self._read()
                for task in document["tasks"]:
                    if task["id"] != claimed["id"] or task.get("claim_token") != claimed["claim_token"]:
                        continue
                    if _stamp(_parse_dt(task["lease_until"]), task.get("timezone", "local")) <= now.timestamp():
                        error = "执行租约已过期：拒绝提交过期执行结果"
                        task["lease_expirations"] = task.get("lease_expirations", 0) + 1
                    if cancelled:
                        # Cancellation is terminal even for recurring tasks and
                        # expired leases still owned by this claim. Never retry a
                        # user's cancelled operation through failure recovery.
                        task.update(status="CANCELLED", enabled=False, next_run=None,
                                    retry_pending=False, last_error=None, last_result=result,
                                    last_run=_in_zone(now, task.get("timezone", "local")).isoformat(timespec="seconds"))
                        task.pop("claim_token", None)
                        task.pop("lease_until", None)
                        error = "任务已取消"
                    elif error is not None:
                        self._fail(task, now, error, result)
                    else:
                        task.update(status="SUCCEEDED", last_error=None, last_result=result)
                        task.pop("claim_token", None)
                        task.pop("lease_until", None)
                        self._advance(task, now)
                    final = copy.deepcopy(task)
                    self._write(document)
                    committed = True
                    break
                self._remember(document)
            record = {"task": final, "ok": error is None and committed, "result": result}
            if error is not None:
                record["error"] = error
            elif not committed:
                record["error"] = "任务已删除或租约已变更，未提交过期执行结果"
            ran.append(record)
        return ran

    @property
    def worker_path(self) -> str:
        return self.path + ".worker.json"

    @staticmethod
    def _poll_value(value) -> float:
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
            raise ValueError("poll_seconds 必须为大于0且不超过86400的有限秒数")
        return float(value)

    def _write_worker(self, status: str, poll_seconds: float, worker_id: str) -> None:
        """Atomic sidecar publication; never takes the task-store transaction lock."""
        document = {"pid": os.getpid(), "updated": _now().astimezone(dt_timezone.utc).isoformat(),
                    "status": status, "poll_seconds": poll_seconds, "worker_id": worker_id}
        fd, temporary = tempfile.mkstemp(prefix=".worker-", suffix=".tmp", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(document, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.worker_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def worker_status(self) -> dict[str, Any]:
        """Read freshness of the worker sidecar, without PID/process inspection.

        An executing heartbeat means the scheduler callback is active, not that
        its external device/model is healthy. Missing, invalid, stale or future
        timestamps are never presented as online. PID is diagnostic metadata.
        """
        unknown = {"online": False, "status": "unknown", "updated": None,
                   "age_seconds": None, "stale_after": None, "poll_seconds": None,
                   "pid": None}
        try:
            with open(self.worker_path, "r", encoding="utf-8") as source:
                worker = json.load(source)
            if not isinstance(worker, dict) or worker.get("status") not in ("idle", "executing", "stopped"):
                raise ValueError("无效的worker状态")
            poll = self._poll_value(worker.get("poll_seconds"))
            updated = _parse_dt(worker.get("updated"))
            if updated.tzinfo is None:
                raise ValueError("worker时间戳缺少时区")
            age = _now().timestamp() - updated.timestamp()
            if not math.isfinite(age):
                raise ValueError("worker时间戳无效")
        except FileNotFoundError:
            return dict(unknown, reason="尚无调度执行器心跳")
        except (OSError, ValueError, TypeError, OverflowError):
            return dict(unknown, reason="无法读取有效的调度执行器心跳")
        stale_after = max(15.0, poll * 3)
        result = dict(unknown, updated=worker["updated"], age_seconds=max(0.0, age),
                      stale_after=stale_after, poll_seconds=poll,
                      pid=worker.get("pid"), worker_id=worker.get("worker_id"))
        if worker["status"] == "stopped":
            return dict(result, status="stopped", reason="调度执行器已停止")
        if age < -5:
            return dict(result, reason="调度执行器心跳时间位于未来")
        if age > stale_after:
            return dict(result, reason="调度执行器心跳已过期", last_status=worker["status"])
        return dict(result, online=True, status=worker["status"])

    def loop(self, execute: Callable[[str], Any], poll_seconds: int = 20,
             stop_flag: Callable[[], bool] | None = None) -> None:
        """Run due work with a separate heartbeat, including during long callbacks.

        stop_flag stops the service between callbacks; it does not cancel a
        callback already running. Every exit joins the heartbeat thread and
        publishes stopped. No task-store lock is held during execute or sleep.
        """
        poll = self._poll_value(poll_seconds)
        worker_id = uuid.uuid4().hex
        stop_heartbeat = threading.Event()
        publication_lock = threading.Lock()
        state = {"status": "idle"}
        heartbeat_errors = []

        def publish(status=None):
            with publication_lock:
                if status is not None:
                    state["status"] = status
                self._write_worker(state["status"], poll, worker_id)

        def heartbeat():
            interval = min(5.0, max(0.05, poll / 2))
            while not stop_heartbeat.wait(interval):
                try:
                    publish()
                except Exception as exc:
                    heartbeat_errors.append(exc)
                    stop_heartbeat.set()
                    return

        def monitored_execute(command):
            publish("executing")
            try:
                return execute(command)
            finally:
                publish("idle")

        thread = threading.Thread(target=heartbeat, name=f"scheduler-heartbeat-{worker_id[:8]}", daemon=True)
        started = False
        try:
            publish("idle")
            thread.start()
            started = True
            while True:
                if heartbeat_errors:
                    raise RuntimeError("调度心跳写入失败") from heartbeat_errors[0]
                ran = self.run_due(monitored_execute)
                for record in ran:
                    print(f"[scheduler] 执行任务 {record['task']['id']} ok={record['ok']}: {record['task']['command']}")
                if heartbeat_errors:
                    raise RuntimeError("调度心跳写入失败") from heartbeat_errors[0]
                if stop_flag and stop_flag():
                    return
                deadline = time.monotonic() + poll
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or stop_heartbeat.wait(min(0.25, remaining)):
                        break
                    if stop_flag and stop_flag():
                        return
        finally:
            stop_heartbeat.set()
            if started:
                thread.join()
            publish("stopped")
