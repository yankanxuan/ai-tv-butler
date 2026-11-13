"""TV voice control by listening to the TV's own ASR results over adb logcat.

The Xiaomi TV already does wake-word detection, cloud ASR and NLP. Its recognised
text appears in logcat as a structured JSON payload, so the NAS can reuse that
pipeline without installing anything on the TV and without any root access.

Only the *final* recognition result is acted on; intermediate streaming updates
are ignored, and one utterance is handled once (dedup by request id + text).

Nothing here reads raw audio: the TV's microphone never leaves the device.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# The ASR payload is logged on one line, prefixed by this marker.
_ASR_MARKER = "processAsrResult"
# Failure / timeout frames that must never trigger the agent.
_FAILURE_HINTS = (
    "state_result_type=fail",
    "STATE_RESULT_TYPE=fail",
    "CONVERSATION_TIMEOUT",
    "no audio bytes",
    "ASR_ERROR",
    "RecognizeError",
)
_REQUEST_ID = re.compile(r"requestId['\"]?\s*[:=]\s*['\"]?([0-9a-fA-F\-]{8,})")
# `logcat -v epoch` prefixes each line with e.g. "1794378472.982".
_EPOCH = re.compile(r"^\s*(\d{10}(?:\.\d{1,6})?)")


def hash_text(text: str) -> str:
    """Stable short hash used for dedup state and redacted logs (no plaintext)."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


@dataclass
class Utterance:
    text: str
    request_id: str = ""
    is_final: bool = True
    origin: str = ""          # KEY (remote mic button) / VOICE (wake word)
    raw_text: str = ""
    at: float = field(default_factory=time.time)     # 本机收到时间
    source_at: float = 0.0                           # 电视产生该结果的时间（epoch）

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "request_id": self.request_id,
                "is_final": self.is_final, "origin": self.origin,
                "at": self.at, "source_at": self.source_at}

    def redacted(self) -> dict[str, Any]:
        """日志/事件用的脱敏视图：只暴露长度与哈希前缀，不含原文。"""
        return {"text_len": len(self.text), "text_hash": hash_text(self.text)[:12],
                "request_id": self.request_id, "origin": self.origin,
                "at": self.at, "source_at": self.source_at}


def parse_log_line(line: str) -> Utterance | None:
    """Extract a final utterance from one logcat line, or None.

    Returns None for intermediate results, failures, and non-ASR lines.
    """
    if not line or _ASR_MARKER not in line:
        return None
    if any(hint in line for hint in _FAILURE_HINTS):
        return None
    start = line.find("{")
    if start < 0:
        return None
    payload = _extract_json(line[start:])
    if not isinstance(payload, dict):
        return None
    body = payload.get("payload") or {}
    if not isinstance(body, dict) or body.get("is_final") is not True:
        return None
    results = body.get("results") or []
    if not isinstance(results, list):
        return None
    text = ""
    origin_text = ""
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate = (item.get("text") or "").strip()
        if candidate and not text:
            text = candidate
            origin_text = (item.get("origin_text") or "").strip()
    if not text:
        return None
    request_id = ""
    match = _REQUEST_ID.search(line)
    if match:
        request_id = match.group(1)
    source_at = 0.0
    epoch = _EPOCH.match(line)
    if epoch:
        try:
            source_at = float(epoch.group(1))
        except ValueError:
            source_at = 0.0
    origin = ""
    if "wakeupOrigin=KEY" in line or "RecordStartEvent" in line or "STARTSPEAKING" in line:
        origin = "KEY"
    elif "wakeupOrigin=VOICE" in line or "mWakeupWord" in line:
        origin = "VOICE"
    return Utterance(text=text, request_id=request_id, is_final=True,
                     origin=origin, raw_text=origin_text or text,
                     source_at=source_at)


def _extract_json(text: str) -> Any:
    """Balanced-brace JSON extraction from a log line."""
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[:index + 1])
                except ValueError:
                    return None
    return None


@dataclass
class VoiceIntent:
    """Structured reading of one utterance (app + action + object)."""
    app: str = ""
    action: str = ""
    obj: str = ""
    domain: str = ""          # media | home | info | control | unknown
    handle: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"app": self.app, "action": self.action, "obj": self.obj,
                "domain": self.domain, "handle": self.handle, "reason": self.reason}


