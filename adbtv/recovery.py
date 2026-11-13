"""Classified, bounded recovery. Never infer success from recovery itself."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .adb import AdbError
from .execution import checkpoint, current_task
from .llm import LLMError


class Failure(str, Enum):
    CONNECTION = "ADB_DISCONNECTED"
    APP_LAUNCH = "APP_LAUNCH_FAILED"
    PAGE_TIMEOUT = "PAGE_TIMEOUT"
    ELEMENT = "ELEMENT_NOT_FOUND"
    NO_CHANGE = "NO_SCREEN_CHANGE"
    POPUP = "POPUP_BLOCKED"
    SEARCH = "SEARCH_FAILED"
    VIDEO = "VIDEO_LOADING_FAILED"
    MODEL_JSON = "MODEL_JSON_INVALID"
    TOOL = "TOOL_FAILED"


class NoScreenChange(RuntimeError):
    pass


@dataclass
class RecoveryResult:
    failure: str
    strategy: str
    retry: bool
    handoff: bool = False
    detail: str = ""

    def to_dict(self):
        return asdict(self)


def classify(error: Exception | str) -> Failure:
    text = str(error).lower()
    if isinstance(error, NoScreenChange):
        return Failure.NO_CHANGE
    if any(w in text for w in ("offline", "unauthorized", "device not found", "no devices", "connection", "连接", "transport")):
        return Failure.CONNECTION
    if any(w in text for w in ("json", "模型输出", "模型返回")):
        return Failure.MODEL_JSON
    if any(w in text for w in ("启动失败", "activity not found", "launch failed")):
        return Failure.APP_LAUNCH
    if any(w in text for w in ("弹窗", "popup", "dialog")):
        return Failure.POPUP
    if any(w in text for w in ("找不到", "未找到", "not found", "element", "控件")):
        return Failure.ELEMENT
    if any(w in text for w in ("搜索", "search")):
        return Failure.SEARCH
    if any(w in text for w in ("视频", "playback", "buffer", "播放")):
        return Failure.VIDEO
    if isinstance(error, TimeoutError) or "超时" in text or "timeout" in text:
        return Failure.PAGE_TIMEOUT
    return Failure.TOOL


# Public alias for result-dictionary failures that are not raised exceptions.
classify_failure = classify


class RecoveryManager:
    def __init__(self, device, perception, cfg):
        self.device = device
        self.perception = perception
        self.cfg = cfg
        rcfg = cfg.get("recovery", {})
        self.limit = int(rcfg.get("max_attempts", 4))
        self.per_failure = int(rcfg.get("per_failure", 2))
        self.counts: dict[Failure, int] = {}
        self.history: list[dict[str, Any]] = []

    def recover(self, error: Exception | str, *, package="", failure: Failure | None = None):
        checkpoint()
        kind = failure or classify(error)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if len(self.history) >= self.limit or self.counts[kind] > self.per_failure:
            result = RecoveryResult(kind.value, "human_takeover", False, True, str(error)[:400])
            return self._record(result)
        strategy = "reobserve_and_replan"
        try:
            if kind == Failure.CONNECTION:
                strategy = "reconnect"
                self.device.adb.connect(force=True)
                self.device.adb.ensure()
            elif kind == Failure.APP_LAUNCH:
                if not re.fullmatch(r"[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+", package):
                    return self._record(RecoveryResult(kind.value, "human_takeover", False, True, "无有效目标应用"))
                strategy = "restart_target_app"
                self.device.adb.shell(["am", "force-stop", package])
                result = self.device.launch_app(package)
                if not result.get("ok"):
                    raise AdbError("目标应用重启失败")
            else:
                self.perception.invalidate()
                state = self.perception.observe(fresh=True, include_playback=False)
                if kind == Failure.POPUP:
                    # Explicitly harmless dismissal only; never click purchase,
                    # authorization, installation, or account prompts.
                    safe = next((e for e in state.elements if e.text in
                                 ("取消", "暂不", "暂不体验", "稍后", "稍后提醒", "关闭", "Cancel", "Not now")), None)
                    if safe:
                        self.perception.click(safe)
                        strategy = "dismiss_known_popup"
                    else:
                        return self._record(RecoveryResult(kind.value, "human_takeover", False, True, "未找到安全关闭按钮"))
                elif kind in (Failure.PAGE_TIMEOUT, Failure.NO_CHANGE, Failure.SEARCH, Failure.VIDEO) and self.counts[kind] > 1:
                    self.device.key("back")
                    strategy = "back_then_replan"
            self.perception.invalidate()
            return self._record(RecoveryResult(kind.value, strategy, True, detail=str(error)[:400]))
        except Exception as exc:
            return self._record(RecoveryResult(kind.value, strategy, False, True, str(exc)[:400]))

    def _record(self, result):
        self.history.append(result.to_dict())
        task = current_task()
        if task:
            task.recoveries += 1
            task.emit("recovery", **result.to_dict())
        return result
