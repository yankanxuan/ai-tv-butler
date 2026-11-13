"""Authenticated lightweight HTTP API and V3 browser console.

Existing /api/cmd, /api/tool and /api/tasks endpoints are preserved. New task
submission is asynchronous via /api/runs; progress uses SSE, never polling TV
from multiple browser tabs. No arbitrary ADB shell or filesystem write endpoint.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import tempfile
import time
import socket
import uuid
from collections import deque
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from .adb import AdbClient
from .agent import Agent
from .config import load_config
from .device import TVDevice
from .llm import LLMClient
from .scheduler import Scheduler
from .tools import ToolBox
from .idempotency import IdempotencyStore, IdempotencyConflict


def redact_secrets(value):
    """Strip credentials recursively, including provider-specific nested fields."""
    if isinstance(value, dict):
        def sensitive(key):
            name = str(key).lower().replace("-", "_")
            return name != "max_tokens" and any(word in name for word in
                ("key", "token", "secret", "password", "authorization", "credential"))
        return {key: redact_secrets(item) for key, item in value.items() if not sensitive(key)}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        query = redact_secrets(dict(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, urlencode(query), ""))
    return value


def build_context(cfg):
    adb = AdbClient(cfg)
    device = TVDevice(adb, cfg)
    toolbox = ToolBox(device)
    llm = LLMClient(cfg)
    agent = Agent(device, toolbox, llm, cfg)
    sched = Scheduler(cfg["scheduler"]["store"])
    return adb, device, toolbox, agent, sched


def make_server(cfg):
    cfg = copy.deepcopy(cfg)
    from .auth import authorized, load_token
    from .execution import device_lease, list_tasks, control_task, runtime_dir, TaskControl, DeviceBusy, TaskCancelled, TaskTimeout
    # An installation without a source config persists its model overrides here.
    fallback = runtime_dir(cfg) / "config.yaml"
    if cfg.get("_source") in (None, "<defaults>") and fallback.is_file():
        saved = load_config(str(fallback))
        cfg["llm"] = saved["llm"]
        cfg["agent"]["use_llm"] = saved["agent"].get("use_llm", True)
        cfg["_source"] = str(fallback)
    adb, device, toolbox, ctx_agent, sched = build_context(copy.deepcopy(cfg))
    # Reuse this agent/device for voice turns: building per utterance would
    # reconfigure the console and waste a full context each time.
    from .voice_runtime import VoiceDialogue as _VoiceDialogue
    from .voice_input import SharedSessionStore as _SharedStore, VoiceInputRegistry as _Registry
    tv_id = (cfg.get("adb", {}) or {}).get("serial") or (cfg.get("adb", {}) or {}).get("host") or "default-tv"
    _shared = (cfg.get("voice", {}) or {}).get("conversation", {}).get("shared_dir") or ""
    voice_dialogue = None
    if (cfg.get("voice", {}) or {}).get("intelligence", {}).get("enabled", False):
        voice_dialogue = _VoiceDialogue(cfg, agent=ctx_agent, device=device)
    voice_registry = (_Registry(voice_dialogue, store=_SharedStore(_shared) if _shared else None,
                                tv_id=tv_id) if voice_dialogue is not None else None)
    # Exposed for health/introspection and tests; still one instance per console.
    voice_dialogue = voice_dialogue
    token = load_token(cfg)
    idempotency = IdempotencyStore(runtime_dir(cfg) / "idempotency.sqlite3",
                                   runtime_dir(cfg) / "tasks")
    owner = IdempotencyStore.subject(token)
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8770))
    web_root = Path(__file__).resolve().parent / "web"
    index_bytes = (web_root / "index.html").read_bytes()
    # Explicit public bootstrap allowlist; never expose arbitrary web files.
    console_bytes = (web_root / "console.js").read_bytes()
    vendor_root = (web_root / "vendor").resolve()
    context_lock = threading.RLock()
    memory_lock = threading.Lock()
    cfg["_server_instance_id"] = uuid.uuid4().hex
    cfg["_server_owner_pid"] = os.getpid()

    def acquire_instance_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, (cfg["_server_instance_id"] + "\n" + str(os.getpid())).encode())
        os.fsync(fd)
        return fd

    def instance_is_live(instance_id):
        # The lock is held for the whole lifetime of that console process. A
        # reused PID cannot make a dead instance look alive.
        if (not isinstance(instance_id, str) or not instance_id
                or len(instance_id) > 64 or any(not (c.isalnum() or c in '-_') for c in instance_id)):
            return False
        path = runtime_dir(cfg) / "instances" / (instance_id + ".lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    instance_fd = acquire_instance_lock(runtime_dir(cfg) / "instances" /
                                        (cfg["_server_instance_id"] + ".lock"))
    if instance_fd is None:
        raise RuntimeError("Cannot acquire this console instance lease")
    recovery_fd = acquire_instance_lock(runtime_dir(cfg) / "recovery-leader.lock")
    recovery_leader = recovery_fd is not None
    # Only records explicitly owned by a dead console process are reconciled.
    # QUEUED tasks can be re-enqueued safely (the worker sets RUNNING only after
    # taking the task and before any device action). RUNNING/PAUSED is uncertain.
    from .execution import atomic_json
    def mark_interrupted(record, path, summary, *, migration=None):
        updated = dict(record)
        updated.update(state="INTERRUPTED", updated=time.time(),
                       result={"ok": False, "status": "UNKNOWN", "summary": summary})
        if migration is not None:
            updated["migration"] = migration
        atomic_json(path, updated)
        return updated

    recovered_queued = []
    for path in (runtime_dir(cfg) / "tasks").glob("*.json"):
        if path.name.endswith(".control.json"):
            continue
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                continue
            if record.get("state") not in ("QUEUED", "RUNNING", "PAUSED"):
                continue
            # A live original console OR a live recovery owner protects the task.
            # This check is read-only and applies even to non-leaders.
            owners = [record.get("server_instance_id"),
                      record.get("recovery_owner_instance_id")]
            if any(isinstance(value, str) and value and instance_is_live(value)
                   for value in owners):
                continue
            legacy_claim = ("recovery_owner_pid" in record
                            and not record.get("recovery_owner_instance_id"))
            instance_id = record.get("server_instance_id")
            if not isinstance(instance_id, str) or not instance_id:
                if not legacy_claim:
                    continue
            # Only the recovery leader may mutate recovery state. Non-leaders
            # still perform the liveness check above but never mark/migrate.
            if not recovery_leader:
                continue
            if legacy_claim:
                # Old recovery records have only a PID. A PID may be reused
                # (Docker PID 1), and execution before the crash cannot be
                # proven absent, so migrate conservatively without replay.
                mark_interrupted(
                    record, path,
                    "旧格式恢复记录缺少实例租约，无法证明任务尚未执行；已标记 UNKNOWN，未自动重放",
                    migration={"from": "recovery_owner_pid",
                               "original_recovery_owner_pid": record.get("recovery_owner_pid"),
                               "migrated_at": time.time(),
                               "reason": "legacy_recovery_without_instance_lease"})
                continue
            if record.get("state") == "QUEUED":
                recovered_queued.append(record)
            else:
                mark_interrupted(record, path,
                                 "原控制台实例已退出且任务处于执行中；结果未确认，未自动重放")
        except (OSError, ValueError, TypeError):
            continue
    stopped = threading.Event()
    streams = set()
    pending = deque()
    condition = threading.Condition()
    active = {}
    settings = cfg.get("server", {})
    worker_count = int(settings.get("workers", 1))
    queue_size = int(settings.get("queue_size", 16))
    queue_timeout = float(settings.get("queue_timeout", 60))
    if not 1 <= worker_count <= 16 or not 1 <= queue_size <= 10000 or not 0 < queue_timeout <= 86400:
        raise ValueError("无效任务队列配置")

    def finish_waiting(item, state, summary):
        item[0].finish(state, {"ok": False, "status": state, "summary": summary})

    def worker():
        while True:
            with condition:
                condition.wait_for(lambda: stopped.is_set() or pending)
                if stopped.is_set():
                    return
                item = pending.popleft()
                task, snapshot, command, deadline = item
                if time.monotonic() >= deadline:
                    finish_waiting(item, "TIMED_OUT", "任务排队等待超时")
                    del item, task, snapshot, command
                    continue
                active[task.id] = task
            try:
                task_agent = build_context(snapshot)[3]
                if time.monotonic() >= deadline:
                    finish_waiting(item, "TIMED_OUT", "任务排队等待超时")
                    continue
                result = task_agent.run(command, task_id=task.id)
                record = json.loads(task.path.read_text())
                if record.get("state") in ("QUEUED", "RUNNING", "PAUSED"):
                    task.finish("SUCCEEDED" if result.get("ok") else "FAILED", result)
            except (TaskCancelled, TaskTimeout) as signal:
                state = "CANCELLED" if isinstance(signal, TaskCancelled) else "TIMED_OUT"
                task.finish(state, {"ok": False, "status": state, "summary": "任务已取消" if state == "CANCELLED" else "任务执行超时"})
            except Exception:
                task.finish("FAILED", {"ok": False, "status": "FAILED", "summary": "任务执行失败，请检查服务诊断"})
            finally:
                with condition:
                    active.pop(task.id, None)
                    condition.notify_all()
                # Keep only the fixed worker thread, never the completed context.
                task_agent = result = record = None
                del item, task, snapshot, command

    def expire_waiting():
        while not stopped.wait(.1):
            with condition:
                for item in list(pending):
                    if time.monotonic() >= item[3]:
                        pending.remove(item)
                        finish_waiting(item, "TIMED_OUT", "任务排队等待超时")
                item = None

    def submit(command, idempotency_key=None):
        with context_lock:
            snapshot = copy.deepcopy(cfg)
        with condition:
            if stopped.is_set() or len(pending) >= queue_size:
                return None, False
            if idempotency_key is None:
                task = TaskControl(snapshot, command)
                pending.append((task, snapshot, command, time.monotonic() + queue_timeout))
                condition.notify()
                return task.id, False
            created = {}
            def create_task(task_id):
                task = TaskControl(snapshot, command, task_id=task_id)
                created['id'] = task.id
                pending.append((task, snapshot, command, time.monotonic() + queue_timeout))
                return task.id
            try:
                task_id, replay = idempotency.submit(
                    owner, idempotency_key,
                    hashlib.sha256(command.encode('utf-8')).hexdigest(), create_task)
            except Exception:
                # The SQLite transaction rolled back. Remove a partially created
                # task/queue item so it cannot execute without a durable record.
                task_id = created.get('id')
                if task_id:
                    pending[:] = [item for item in pending if item[0].id != task_id]
                    path = runtime_dir(cfg) / "tasks" / (task_id + ".json")
                    path.unlink(missing_ok=True)
                    path.with_suffix(".control.json").unlink(missing_ok=True)
                raise
            if not replay:
                condition.notify()
            return task_id, replay


    def recover_queued():
        """Re-enqueue only tasks that never left QUEUED; never replay RUNNING."""
        if not recovered_queued or not recovery_leader:
            return []
        snapshot = copy.deepcopy(cfg)
        recovered = []
        for record in recovered_queued:
            task_id = record.get("id")
            if not isinstance(task_id, str) or not task_id.isalnum() or len(task_id) > 64:
                continue
            path = runtime_dir(cfg) / "tasks" / (task_id + ".json")
            try:
                current = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if current.get("state") != "QUEUED":
                continue
            prior_instance = current.get("recovery_owner_instance_id")
            if isinstance(prior_instance, str) and prior_instance and instance_is_live(prior_instance):
                continue  # another live console claims this queued task
            if "recovery_owner_pid" in current and not prior_instance:
                mark_interrupted(
                    current, path,
                    "旧格式恢复记录缺少实例租约，无法证明任务尚未执行；已标记 UNKNOWN，未自动重放",
                    migration={"from": "recovery_owner_pid",
                               "original_recovery_owner_pid": current.get("recovery_owner_pid"),
                               "migrated_at": time.time(),
                               "reason": "legacy_recovery_without_instance_lease"})
                continue
            command = current.get("instruction")
            if not isinstance(command, str) or not command.strip():
                current.update(state="INTERRUPTED", updated=time.time(),
                               result={"ok": False, "status": "UNKNOWN",
                                       "summary": "启动恢复时任务指令缺失；未自动重放"})
                atomic_json(path, current)
                continue
            try:
                task = TaskControl(snapshot, command, task_id=task_id)
                raw = json.loads(task.path.read_text())
                raw["recovery_owner_pid"] = os.getpid()
                raw["recovery_owner_instance_id"] = cfg.get("_server_instance_id")
                atomic_json(task.path, raw)
                pending.append((task, snapshot, command, time.monotonic() + queue_timeout))
                idempotency.mark_committed(task_id)
                recovered.append(task.id)
            except Exception:
                try:
                    raw = json.loads(path.read_text())
                    raw.update(state="INTERRUPTED", updated=time.time(),
                               result={"ok": False, "status": "UNKNOWN",
                                       "summary": "启动恢复失败；未自动重放"})
                    atomic_json(path, raw)
                except Exception:
                    pass
        return recovered

    def memory():
        from .memory import MemoryStore
        path = cfg.get("memory", {}).get("path") or runtime_dir(cfg).parent / "memory.sqlite3"
        return MemoryStore(path)

    def skill_engine():
        from .skill_engine import SkillEngine
        return SkillEngine(memory(), cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AITVButler/3.0"

        def _auth(self):
            return authorized(self.headers, token)

        def _vendor(self, name):
            """Serve pinned local assets only; no traversal, no listings."""
            if (not name or len(name) > 64 or not all(c.isalnum() or c in ".-" for c in name)
                    or not name.endswith((".js", ".css", ".txt"))):
                raise ValueError("无效静态资源名")
            target = (vendor_root / name).resolve()
            if target.parent != vendor_root or not target.is_file():
                return self._send(404, {"error": "not found"})
            content_type = ("text/javascript" if name.endswith(".js") else
                            "text/css" if name.endswith(".css") else "text/plain")
            return self._bytes(200, target.read_bytes(), content_type + "; charset=utf-8")

        def _voice_audio(self, name):
            # Cache files are hashed by default, but any safe basename is allowed:
            # containment below is what actually prevents traversal.
            if (not name or not name.endswith(".wav") or len(name) > 96
                    or not all(ch.isalnum() or ch in "._-" for ch in name)):
                raise ValueError("无效语音文件名")
            from .voice_response import VoiceResponder
            directory = VoiceResponder(cfg).tts.cache.resolve()
            target = (directory / name).resolve()
            if target.parent != directory or not target.is_file():
                return self._send(404, {"error": "not found"})
            return self._bytes(200, target.read_bytes(), "audio/wav")

        def _voice_turn(self, body):
            text = body.get("text", "")
            source = body.get("source", "web_microphone")
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise ValueError("text 须为 1..500 字符")
            if source not in ("web_microphone", "external_asr"):
                raise ValueError("source 仅支持 web_microphone/external_asr")
            if voice_registry is None:
                return self._send(409, {"error": "语音 Beta 未启用（voice.intelligence.enabled）"})
            from .voice_input import UnsupportedInput
            try:
                lease = device_lease(cfg)
                lease.__enter__()
            except DeviceBusy:
                # Another task owns the TV: fail fast with a retry hint instead
                # of making the browser wait behind an unrelated task.
                return self._send(409, {"error": "电视正在执行其他任务，请稍后重试或取消该任务"})
            try:
                try:
                    result = voice_registry.submit_text(text, source=source)
                    from .schemas import SchemaError, validate_voice_response
                    try:
                        validate_voice_response(result)
                    except SchemaError as exc:
                        return self._send(500, {"error": f"响应结构非法: {exc}"})
                    return self._send(200, result)
                except UnsupportedInput:
                    # Reserved channel: never report success for unimplemented input.
                    return self._send(501, {"error": "external_asr 尚未接入"})
            finally:
                lease.__exit__(None, None, None)

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, code, data, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError as e:
                raise ValueError("无效 Content-Length") from e
            if n < 0 or n > 65536:
                raise ValueError("请求体超出限制")
            if not n:
                return {}
            value = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON请求必须是对象")
            return value

        def _parts(self):
            parts = urlsplit(self.path)
            if ".." in parts.path or "\\" in parts.path:
                raise ValueError("非法路径")
            return [p for p in parts.path.strip("/").split("/") if p], parse_qs(parts.query)

        def _idempotency_key(self, body):
            header = self.headers.get("X-Idempotency-Key")
            body_key = body.get("idempotency_key")
            if header and body_key and header != body_key:
                raise ValueError("幂等键头与请求体不一致")
            key = header or body_key
            if key is not None:
                IdempotencyStore.validate_key(key)
            return key

        def _idempotency_view(self, record):
            path = runtime_dir(cfg) / "tasks" / (record["task_id"] + ".json")
            task_state = "UNKNOWN" if record.get("state") == "committed" else "PENDING"
            try:
                task_state = json.loads(path.read_text()).get("state") or "UNKNOWN"
            except (OSError, ValueError, TypeError):
                pass
            return {key: value for key, value in record.items() if key != "command_hash"} | {
                "task_state": task_state}

        def do_GET(self):
            try:
                parts, _ = self._parts()
                if parts == ["static", "console.js"]:
                    return self._bytes(200, console_bytes, "text/javascript; charset=utf-8")
                if len(parts) == 2 and parts[0] == "vendor":
                    return self._vendor(parts[1])
                if parts == ["health"]:
                    return self._send(200, {"ok": True})
                if parts == []:
                    return self._bytes(200, index_bytes, "text/html; charset=utf-8")
                if not self._auth():
                    return self._send(401, {"error": "unauthorized"})
                if parts == ["api", "idempotency"]:
                    records = idempotency.list(owner)
                    return self._send(200, {"records": [self._idempotency_view(r) for r in records]})
                if len(parts) == 3 and parts[:2] == ["api", "idempotency"]:
                    record = idempotency.get(owner, parts[2])
                    if record is None:
                        return self._send(404, {"error": "idempotency record not found"})
                    return self._send(200, {"record": self._idempotency_view(record)})
                if parts == ["api", "info"]:
                    with device_lease(cfg):
                        return self._send(200, device.info())
                if parts == ["api", "runs"]:
                    return self._send(200, {"tasks": list_tasks(cfg)})
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    from .execution import runtime_dir
                    task_id = parts[2]
                    if not task_id.isalnum() or len(task_id) > 64:
                        raise ValueError("无效任务id")
                    record = json.loads((runtime_dir(cfg) / "tasks" / (task_id + ".json")).read_text())
                    return self._send(200, record)
                if len(parts) == 4 and parts[:3] == ["api", "voice", "audio"]:
                    return self._voice_audio(parts[3])
                if len(parts) == 4 and parts[:3] == ["api", "voice", "tts"]:
                    responder = voice_dialogue.responder if voice_dialogue else None
                    job = responder.job(parts[3]) if responder else None
                    if job is None:
                        return self._send(404, {"error": "unknown job"})
                    return self._send(200, job)
                if parts == ["api", "voice", "status"]:
                    responder = (voice_dialogue.responder if voice_dialogue
                                 else None)
                    if responder is None:
                        return self._send(200, {"tts_enabled": False, "voice_beta": False,
                                                "tv_audio": False, "delivery": "unavailable"})
                    # Single source of truth: the responder's own status contract.
                    payload = responder.status()
                    payload["voice_beta"] = True
                    return self._send(200, payload)
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "events":
                    return self._events(parts[2])
                if parts == ["api", "memory"]:
                    if not cfg.get("memory", {}).get("enabled", False):
                        return self._send(200, {"enabled": False, "history": []})
                    with memory_lock:
                        store = memory()
                        return self._send(200, {"enabled": True, "history": store.history(100), "stats": store.stats()})
                if parts == ["api", "skills"]:
                    with memory_lock:
                        return self._send(200, {"skills": memory().list_skills()})
                if parts == ["api", "config"]:
                    with context_lock:
                        safe = {"llm": copy.deepcopy(cfg["llm"]),
                            "agent": {"use_llm": cfg["agent"].get("use_llm", True)},
                            "voice": {"enabled": cfg.get("voice", {}).get("enabled", False),
                                       # Explicit, non-secret flag so the page can show
                                       # the voice panel only when the Beta is on.
                                       "intelligence": {
                                           "enabled": bool((cfg.get("voice", {}) or {})
                                                           .get("intelligence", {})
                                                           .get("enabled", False))}},
                            "memory": {"enabled": cfg.get("memory", {}).get("enabled", False)}}
                    return self._send(200, redact_secrets(safe))
                if parts == ["api", "screenshot"]:
                    with device_lease(cfg):
                        data = adb.screenshot_bytes()
                    return self._bytes(200, data, "image/png")
                if parts == ["api", "tasks"]:
                    sched.load()
                    try:
                        executor = sched.worker_status()
                    except Exception:
                        executor = {"online": False, "status": "unknown", "reason": "无法读取调度心跳"}
                    if not isinstance(executor, dict):
                        executor = {"online": False, "status": "unknown", "reason": "调度心跳不可用"}
                    return self._send(200, {"tasks": sched.tasks, "executor": executor})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._error(e)

        def _error(self, error):
            code = (409 if isinstance(error, (DeviceBusy, IdempotencyConflict))
                    else 404 if isinstance(error, FileNotFoundError)
                    else 400 if isinstance(error, (ValueError, KeyError)) else 500)
            message = "not found" if code == 404 else "服务请求失败" if code == 500 else str(error)
            return self._send(code, {"error": message})

        def _events(self, task_id):
            if not task_id.isalnum() or len(task_id) > 64:
                raise ValueError("无效任务id")
            path = runtime_dir(cfg) / "tasks" / (task_id + ".json")
            if not path.is_file():
                return self._send(404, {"error": "task not found"})
            # Snapshots replace the frontend view, so reconnects do not duplicate events.
            with condition:
                if stopped.is_set() or len(streams) >= 32:
                    return self._send(429, {"error": "实时连接已满或服务正在关闭"})
                streams.add(self.connection)
            try:
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                last = None
                heartbeat = time.monotonic()
                for _ in range(1800):
                    if stopped.is_set():
                        break
                    raw = path.read_text()
                    record = json.loads(raw)
                    if raw != last:
                        payload = json.dumps(record, ensure_ascii=False, default=str)
                        self.wfile.write(("data: " + payload + "\n\n").encode())
                        self.wfile.flush()
                        last = raw
                    elif time.monotonic() - heartbeat >= 5:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        heartbeat = time.monotonic()
                    if record.get("state") not in ("QUEUED", "RUNNING", "PAUSED"):
                        break
                    if stopped.wait(.3):
                        break
            except (OSError, ValueError):
                pass
            finally:
                with condition:
                    streams.discard(self.connection)
                    condition.notify_all()

        def do_POST(self):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            try:
                parts, _ = self._parts()
                body = self._body()
                if parts == ["api", "runs"]:
                    command = body.get("command", "")
                    if not isinstance(command, str) or not command.strip() or len(command) > 2000:
                        raise ValueError("command须为1..2000字符")
                    task_id, replay = submit(command, self._idempotency_key(body))
                    if task_id is None:
                        return self._send(429, {"error": "任务队列已满或服务正在关闭，请稍后重试"})
                    return self._send(202, {"task_id": task_id, "idempotent": replay})
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in ("pause", "resume", "cancel"):
                    with condition:
                        for item in list(pending):
                            if item[0].id == parts[2] and parts[3] == "cancel":
                                pending.remove(item)
                                finish_waiting(item, "CANCELLED", "排队任务已取消")
                                return self._send(200, {"id": parts[2], "requested": "cancel"})
                        return self._send(200, control_task(cfg, parts[2], parts[3]))
                if parts == ["api", "voice"]:
                    return self._voice_turn(body)
                if parts == ["api", "cmd"]:
                    command = body.get("command", "")
                    if not isinstance(command, str) or not command.strip() or len(command) > 2000:
                        raise ValueError("command须为1..2000字符")
                    task_id, replay = submit(command, self._idempotency_key(body))
                    if task_id is None:
                        return self._send(429, {"error": "任务队列已满或服务正在关闭，请稍后重试"})
                    path = runtime_dir(cfg) / "tasks" / (task_id + ".json")
                    while not stopped.is_set():
                        record = json.loads(path.read_text())
                        if record.get("state") not in ("QUEUED", "RUNNING", "PAUSED"):
                            return self._send(200, record.get("result") or record)
                        stopped.wait(.1)
                    return self._send(503, {"error": "服务正在关闭"})
                if parts == ["api", "tool"]:
                    with device_lease(cfg):
                        return self._send(200, {"result": toolbox.call(body.get("name"), body.get("args") or {})})
                if parts == ["api", "tasks"]:
                    return self._send(200, {"task": sched.add(command=body["command"], kind=body.get("kind", "once"),
                                      when=body.get("when"), interval=body.get("interval"),
                                      cron=body.get("cron"), delay=body.get("delay"))})
                if parts == ["api", "config"]:
                    with context_lock:
                        return self._save_config(body)
                if len(parts) == 4 and parts[:2] == ["api", "skills"]:
                    skill_id, action = parts[2], parts[3]
                    if not skill_id.isalnum() or len(skill_id) > 64:
                        raise ValueError("无效Skill id")
                    with memory_lock:
                        if action == "validate":
                            return self._send(200, {"skill": skill_engine().validate_in_simulator(skill_id)})
                        if action == "approve":
                            # Explicit interactive confirmation is required by the
                            # UI; an API caller must also opt in intentionally.
                            if body.get("confirm") is not True:
                                raise ValueError("审批需要 confirm:true 的明确确认")
                            return self._send(200, {"skill": skill_engine().approve(skill_id, confirmed=True)})
                        if action == "disable":
                            return self._send(200, {"skill": memory().update_skill(skill_id, status="disabled")})
                    raise ValueError("无效Skill操作")
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._error(e)

        def _save_config(self, body):
            allowed = {"provider", "base_url", "model", "vision_model", "api_key", "use_llm"}
            if set(body) - allowed:
                raise ValueError("模型配置仅允许白名单字段")
            provider = body.get("provider", cfg["llm"]["provider"])
            if provider not in ("anthropic", "openai", "none"):
                raise ValueError("无效模型provider")
            base_url = body.get("base_url", cfg["llm"]["base_url"])
            if not isinstance(base_url, str) or urlsplit(base_url).scheme not in ("http", "https") or not urlsplit(base_url).hostname:
                raise ValueError("模型接口必须为http/https")
            llm = dict(cfg["llm"])
            for key in ("provider", "base_url", "model", "vision_model"):
                if key in body:
                    if not isinstance(body[key], str) or len(body[key]) > 1000 or (key in ("model", "provider", "base_url") and not body[key].strip()):
                        raise ValueError("模型配置字段无效")
                    llm[key] = body[key].strip()
            if "api_key" in body and body["api_key"] != "":
                if not isinstance(body["api_key"], str) or len(body["api_key"]) > 4096:
                    raise ValueError("API Key无效")
                llm["api_key"] = body["api_key"]
            agent_cfg = dict(cfg["agent"])
            if "use_llm" in body:
                if type(body["use_llm"]) is not bool:
                    raise ValueError("use_llm须为布尔值")
                agent_cfg["use_llm"] = body["use_llm"]
            # Preserve unrelated config, aliases and Flow definitions. Update
            # only these fields using YAML's safe dumper. Never return the key.
            import yaml
            from .execution import runtime_dir
            source = cfg.get("_source")
            path = Path(source).expanduser() if source and source != "<defaults>" else runtime_dir(cfg) / "config.yaml"
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            current = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {k: copy.deepcopy(v) for k, v in cfg.items() if not k.startswith("_")}
            if not isinstance(current, dict):
                raise ValueError("配置文件不是对象")
            current["llm"] = llm
            current["agent"] = agent_cfg
            content = yaml.safe_dump(current, allow_unicode=True, sort_keys=False)
            descriptor, temp = tempfile.mkstemp(prefix=".config-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            cfg["llm"] = llm
            cfg["agent"] = agent_cfg
            cfg["_source"] = str(path)
            return self._send(200, {"ok": True})

        def do_DELETE(self):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            try:
                parts, _ = self._parts()
                if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                    return self._send(200, {"removed": sched.remove(parts[2])})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._error(e)

        def log_message(self, fmt, *args):
            pass

    class ConsoleServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            stopped.set()
            with condition:
                for item in pending:
                    finish_waiting(item, "CANCELLED", "控制台正在关闭")
                pending.clear()
                for task_id in active:
                    try:
                        control_task(cfg, task_id, "cancel")
                    except (OSError, ValueError):
                        pass
                condition.notify_all()
            with condition:
                for connection in list(streams):
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                condition.wait_for(lambda: not streams, timeout=5)
            super().server_close()
            for thread in self.workers:
                thread.join(timeout=5)
            if self.scheduler is not None:
                self.scheduler.join(timeout=5)
            for fd in (getattr(self, "recovery_fd", None), getattr(self, "instance_fd", None)):
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            instance_path = runtime_dir(cfg) / "instances" / (cfg["_server_instance_id"] + ".lock")
            try:
                instance_path.unlink(missing_ok=True)
            except OSError:
                pass

    httpd = ConsoleServer((host, port), Handler)
    httpd.instance_fd = instance_fd
    httpd.recovery_fd = recovery_fd
    # Console embedding of the scheduler is opt-in. Production deployments run
    # an independent scheduler service against the same store and device lock;
    # embedding here is convenient for a single-process install.
    httpd.scheduler = None
    if cfg.get("scheduler", {}).get("embedded", False):
        scheduling_agent = build_context(copy.deepcopy(cfg))[3]
        def execute_scheduled(command):
            return scheduling_agent.run(command)
        httpd.scheduler = threading.Thread(
            target=sched.loop, args=(execute_scheduled,),
            kwargs={"poll_seconds": int(cfg.get("scheduler", {}).get("poll_seconds", 20)),
                    "stop_flag": stopped.is_set},
            name="tv-scheduler", daemon=True)
        httpd.scheduler.start()
    # Expose the single dialogue/responder so /api/voice/tts can read job state.
    httpd.voice_dialogue = voice_dialogue
    httpd.streams = streams
    recover_queued()
    httpd.workers = [threading.Thread(target=worker, name="tv-worker-" + str(i), daemon=True) for i in range(worker_count)]
    httpd.workers.append(threading.Thread(target=expire_waiting, name="tv-queue-expiry", daemon=True))
    for thread in httpd.workers:
        thread.start()
    return httpd


def serve(cfg):
    httpd = make_server(cfg)
    host, port = httpd.server_address[:2]
    print(f"[ai-tv] 认证控制台 http://{host}:{port}")
    try:
        httpd.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