class VoiceRouter:
    """Decide whether an utterance belongs to the agent or to the native assistant.

    Matching is deliberately *combinatorial* (app + action + object) rather than a
    plain substring test, because substring rules misroute both ways: "关闭客厅
    的灯" contains 关闭 (a media verb) but is home automation, while "打开B站播放
    新闻" contains 新闻 (an info noun) but is clearly media playback.

    Honest boundary: this only decides whether *we* act. It cannot stop the TV's
    own assistant from also acting on the same sentence, so the agent always
    verifies the resulting state afterwards (see the conflict note in the docs).
    """

    MEDIA_APPS = ("b站", "bilibili", "哔哩哔哩", "小电视", "飞牛", "feiniu",
                  "爱奇艺", "奇异果", "腾讯视频", "极光", "优酷", "酷喵",
                  "芒果", "抖音", "鲜时光")
    MEDIA_ACTIONS = ("播放", "放", "看", "观看", "打开", "启动", "进入", "搜索",
                     "搜一下", "搜", "换一部", "换一集", "暂停", "继续", "续播",
                     "下一集", "上一集", "从头", "快进", "快退", "跳到", "选集")
    MEDIA_OBJECTS = ("视频", "短剧", "电影", "电视剧", "剧集", "剧", "综艺", "动漫",
                     "番剧", "纪录片", "直播", "新闻", "音乐", "歌", "相声",
                     "小品", "up主", "影视", "内容", "节目")
    CONTROL_ONLY = ("暂停", "继续", "下一集", "上一集", "从头播", "快进", "快退",
                    "音量", "声音", "静音", "返回", "回到", "主页", "现在播放",
                    "正在播放", "当前应用", "在哪个应用")
    # 这些动词本身已足够表明是媒体操作（"播放某部剧"不需要再说"视频"）。
    STRONG_MEDIA_ACTIONS = ("播放", "放", "看", "观看", "搜索", "搜", "搜一下",
                            "换一部", "换一集", "续播", "从头", "选集", "跳到")
    # "放"也可能是"放在/放到/放进"这类方位动词，不是播放指令。
    _FANG_NON_MEDIA = ("放在", "放到", "放进", "放入", "放下")
    # "打开/进入"类必须同时点名应用或介质，否则可能指向设置/系统页面。
    OPEN_ACTIONS = ("打开", "启动", "进入")
    HOME_DEVICES = ("灯", "台灯", "吊灯", "空调", "窗帘", "插座", "热水器", "扫地机",
                    "扫地机器人", "风扇", "加湿器", "净化器", "门锁", "摄像头",
                    "冰箱", "洗衣机", "电饭煲", "取暖器", "浴霸", "晾衣架")
    INFO_NOUNS = ("天气", "气温", "几点", "时间", "日期", "星期", "闹钟", "日历",
                  "股票", "汇率", "翻译", "计算", "限行", "快递", "话费", "流量",
                  "百科", "是谁", "为什么", "多远", "多少钱", "温度", "湿度",
                  # 闲聊/问答类：由电视助手回答更自然
                  "笑话", "故事", "谜语", "成语", "古诗", "绕口令", "脑筋急转弯")
    # 无法抑制小爱时的已知冲突域：小爱也会执行这些，故只做状态核验。
    CONFLICT_PRONE = ("打开", "关闭", "暂停", "继续", "音量", "播放")

    def __init__(self, cfg: dict[str, Any] | None = None):
        voice = ((cfg or {}).get("voice", {}) or {})
        listener = (voice.get("listener") or {})
        raw_allow = listener.get("allow") or []
        raw_deny = listener.get("deny") or []
        # 用户显式给列表时沿用列表模式（保持既有行为与测试），否则用组合判断。
        self.custom_allow = tuple(raw_allow)
        self.custom_deny = tuple(raw_deny)
        self.use_lists = bool(raw_allow)
        self.prefix_only = tuple(listener.get("prefix_only") or ())
        self.allow = tuple(raw_allow or ())
        self.deny = tuple(raw_deny or self.INFO_NOUNS)

    # ---------- 组合判断 ----------
    # Real remote-microphone capture (b8) showed these prefixes/slips reaching
    # the rules verbatim: "小爱同学播放第一个", "播播放我的世界".
    # Longest first: "小爱小爱" must not be eaten by the shorter "小爱".
    WAKE_WORDS = ("小爱同学", "你好小爱", "小爱小爱", "小爱")

    @staticmethod
    def strip_wake_words(text: str) -> str:
        """Remove repeated leading wake words (longest match first)."""
        clean = text or ""
        while True:
            for wake in VoiceRouter.WAKE_WORDS:
                if clean.startswith(wake):
                    clean = clean[len(wake):].lstrip()
                    break
            else:
                return clean.strip()

    @staticmethod
    def normalize(text: str) -> str:
        """Drop wake words, fillers and an immediately repeated verb."""
        from .intent import _normalize_speech
        import re as _re
        clean = VoiceRouter.strip_wake_words(_normalize_speech((text or "").strip()))
        clean = _re.sub(r'^([播放开看搜找选])\1+', r'\1', clean)
        return _re.sub(r'^(确定|确认|好的|可以|知道|是的)\1+', r'\1', clean)

    def classify(self, text: str) -> VoiceIntent:
        clean = self.normalize(text)
        if not clean:
            return VoiceIntent(domain="unknown", handle=False, reason="empty")
        low = clean.casefold()
        app = next((name for name in self.MEDIA_APPS if name in low), "")
        action = next((word for word in self.MEDIA_ACTIONS if word in clean), "")
        control = next((word for word in self.CONTROL_ONLY if word in clean), "")
        obj_media = next((word for word in self.MEDIA_OBJECTS if word in low), "")
        device = next((word for word in self.HOME_DEVICES if word in low), "")
        info = next((word for word in self.INFO_NOUNS if word in low), "")
        return VoiceIntent(app=app, action=action, obj=obj_media or device or info,
                           domain=self._domain(app, action, control, obj_media, device, info),
                           **self._decide(app, action, control, obj_media, device, info, clean))

    @staticmethod
    def _domain(app, action, control, media_obj, device, info) -> str:
        if device:
            return "home"
        if app or (action and media_obj):
            return "media"
        if control:
            return "control"
        if info:
            return "info"
        return "unknown"

    @classmethod
    def _decide(cls, app, action, control, media_obj, device, info, text_hint=""):
        # 智能家居设备优先让行：即使含"打开/关闭"也不接管。
        if device:
            return {"handle": False, "reason": "denied:home-automation"}
        # 明确点名媒体应用，或"媒体动作 + 媒体对象"，归 Agent。
        if app:
            # 点名媒体应用即由 Agent 负责（"打开B站"、"B站搜新闻"）。
            return {"handle": True, "reason": "media:app"}
        strong = action and any(word in action for word in cls.STRONG_MEDIA_ACTIONS)
        if strong and action == "放" and any(
                phrase in (text_hint or "") for phrase in cls._FANG_NON_MEDIA):
            strong = False
        if strong:
            return {"handle": True, "reason": "media:strong-action"}
        if action and media_obj:
            return {"handle": True, "reason": "media:action+object"}
        if control and not info:
            return {"handle": True, "reason": "media:playback-control"}
        if info:
            return {"handle": False, "reason": "denied:native-assistant-domain"}
        return {"handle": False, "reason": "not-in-allowlist"}

    # ---------- 对外接口 ----------
    def route(self, text: str) -> tuple[bool, str]:
        """Return (should_agent_handle, reason)."""
        from .intent import _normalize_speech
        raw = _normalize_speech((text or "").strip())
        clean = self.normalize(text)
        if self.prefix_only:
            # Authorization looks at the spoken text, not the stripped one: the
            # prefix is the permission token and must survive normalization.
            prefix = next((p for p in sorted(self.prefix_only, key=len, reverse=True)
                           if isinstance(p, str) and raw.startswith(p)), None)
            if prefix is None:
                return False, "not-in-prefix-allowlist"
            clean = self.normalize(raw[len(prefix):])
            if not clean:
                return False, "empty"
        if not clean:
            return False, "empty"
        if self.custom_deny and any(word in clean for word in self.custom_deny):
            return False, "denied:custom"
        if self.use_lists:
            if any(word in clean for word in self.custom_allow):
                return True, "allowlist"
            return False, "not-in-allowlist"
        intent = self.classify(clean)
        return intent.handle, intent.reason

    # 只读查询不改变状态，即使小爱也"回答"过也不会造成双重执行。
    QUERY_MARKERS = ("是什么", "什么", "哪个", "当前", "正在播放", "现在播放")

    @classmethod
    def conflict_prone(cls, text: str) -> bool:
        """True when the TV assistant may also *act* on this sentence."""
        clean = text or ""
        if any(marker in clean for marker in cls.QUERY_MARKERS):
            return False
        return any(word in clean for word in cls.CONFLICT_PRONE)


class VoiceListener:
    """Stream `adb logcat` and dispatch *new* final results to a bounded worker.

    Stability rules:
    - `logcat` is started with `-v epoch` and a `-T` starting point, and every
      line is checked against `stale_seconds`; historical ASR results replayed
      after a restart are therefore ignored instead of being executed.
    - Dedup keys are persisted (hash only, never the transcript) so a restart
      does not re-run the last command.
    - Execution happens on a bounded queue + worker thread, so a long task can
      never block the logcat reader.
    """

    def __init__(self, cfg: dict[str, Any], on_utterance: Callable[[Utterance], Any],
                 *, on_event: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] | None = None,
                 on_interrupt: Callable[[str], Any] | None = None):
        voice = (cfg.get("voice", {}) or {})
        listener = (voice.get("listener") or {})
        self.cfg = cfg
        self.on_utterance = on_utterance
        self.on_event = on_event or (lambda event: None)
        self.serial = (cfg.get("adb", {}) or {}).get("serial") or ""
        self.binary = (cfg.get("adb", {}) or {}).get("binary", "adb") or "adb"
        self.dedup_window = float(listener.get("dedup_seconds", 8))
        self.stale_seconds = float(listener.get("stale_seconds", 10))
        self.backoff_min = float(listener.get("backoff_min", 2))
        self.backoff_max = float(listener.get("backoff_max", 30))
        self.router = VoiceRouter(cfg)
        self._adb_link = None  # reused transport for actual reconnects, not just logcat restarts
        self._adb_reconnects = 0
        self.intelligent = bool((voice.get("intelligence") or {}).get("enabled", False))
        self._clock = clock or time.time
        self._seen: dict[str, float] = {}
        self._state_path = self._resolve_state_path(listener.get("state_file") or "")
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(listener.get("queue_size", 4))))
        self._worker: threading.Thread | None = None
        self._dropped = 0
        self._expired = 0
        self._executed = 0
        self._started_at = 0.0
        self._last_line_at = 0.0
        self._last_utterance_at = 0.0
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        # Interrupts bypass the bounded queue; they must never wait behind a task.
        self.on_interrupt = on_interrupt
        self._interrupts = 0
        self._interrupt_ms: list[float] = []
        self._heartbeat_path = self._state_path.with_name("voice_worker.json")
        # 便于在不读取容器日志的前提下审计路由：只存哈希/长度/结论。
        self._events_path = self._state_path.with_name("voice_events.jsonl")
        self._load_state()

    # ---------- 持久化去重 ----------
    def _resolve_state_path(self, configured: str) -> Path:
        if configured:
            return Path(configured).expanduser()
        from .execution import runtime_dir
        return runtime_dir(self.cfg) / "voice_state.json"

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        keys = data.get("keys") if isinstance(data, dict) else None
        if isinstance(keys, dict):
            now = self._clock()
            for key, stamp in keys.items():
                if not isinstance(key, str) or not isinstance(stamp, (int, float)):
                    continue
                if now - float(stamp) <= max(self.dedup_window, self.stale_seconds) * 4:
                    self._seen[key] = float(stamp)

    def _save_state(self) -> None:
        payload = {"version": 1, "keys": dict(self._seen)}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._state_path)
        except OSError as exc:
            self.on_event({"kind": "state_write_error", "error": str(exc)[:200]})

    def _dedup_key(self, utterance: Utterance) -> str:
        # Prefer the ASR request id; otherwise a hash of the text (never the text).
        return utterance.request_id or "h:" + hash_text(utterance.text)[:32]

    def _is_duplicate(self, utterance: Utterance) -> bool:
        key = self._dedup_key(utterance)
        now = self._clock()
        for old_key, stamp in list(self._seen.items()):
            if now - stamp > self.dedup_window * 4:
                self._seen.pop(old_key, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        self._save_state()
        return False

    # ---------- 过期判断 ----------
    # 设备时钟异常时（例如未来时间），超过该容差即视为不可信时间戳。
    FUTURE_TOLERANCE = 300.0

    def is_stale(self, utterance: Utterance) -> bool:
        """True when the result predates this listener run (history replay).

        Absent or implausible timestamps fall back to "not stale": the caller
        still deduplicates, so replaying one bogus line cannot spam the agent.
        """
        if not self._started_at or not utterance.source_at:
            return False
        if utterance.source_at > self._started_at + self.FUTURE_TOLERANCE:
            return False
        return utterance.source_at < (self._started_at - self.stale_seconds)

    # ---------- 有界队列 ----------
    def _enqueue(self, utterance: Utterance) -> bool:
        """Queue for execution, dropping the oldest pending item when full."""
        while True:
            try:
                self._queue.put_nowait(utterance)
                return True
            except queue.Full:
                try:
                    dropped = self._queue.get_nowait()
                except queue.Empty:
                    continue
                self._dropped += 1
                self.on_event({"kind": "queue_overflow",
                               "dropped": dropped.redacted()})
                # 丢弃的那个"任务"也要结束计数，否则 unfinished_tasks 只增不减。
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
                # loop retries the put; capacity now exists.

    def _drain(self) -> None:
        while True:
            try:
                utterance = self._queue.get_nowait()
            except queue.Empty:
                return
            if self.is_stale(utterance):
                self._expired += 1
                self.on_event({"kind": "stale_dropped", "utterance": utterance.redacted()})
                self._queue.task_done()
                continue
            try:
                self._deliver(utterance)
            except Exception as exc:  # keep the worker alive
                self.on_event({"kind": "handler_error", "error": str(exc)[:300]})
            finally:
                self._queue.task_done()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                utterance = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self.is_stale(utterance):
                    self._expired += 1
                    self.on_event({"kind": "stale_dropped",
                                   "utterance": utterance.redacted()})
                    continue
                self._deliver(utterance)
            except Exception as exc:
                self.on_event({"kind": "handler_error", "error": str(exc)[:300]})
            finally:
                self._queue.task_done()

    def _dispatch_interrupt(self, utterance: Utterance) -> None:
        """Run a priority control on the reader thread: no queue, no model.

        Handlers are expected to be fast (task control file writes or a single
        device key). Exceptions are reported and never block ASR reading.
        """
        started = time.monotonic()
        try:
            outcome = self.on_interrupt(utterance.text)
        except Exception as exc:  # noqa: BLE001
            outcome = None
            self.on_event({"kind": "interrupt_error", "error": str(exc)[:200]})
        elapsed = (time.monotonic() - started) * 1000
        self._interrupts += 1
        self._interrupt_ms.append(elapsed)
        del self._interrupt_ms[:-200]
        summary = outcome.to_dict() if hasattr(outcome, "to_dict") else {"ok": bool(outcome)}
        summary.setdefault("total_ms", round(elapsed, 1))
        self._record_event(utterance, "priority_interrupt", bool(summary.get("ok")),
                           True, decision={"route": "agent", "intent": "interrupt",
                                           "source": "priority_rule"},
                           metrics={"interrupt_ms": round(elapsed, 1)},
                           status=summary.get("status"))
        self.on_event({"kind": "priority_interrupt", "utterance": utterance.redacted(),
                       "outcome": summary})

    def _deliver(self, utterance):
        """Runs only on the bounded worker, never on the logcat reader."""
        outcome = self.on_utterance(utterance)
        self._executed += 1  # Backward-compatible count of completed callbacks.
        if isinstance(outcome, dict) and isinstance(outcome.get("decision"), dict):
            allowed = {"route", "intent", "risk", "confidence", "source", "reason",
                       "llm_ms", "route_ms", "conflict_risk", "requires_confirmation",
                       "native_suppressed"}
            decision = {k: v for k, v in outcome["decision"].items() if k in allowed}
            metrics = {k: v for k, v in (outcome.get("metrics") or {}).items()
                       if k in ("route_ms", "llm_ms", "adb_ms", "observe_ms", "tts_ms", "total_ms")}
            metrics["queue_ms"] = max(0, (self._clock() - utterance.at) * 1000 - float(metrics.get("total_ms") or 0))
            age = utterance.at - utterance.source_at if utterance.source_at else None
            metrics["asr_receive_ms"] = age * 1000 if age is not None and 0 <= age < 300 else None
            self._record_event(utterance, str(decision.get("route", "clarify")),
                               decision.get("route") == "agent", bool(decision.get("conflict_risk")),
                               decision=decision, metrics=metrics, status=outcome.get("status"))
            self.on_event({"kind": "intelligent_route", "utterance": utterance.redacted(),
                           "decision": decision, "metrics": metrics, "status": outcome.get("status")})
        return outcome

    def _record_event(self, utterance: Utterance, route: str, handled: bool,
                      conflict: bool, *, decision=None, metrics=None, status=None) -> None:
        import json as _json
        record = {"at": self._clock(), "source_at": utterance.source_at,
                  "hash": hash_text(utterance.text)[:16],
                  "len": len(utterance.text), "route": route,
                  "handled": handled, "conflict_risk": conflict}
        if decision is not None:
            record.update(decision=decision, metrics=metrics or {}, status=status)
        try:
            self._events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._events_path.exists() and \
                    self._events_path.stat().st_size > 512 * 1024:
                self._events_path.replace(self._events_path.with_suffix(".old"))
            with self._events_path.open("a", encoding="utf-8") as handle:
                handle.write(_json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------- 心跳（容器/健康检查用） ----------
    def _publish_heartbeat(self, status: str | None = None) -> None:
        payload = {"pid": os.getpid(), "updated": self._clock(),
                   "started_at": self._started_at, "stats": self.stats()}
        if status:
            payload["status"] = status
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self._heartbeat_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._heartbeat_path)
        except OSError:
            pass

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(5):
            self._publish_heartbeat("listening")

    @staticmethod
    def worker_status(cfg: dict[str, Any], *, stale_after: float = 30) -> dict[str, Any]:
        """Freshness-based health; never claims online without a recent write."""
        from .execution import runtime_dir
        path = runtime_dir(cfg) / "voice_worker.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"online": False, "status": "unknown", "reason": "尚无语音监听心跳"}
        updated = data.get("updated")
        if not isinstance(updated, (int, float)):
            return {"online": False, "status": "unknown", "reason": "心跳内容无效"}
        age = time.time() - float(updated)
        if data.get("status") == "stopped":
            return {"online": False, "status": "stopped", "age_seconds": round(age, 1),
                    "pid": data.get("pid")}
        online = age <= stale_after
        return {"online": online, "status": data.get("status", "unknown") if online else "stale",
                "age_seconds": round(age, 1), "stale_after": stale_after,
                "pid": data.get("pid"), "stats": data.get("stats")}

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._started_at = self._clock()
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="tv-voice-worker", daemon=True)
        self._worker.start()
        self._heartbeat_stop.clear()
        self._publish_heartbeat("listening")
        self._heartbeat = threading.Thread(target=self._heartbeat_loop,
                                           name="tv-voice-heartbeat", daemon=True)
        self._heartbeat.start()

    def stop(self) -> None:
        self._stop.set()
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None
        self._heartbeat_stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=5)
            self._heartbeat = None
        self._publish_heartbeat("stopped")

    def stats(self) -> dict[str, Any]:
        return {"executed": self._executed, "dropped": self._dropped,
                "expired": self._expired, "queued": self._queue.qsize(),
                "queue_size": self._queue.maxsize,
                "started_at": self._started_at,
                "last_line_at": self._last_line_at,
                "last_utterance_at": self._last_utterance_at,
                "adb_reconnects": self._adb_reconnects,
                "interrupts": self._interrupts,
                "interrupt_p95_ms": (sorted(self._interrupt_ms)[max(0, int(len(self._interrupt_ms) * 0.95) - 1)]
                                     if self._interrupt_ms else None)}

    def health(self) -> dict[str, Any]:
        """Distinguish: listener alive, ADB connected, logs actually flowing."""
        status = self.worker_status(self.cfg)
        adb_ok = False
        try:
            from .adb import AdbClient
            adb_ok = AdbClient(self.cfg).state() == "device"
        except Exception:
            adb_ok = False
        stats = status.get("stats") or {}
        now = time.time()
        last_line = float(stats.get("last_line_at") or 0)
        last_utterance = float(stats.get("last_utterance_at") or 0)
        return {"listener_online": bool(status.get("online")),
                "listener_status": status.get("status"),
                "adb_connected": adb_ok,
                "log_age_seconds": (round(now - last_line, 1) if last_line else None),
                "utterance_age_seconds": (round(now - last_utterance, 1)
                                          if last_utterance else None),
                "stats": stats}

    # ---------- 日志采集 ----------
    def _base_args(self) -> list[str]:
        import shlex
        args = shlex.split(self.binary)
        if self.serial:
            args += ["-s", self.serial]
        return args

    def _logcat_args(self) -> list[str]:
        """Only new lines, with machine-readable epoch timestamps."""
        return self._base_args() + ["logcat", "-v", "epoch", "-T",
                                    time.strftime("%m-%d %H:%M:%S.000",
                                                  time.localtime(self._clock() - 1))]

    def ingest_line(self, line: str) -> bool:
        """Parse/deduplicate/enqueue only; semantic routing runs on the worker."""
        self._last_line_at = time.time()
        utterance = parse_log_line(line)
        if utterance is None:
            return False
        self._last_utterance_at = time.time()
        if self.is_stale(utterance):
            self._expired += 1
            self.on_event({"kind": "history_ignored",
                           "utterance": utterance.redacted()})
            return False
        if self._is_duplicate(utterance):
            self.on_event({"kind": "duplicate_ignored",
                           "utterance": utterance.redacted()})
            return False
        if self.on_interrupt is not None:
            from .voice_interrupt import classify_interrupt
            if classify_interrupt(utterance.text) is not None:
                self._dispatch_interrupt(utterance)
                return True
        if self.intelligent:
            # The reader performs no model calls or conversation access.
            # Contextual answers must not be dropped by legacy substring rules.
            handle, reason = True, "deferred_intelligent_router"
        else:
            handle, reason = self.router.route(utterance.text)
        # 小爱也可能对同一句话执行；我们无法抑制它，只能标记风险，
        # 由 Agent 执行后的状态核验来发现冲突。
        conflict = VoiceRouter.conflict_prone(utterance.text)
        self._record_event(utterance, reason, handle, conflict)
        self.on_event({"kind": "utterance",
                       "utterance": utterance.redacted(),
                       "handled": handle, "route": reason,
                       "conflict_risk": conflict})
        if handle:
            self._enqueue(utterance)
        return bool(handle)

    def _ensure_adb(self) -> bool:
        if not (self.cfg.get("adb") or {}).get("auto_connect", False):
            return True
        from .adb import AdbClient, AdbError
        try:
            if self._adb_link is None:
                self._adb_link = AdbClient(self.cfg)
            before = self._adb_link.state()
            self._adb_link.ensure()
            if before != "device":
                self._adb_reconnects += 1
                self.on_event({"kind": "adb_reconnected", "count": self._adb_reconnects})
            return True
        except (AdbError, OSError):
            # Do not persist endpoint/credential-bearing raw errors.
            self.on_event({"kind": "adb_reconnect_failed"})
            return False

    def run_forever(self) -> None:
        """Read logcat until stopped, reconnecting with bounded backoff."""
        self.start()
        backoff = self.backoff_min
        while not self._stop.is_set():
            if not self._ensure_adb():
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self.backoff_max)
                continue
            # 与 _logcat_args() 保持同一条路径：-v epoch，且从"现在"开始读。
            cmd = self._logcat_args()
            started = time.time()
            try:
                self._process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            except OSError as exc:
                self.on_event({"kind": "listener_error", "error": str(exc)})
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self.backoff_max)
                continue
            self.on_event({"kind": "listener_connected"})
            # Everything already in the buffer is older than this moment, so the
            # staleness filter will reject replayed history regardless of -T.
            self._started_at = started
            backoff = self.backoff_min
            try:
                for line in self._process.stdout:
                    if self._stop.is_set():
                        break
                    self.ingest_line(line)
            finally:
                if self._process is not None:
                    try:
                        self._process.terminate()
                    except OSError:
                        pass
                    try:
                        if self._process.stdout is not None:
                            self._process.stdout.close()
                    except OSError:
                        pass
                    try:
                        self._process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    self._process = None
            if self._stop.is_set():
                break
            self.on_event({"kind": "listener_disconnected"})
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.backoff_max)
        self.stop()
