"""电视高层操作：在 adb 之上提供语义化控制与界面解析。"""
from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from .adb import AdbClient, AdbError
from . import apps as appmod

KEYCODES = {
    "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "menu": "KEYCODE_MENU",
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
    "center": "KEYCODE_DPAD_CENTER", "enter": "KEYCODE_ENTER",
    "ok": "KEYCODE_DPAD_CENTER", "select": "KEYCODE_DPAD_CENTER",
    "play": "KEYCODE_MEDIA_PLAY", "pause": "KEYCODE_MEDIA_PAUSE",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "stop": "KEYCODE_MEDIA_STOP",
    "next": "KEYCODE_MEDIA_NEXT", "prev": "KEYCODE_MEDIA_PREVIOUS",
    "search": "KEYCODE_SEARCH", "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
    "del": "KEYCODE_DEL", "backspace": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB", "wakeup": "KEYCODE_WAKEUP",
}


@dataclass
class UIElement:
    text: str = ""
    desc: str = ""
    resource_id: str = ""
    cls: str = ""
    clickable: bool = False
    focused: bool = False
    bounds: str = ""
    x: int = 0
    y: int = 0
    node_id: str = ""
    parent_id: str = ""
    focusable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text, "desc": self.desc, "resource_id": self.resource_id,
            "class": self.cls, "clickable": self.clickable, "focused": self.focused,
            "bounds": self.bounds, "center": [self.x, self.y],
            "node_id": self.node_id, "parent_id": self.parent_id,
            "focusable": self.focusable,
        }

    @property
    def label(self) -> str:
        return (self.text or self.desc or self.resource_id or self.cls).strip()


def _parse_bounds(b: str) -> tuple[int, int]:
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b or "")
    if not m:
        return (0, 0)
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


class TVDevice:
    def __init__(self, adb: AdbClient, cfg: dict[str, Any]):
        self.adb = adb
        self.cfg = cfg

    def _shell_checked(self, command: list[str]):
        """保持 AdbClient.shell 的远端调用形式，并统一检查失败。"""
        result = self.adb.shell(command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AdbError(f"adb {' '.join(command)} 失败({result.returncode}): {detail}")
        return result

    # ---------- 基础信息 ----------
    def info(self) -> dict[str, Any]:
        self.adb.ensure()
        g = self.adb.getprop
        return {
            "serial": self.adb.serial,
            "model": g("ro.product.model"),
            "brand": g("ro.product.brand"),
            "device": g("ro.product.device"),
            "sdk": g("ro.build.version.sdk"),
            "android": g("ro.build.version.release"),
            "resolution": self.adb.shell(["wm", "size"]).out.replace("Physical size:", "").strip(),
        }

    # ---------- 输入 ----------
    def key(self, name: str) -> str:
        code = KEYCODES.get(name.lower())
        if not code:
            if re.fullmatch(r"(KEYCODE_\w+|\d+)", name):
                code = name
            else:
                raise ValueError(f"未知按键: {name}（可用: {', '.join(KEYCODES)}）")
        return self._shell_checked(["input", "keyevent", code]).out

    def tap(self, x: int, y: int) -> str:
        return self._shell_checked(["input", "tap", str(int(x)), str(int(y))]).out

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> str:
        return self.adb.shell(["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]).out

    def input_text(self, text: str) -> dict[str, Any]:
        """ASCII is shell-quoted by AdbClient; Unicode needs a working, verified IME.

        A broadcast acknowledgement alone is never evidence that text arrived.
        IME restoration is necessary cleanup even when the task was cancelled.
        """
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError("输入文字须为不含NUL的字符串")
        if not text:
            return {"ok": True, "method": "noop"}
        if all(ord(c) < 128 for c in text):
            # shlex.join already quotes the remote argument: escaping '&' here
            # would type a literal backslash. Android input still needs %s spaces.
            r = self.adb.shell(["input", "text", text.replace(" ", "%s")])
            return {"ok": r.returncode == 0, "method": "input_text",
                    **({"error": r.stderr} if r.returncode else {})}

        import sys
        import time
        from .execution import checkpoint

        timeout = self.cfg.get("ui", {}).get("input_timeout", 2)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("ui.input_timeout须为非负有限秒数")
        timeout = min(float(timeout), 2.)
        component_pattern = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+/(?:\.)?[A-Za-z_$][A-Za-z0-9_.$]*"

        def component(value):
            value = value.strip()
            if not re.fullmatch(component_pattern, value):
                raise AdbError("无法安全识别当前输入法组件，未发送中文输入")
            return value

        def same_ime(left, right):
            def canonical(value):
                package, service = component(value).split("/", 1)
                return package, package + service if service.startswith(".") else service
            return canonical(left) == canonical(right)

        def current_ime():
            return component(self._shell_checked(["settings", "get", "secure", "default_input_method"]).stdout)

        original = current_ime()
        available = []
        for args in (["ime", "list", "-a", "-s"], ["ime", "list", "-s"]):
            response = self.adb.shell(args)
            if response.returncode == 0:
                available = [line.strip() for line in response.stdout.splitlines()
                             if re.fullmatch(component_pattern, line.strip())]
            if available:
                break
        candidates = [name for name in available if name in (
            "com.android.adbkeyboard/.AdbIME", "com.android.adbkeyboard/com.android.adbkeyboard.AdbIME")]
        if not candidates:
            raise AdbError("未发现可用ADBKeyboard输入法；中文未发送，请先安装受信任的ADBKeyboard")
        target = original if any(same_ime(original, candidate) for candidate in candidates) else candidates[0]
        restore = False
        result = {"ok": False, "method": "adbkeyboard_b64", "acknowledged": False,
                  "verified": False, "error": "广播后未能确认当前输入框文字"}
        try:
            if original != target:
                self._shell_checked(["ime", "enable", component(target)])
                # Mark before set: a failed/ interrupted response can still have
                # changed the device, so restore rather than assuming no effect.
                restore = True
                self._shell_checked(["ime", "set", component(target)])
                if not same_ime(current_ime(), target):
                    raise AdbError("ADBKeyboard未成为当前输入法，未发送中文输入")
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            response = self.adb.shell(["am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", payload])
            output = response.stdout + response.stderr
            if response.returncode or re.search(r"permission denial|exception|\berror\b", output, re.I):
                raise AdbError("ADBKeyboard输入广播失败，未确认文字送达")
            result["acknowledged"] = "Broadcast completed" in output
            deadline = time.monotonic() + timeout
            for attempt in range(3):
                checkpoint()
                try:
                    editors = [element for element in self.elements()
                               if element.cls.rsplit(".", 1)[-1] == "EditText"]
                    focused = [element for element in editors if element.focused]
                    current = focused[0] if len(focused) == 1 else editors[0] if not focused and len(editors) == 1 else None
                    if current is not None and text in current.text:
                        result.update(ok=True, verified=True)
                        result.pop("error", None)
                        break
                except AdbError:
                    # Transient dump failures may be retried; cancellation is a
                    # BaseException and must propagate to the managed executor.
                    pass
                remaining = deadline - time.monotonic()
                if attempt == 2 or remaining <= 0:
                    break
                time.sleep(min(.15, remaining))
        finally:
            if restore:
                pending = sys.exc_info()[1]
                # AdbClient's flag is thread-local. Suppress only this cleanup's
                # checkpoint, without changing the shared callback or task state.
                state = getattr(self.adb, "_checkpoint_state", None)
                previous = getattr(state, "active", False) if state is not None else False
                if state is not None:
                    state.active = True
                try:
                    self._shell_checked(["ime", "set", component(original)])
                    if not same_ime(current_ime(), original):
                        raise AdbError("恢复原输入法未生效")
                except Exception as error:
                    if pending is None:
                        raise AdbError("中文输入结束后恢复原输入法失败，请检查输入法设置") from error
                    if hasattr(pending, "add_note"):
                        pending.add_note("清理时恢复原输入法失败，请检查输入法设置")
                finally:
                    if state is not None:
                        state.active = previous
        return result

    # ---------- 界面解析 ----------
    @staticmethod
    def _parse_ui(xml: str) -> list[UIElement]:
        """所有节点共享同一解析，ID 仅在本次快照内有效。"""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise AdbError(f"UI XML 解析失败: {exc}") from exc
        if root.tag not in ("hierarchy", "node"):
            raise AdbError(f"无效 UI XML 根节点: {root.tag}")
        elements: list[UIElement] = []

        def visit(node: ET.Element, parent_id: str = "") -> None:
            if node.tag == "node":
                a = node.attrib
                node_id = str(len(elements))
                x, y = _parse_bounds(a.get("bounds", ""))
                elements.append(UIElement(
                    text=(a.get("text") or "").strip(),
                    desc=(a.get("content-desc") or "").strip(),
                    resource_id=a.get("resource-id", ""), cls=a.get("class", ""),
                    clickable=a.get("clickable") == "true",
                    focused=a.get("focused") == "true",
                    focusable=a.get("focusable") == "true",
                    bounds=a.get("bounds", ""), x=x, y=y,
                    node_id=node_id, parent_id=parent_id,
                ))
                parent_id = node_id
            for child in node:
                visit(child, parent_id)

        visit(root)
        return elements

    def capture_ui(self) -> list[UIElement]:
        """重新采样一次 UI；失败抛 AdbError，不读取或复用旧快照。"""
        result = self._shell_checked(["uiautomator", "dump", "/sdcard/ai_tv_ui.xml"])
        message = result.stdout + result.stderr
        if "dumped to" not in message.lower() or re.search(
                r"\b(?:error|exception|failed)\b", message, re.IGNORECASE):
            raise AdbError(f"UI dump 未成功生成快照: {message.strip()}")
        xml = self._shell_checked(["cat", "/sdcard/ai_tv_ui.xml"]).stdout
        return self._parse_ui(xml)

    def ui_dump(self, simplify: bool = True) -> dict[str, Any]:
        """返回新采样的文本与元素；精简输出优先保留焦点和有文本的节点。"""
        elements = self.capture_ui()
        texts = [text for e in elements for text in (e.text, e.desc) if text]
        visible = elements
        if simplify:
            visible = sorted(elements, key=lambda e: (
                not e.focused, not bool(e.text or e.desc), not e.focusable,
                not e.clickable, not bool(e.resource_id),
            ))[:60]
        return {"count": len(elements), "texts": texts,
                "elements": [e.to_dict() for e in visible]}

    def elements(self) -> list[UIElement]:
        """每次调用只重新采样一次，不缓存 UIElement。"""
        return self.capture_ui()

    def find(self, keyword: str | None = None, cls_contains: str | None = None,
             clickable: bool | None = None) -> list[UIElement]:
        kw = (keyword or "").lower()
        res = []
        for e in self.elements():
            if kw:
                hay = f"{e.text}\n{e.desc}\n{e.resource_id}".lower()
                if kw not in hay:
                    continue
            if cls_contains and cls_contains.lower() not in e.cls.lower():
                continue
            if clickable is not None and e.clickable != clickable:
                continue
            res.append(e)
        return res

    def current_focus(self) -> str:
        r = self._shell_checked(["dumpsys", "window"])
        for line in r.stdout.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                return line.strip()
        return ""

    def current_package(self) -> str:
        focus = self.current_focus()
        m = re.search(r"([a-zA-Z][\w.]+)/[\w.$]+", focus)
        return m.group(1) if m else ""

    def home_package(self) -> str:
        cached = getattr(self, "_home_package_cache", "")
        if cached:
            return cached
        # Different Android builds accept different resolve variants; try both
        # and only cache a real package. Never cache a failed probe.
        for extra in (["-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
                      ["-c", "android.intent.category.HOME"]):
            result = self.adb.shell(["cmd", "package", "resolve-activity", "--brief", *extra])
            match = re.search(r"([A-Za-z][\w.]+)/[\w.$]+", result.stdout or "")
            if result.returncode == 0 and match:
                self._home_package_cache = match.group(1)
                return self._home_package_cache
        return ""

    # Player packages that accept a plain VIEW intent for http(s) video.
    VIDEO_PLAYERS = (
        ("com.mitv.mivideoplayer", "com.mitv.videoplayer.VideoPlayerActivity"),
    )

    def launch_video(self, url: str) -> dict[str, Any]:
        """Hand a video URL to a TV player that supports VIEW + http + video/*."""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("播放地址必须是 http(s) URL")
        last_error = ""
        for package, activity in self.VIDEO_PLAYERS:
            result = self.adb.shell(["am", "start", "-a", "android.intent.action.VIEW",
                                     "-d", url, "-t", "video/mp4", "-n",
                                     f"{package}/{activity}"])
            output = (result.stdout + result.stderr).strip()
            if result.returncode == 0 and "Error" not in output:
                return {"ok": True, "package": package, "activity": f"{package}/{activity}",
                        "method": "VIEW intent", "output": output[:200]}
            last_error = output[:200]
        return {"ok": False, "summary": f"没有可用的电视播放器接受该地址：{last_error}"}

    def set_volume(self, level: int) -> dict[str, Any]:
        """Set the media volume stream to an absolute index.

        Some Android 9 builds accept `media volume --set N`; others clamp or
        ignore it, so the caller must verify the resulting index instead of
        trusting the exit code.
        """
        if type(level) is not int or level < 0:
            raise ValueError("目标音量必须是非负整数")
        result = self.adb.shell(["media", "volume", "--stream", "3", "--set", str(level)])
        text = (result.stdout + "\n" + result.stderr).strip()
        after = self.volume()
        if result.returncode != 0:
            return {"ok": False, "error": text[:200], "after": after,
                    "requested": level}
        return {"ok": True, "requested": level, "after": after, "output": text[:120]}

    def power_state(self) -> dict[str, Any]:
        """Report wakefulness/screen state (Xiaomi TVs sleep after inactivity)."""
        try:
            result = self.adb.shell(["dumpsys", "power"], timeout=4)
        except Exception as exc:      # 电源查询失败不能拖垮整个任务
            return {"wakefulness": "", "screen": "", "asleep": False,
                    "known": False, "error": str(exc)[:120]}
        wake = ""
        screen = ""
        for line in result.stdout.splitlines():
            if "mWakefulness=" in line and not wake:
                wake = line.split("mWakefulness=", 1)[1].strip()
            if "Display Power" in line and "state=" in line and not screen:
                screen = line.split("state=", 1)[1].strip()
        asleep = wake.lower() in ("asleep", "dozing", "dreaming") or screen.upper() == "OFF"
        return {"wakefulness": wake, "screen": screen, "asleep": asleep,
                "known": bool(wake or screen)}

    # 小米电视实测：KEYCODE_WAKEUP 无效，KEYCODE_POWER 才能唤醒。
    WAKE_KEYS = ("power", "wakeup")

    def wake(self, timeout: float = 10.0) -> dict[str, Any]:
        """Bring the TV out of standby; a no-op when already awake.

        Safe by construction: keys are sent only while the device reports
        asleep, so an awake TV never receives a POWER toggle.
        """
        import time as _time
        state = self.power_state()
        if not state["known"]:
            # 状态未知时不盲发电源键：可能把正在播放的电视关掉。
            return {"ok": True, "woke": False, "summary": "电源状态未知，未发送唤醒键", **state}
        if not state["asleep"]:
            return {"ok": True, "woke": False, **state}
        for key in self.WAKE_KEYS:
            self.key(key)
            end = _time.monotonic() + timeout / len(self.WAKE_KEYS)
            while _time.monotonic() < end:
                state = self.power_state()
                if not state["asleep"]:
                    return {"ok": True, "woke": True, "method": key, **state}
                _time.sleep(0.4)
        return {"ok": False, "woke": True, "summary": "电视未在超时内唤醒", **state}

    def volume(self) -> dict[str, Any]:
        result = self.adb.shell(["media", "volume", "--stream", "3", "--get"])
        text = result.stdout + "\n" + result.stderr
        match = re.search(r"volume\s+is\s+(\d+)\s+in\s+range\s*\[(\d+)\s*\.\.\s*(\d+)\]", text, re.I)
        if result.returncode or not match:
            return {"known": False, "stream": 3}
        value, minimum, maximum = map(int, match.groups())
        return {"known": True, "stream": 3, "index": value, "min": minimum, "max": maximum}

    # ---------- 有能力声明和实际观测支持的播放控制 ----------
    @staticmethod
    def _control_result(action, *, method="", reason="", **details):
        """An input acknowledgement is deliberately distinct from goal verification."""
        return {"ok": not bool(reason), "action": action,
                "status": "UNSUPPORTED" if reason else "ACKNOWLEDGED",
                "acknowledged": not bool(reason), "method": method,
                **({"reason": reason, "error": reason} if reason else {}), **details}

    @staticmethod
    def _player_scope(status):
        package = status.get("package", "")
        activity = status.get("activity", "")
        if not isinstance(package, str) or not isinstance(activity, str) or not package:
            return None
        component = re.search(r"([A-Za-z][\w.]+)/([\w.$]+)", activity)
        if component:
            if component.group(1) != package:
                return None
            activity = component.group(2)
        if not re.search(r"(?:player|playback|video)activity$", activity.rsplit(".", 1)[-1], re.I):
            return None
        if activity.startswith("."):
            activity = package + activity
        elif "." not in activity:
            activity = package + "." + activity
        return package, activity

    def transport(self, action: str, observed: dict | None = None) -> dict:
        """Pause/resume a positively identified foreground player, then verify outside.

        Reusing an observation avoids another device read. Callers must invalidate
        it after an action; a second invocation needs a new observation for a no-op.
        """
        if action not in ("pause", "resume"):
            raise ValueError("transport只支持pause/resume")
        # Transport never needs the UI tree; dumping it while video renders
        # is both slow and unreliable.
        status = self.playback_status(include_ui=False) if observed is None else observed
        if not isinstance(status, dict) or not self._player_scope(status):
            return self._control_result(action, reason="前台页面不是已确认的播放器")
        state = str(status.get("state") or "").upper()
        # Absence of audio/MediaSession never means PAUSED. Only positive facts
        # may fill an absent state; an explicit UNKNOWN or conflicting state stays unknown.
        evidence = status.get("evidence") or {}
        if not state and isinstance(evidence, dict):
            if evidence.get("media_session_playing") or evidence.get("audio_track_started") or evidence.get("ui_playing"):
                state = "PLAYING"
            elif evidence.get("audio_track_paused"):
                state = "PAUSED"
        if state not in ("PLAYING", "PAUSED"):
            return self._control_result(action, reason="播放状态未知或尚未稳定，未发送控制键")
        desired = "PAUSED" if action == "pause" else "PLAYING"
        if state == desired:
            return self._control_result(action, method="noop", state_before=state, changed=False)
        # Players differ in which transport key they honour. Feiniu and the
        # Xiaomi video player toggle with DPAD_CENTER (measured on device); other
        # players use explicit media keys.
        center_players = ("com.trim.tv", "com.mitv.mivideoplayer")
        key = "center" if status["package"] in center_players else ("pause" if action == "pause" else "play")
        self.key(key)
        return self._control_result(action, method="key", key=key, state_before=state, changed=True)

    def skip_episode(self, direction: str, observed: dict | None = None) -> dict:
        """Dispatch next/previous only when the owned native session advertises it."""
        if direction not in ("next", "prev", "previous"):
            raise ValueError("换集方向只支持next/prev/previous")
        action = "next" if direction == "next" else "previous"
        status = self.playback_status(include_ui=False) if observed is None else observed
        if not isinstance(status, dict) or not self._player_scope(status):
            return self._control_result(action, reason="前台页面不是已确认的播放器")
        if str(status.get("state") or "").upper() not in ("PLAYING", "PAUSED"):
            return self._control_result(action, reason="原生播放器状态未知或未就绪，未执行换集")
        actions = status.get("media_actions")
        required = 32 if action == "next" else 16  # PlaybackState.ACTION_SKIP_TO_*
        if (status.get("media_session_registered") is not True or type(actions) is not int
                or not actions & required):
            return self._control_result(action, reason="当前播放器未声明可用的原生换集能力")
        key = "next" if action == "next" else "prev"
        self.key(key)
        return self._control_result(action, method="key", key=key, changed=True)

    @staticmethod
    def _session_details(text: str, package: str) -> dict:
        """Select one explicitly owned session; never borrow a foreign/unowned one."""
        records, current = [], None
        for line in text.splitlines():
            header = re.match(r"\s*([\w]+(?:\.[\w]+)+)/[^\s]+", line)
            if not header and "SessionRecord{" in line:
                header = re.search(r"\b([\w]+(?:\.[\w]+)+)/[^\s}]+", line)
            explicit = re.search(r"\bpackage(?:Name)?\s*[=:]\s*([\w.]+)", line)
            owner = (explicit or header).group(1) if explicit or header else ""
            if header or (explicit and (current is None or current["package"] != owner)):
                current = {"package": owner}
                records.append(current)
            elif re.match(r"\s*(?:SessionRecord\{|Session \S+)", line):
                current = None
            if current is None:
                continue
            state = re.search(r"\bstate\s*[=:]\s*(?:PlaybackState\s*\{\s*state\s*=\s*)?(\d+|PLAYING|PAUSED)\b", line)
            if state:
                current["state"] = state.group(1)
            actions = re.search(r"\bactions\s*[=:]\s*(0x[0-9a-fA-F]+|\d+)\b", line)
            if actions:
                current["media_actions"] = int(actions.group(1), 16 if actions.group(1).startswith("0x") else 10)
            if "PlaybackState" in line:
                position = re.search(r"(?<!buffered )\bposition\s*[=:]\s*(\d+)\b", line)
                if position:
                    current["position_ms"] = int(position.group(1))
            duration = re.search(r"(?:android\.media\.metadata\.DURATION|\bduration)\s*[=:]\s*(\d+)\b", line, re.I)
            if duration:
                current["duration_ms"] = int(duration.group(1))
        owned = [record for record in records if record["package"] == package]
        playing = [record for record in owned if record.get("state") in ("3", "PLAYING")]
        selected = playing[0] if len(playing) == 1 else owned[0] if len(owned) == 1 else None
        if selected is None:
            return {}
        return {"media_session_registered": True, **{key: value for key, value in selected.items()
                if key in ("media_actions", "position_ms", "duration_ms")}}

    @staticmethod
    def _timeline(playback, elements):
        def seconds(key):
            for name, divisor in ((key + "_ms", 1000), (key + "_seconds", 1)):
                value = playback.get(name)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    return value / divisor
            return None
        position, duration = seconds("position"), seconds("duration")
        if position is not None and duration is not None and 0 < duration and position <= duration:
            return position, duration, "media"
        pairs = set()
        for element in elements:
            for text in (element.text, element.desc):
                match = re.search(r"(?<![\d:])(\d{1,3}:[0-5]\d(?::[0-5]\d)?)\s*/\s*(\d{1,3}:[0-5]\d(?::[0-5]\d)?)(?![\d:])", text or "")
                if match:
                    values = []
                    for clock in match.groups():
                        total = 0
                        for component in clock.split(":"):
                            total = total * 60 + int(component)
                        values.append(total)
                    if 0 <= values[0] <= values[1] and values[1] > 0:
                        pairs.add(tuple(values))
        if len(pairs) == 1:
            position, duration = pairs.pop()
            return position, duration, "player_xml"
        return None

    @staticmethod
    def _seekbar(elements):
        candidates = [element for element in elements
                      if "seekbar" in element.cls.lower()
                      and element.resource_id
                      and re.search(r"seek|progress|timeline|timebar|scrub", element.resource_id, re.I)
                      and not re.search(r"volume|brightness", element.resource_id, re.I)]
        if len(candidates) != 1:
            return None
        element = candidates[0]
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", element.bounds or "")
        if not bounds:
            return None
        x1, y1, x2, y2 = map(int, bounds.groups())
        if not (x2 - x1 >= 2 and 0 < y2 - y1 < x2 - x1):
            return None
        return element, (x1, y1, x2, y2)

    def seek_to(self, seconds: float, observation=None) -> dict:
        """Map seconds onto a real visible timeline; refuse unsupported guessing.

        ScreenState, its serialized dict, or a playback dict with elements are
        accepted. The fresh UI must keep the same unique element and bounds.
        The result acknowledges only the tap; actual progress must be reobserved.
        """
        if type(seconds) not in (int, float) or not math.isfinite(seconds):
            raise ValueError("跳转秒数须为有限数字")
        if observation is None:
            playback = self.playback_status(include_ui=False)
            raw_elements = self.capture_ui() if self._player_scope(playback) else []
        else:
            data = observation if isinstance(observation, dict) else observation.to_dict()
            playback = dict(data.get("playback", data))
            playback.setdefault("package", data.get("app", data.get("package", "")))
            playback.setdefault("activity", data.get("activity", ""))
            raw_elements = data.get("elements", [])
        elements = []
        for element in raw_elements:
            if isinstance(element, UIElement):
                elements.append(element)
            elif isinstance(element, dict):
                elements.append(UIElement(text=element.get("text", ""), desc=element.get("desc", ""),
                    resource_id=element.get("resource_id", ""), cls=element.get("cls", element.get("class", "")),
                    bounds=element.get("bounds", "")))
        scope = self._player_scope(playback)
        timeline = self._timeline(playback, elements)
        candidate = self._seekbar(elements)
        if not scope or not timeline or not candidate:
            return self._control_result("seek_to", reason="无可验证的播放器时间线和可见SeekBar，未执行跳转")
        element, bounds = candidate
        focus = self.current_focus()
        if self._player_scope({"package": scope[0], "activity": focus}) != scope:
            return self._control_result("seek_to", reason="播放器前台归属已变化，未执行跳转")
        fresh_elements = self.capture_ui()
        fresh_candidate = self._seekbar(fresh_elements)
        if not fresh_candidate:
            return self._control_result("seek_to", reason="SeekBar已消失或不唯一，未执行跳转")
        fresh, fresh_bounds = fresh_candidate
        if ((fresh.resource_id, fresh.cls, fresh.text, fresh.desc, fresh_bounds)
                != (element.resource_id, element.cls, element.text, element.desc, bounds)):
            return self._control_result("seek_to", reason="SeekBar身份或坐标已变化，未执行跳转")
        fresh_playback = playback
        if timeline[2] == "media":
            fresh_playback = self.playback_status(include_ui=False, expected_package=scope[0], focus=focus)
            if self._player_scope(fresh_playback) != scope:
                return self._control_result("seek_to", reason="媒体归属已变化，未执行跳转")
        fresh_timeline = self._timeline(fresh_playback if timeline[2] == "media" else {}, fresh_elements)
        if not fresh_timeline or abs(fresh_timeline[1] - timeline[1]) > .5:
            return self._control_result("seek_to", reason="时间线时长已变化或无法重新确认，未执行跳转")
        if self._player_scope({"package": scope[0], "activity": self.current_focus()}) != scope:
            return self._control_result("seek_to", reason="点击前播放器已变化，未执行跳转")
        position, duration, source = fresh_timeline
        target = max(0., min(float(seconds), duration))
        x1, y1, x2, y2 = fresh_bounds
        x, y = x1 + round(target / duration * (x2 - x1 - 1)), (y1 + y2) // 2
        self.tap(x, y)
        return self._control_result("seek_to", method="seekbar", requested_seconds=seconds,
            target_seconds=target, position_seconds=position, duration_seconds=duration,
            source=source, x=x, y=y, changed=True)

    # ---------- 应用 ----------
    def launch_app(self, name_or_pkg: str) -> dict[str, Any]:
        pkg = appmod.resolve_package(self.adb, name_or_pkg, self.cfg)
        act = appmod.resolve_launcher_activity(self.adb, pkg)
        if act:
            r = self.adb.shell(["am", "start", "-n", act])
            method = f"am start -n {act}"
            if "Error" in r.stdout or r.returncode != 0:
                pass
            else:
                return {"ok": True, "package": pkg, "activity": act, "method": method}
        # 退回 monkey
        r = self.adb.shell(["monkey", "-p", pkg, "-c",
                            "android.intent.category.LAUNCHER", "1"])
        ok = "Events injected: 1" in r.stdout or r.returncode == 0
        return {"ok": ok, "package": pkg, "activity": act, "method": "monkey"}

    def list_apps(self) -> dict[str, list[str]]:
        return appmod.list_packages(self.adb)

    # ---------- 截图 ----------
    def screenshot(self, path: str = "") -> dict[str, Any]:
        data = self.adb.screenshot_bytes()
        if not path:
            import time
            path = f"logs/shot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return {"path": path, "bytes": len(data)}

    # ---------- 等待 / 播放验证 ----------
    def wait_for_text(self, text: str, timeout: int | None = None) -> bool:
        import time
        timeout = timeout or int(self.cfg.get("ui", {}).get("wait_timeout", 20))
        interval = float(self.cfg.get("ui", {}).get("poll_interval", 1.2))
        end = time.time() + timeout
        needle = text.lower()
        while time.time() < end:
            dump = self.ui_dump()
            joined = "\n".join(dump["texts"]).lower()
            if needle in joined:
                return True
            time.sleep(interval)
        return False

    @staticmethod
    def _media_states(text: str, package: str,
                      allow_unowned: bool = True) -> list[int]:
        """读取 session 自身的包名，避免别的应用的 PLAYING 污染结果。

        老 Android / fake_adb 未输出包名时才允许使用无归属状态。
        """
        owner = ""
        records: list[tuple[str, int]] = []
        has_owner = False
        names = {"NONE": 0, "STOPPED": 1, "PAUSED": 2, "PLAYING": 3,
                 "FAST_FORWARDING": 4, "REWINDING": 5, "BUFFERING": 6,
                 "ERROR": 7, "CONNECTING": 8}
        for line in text.splitlines():
            # 常见格式：com.example/SessionTag (userId=0)，或 package=com.example。
            header = re.match(r"\s*([\w]+(?:\.[\w]+)+)/[^\s]+", line)
            if not header and "SessionRecord{" in line:
                header = re.search(r"\b([\w]+(?:\.[\w]+)+)/[^\s}]+", line)
            explicit = re.search(r"\bpackage(?:Name)?\s*[=:]\s*([\w.]+)", line)
            if header or explicit:
                owner = (explicit or header).group(1)
                has_owner = True
            # 单个 SessionRecord 头标志新会话，不能继承上一会话包名。
            elif re.match(r"\s*(?:SessionRecord\{|Session \S+)", line):
                owner = ""
            match = re.search(
                r"\bstate\s*[=:]\s*(?:PlaybackState\s*\{\s*state\s*=\s*)?"
                r"(\d+|[A-Z_]+)\b", line,
            )
            if match:
                raw = match.group(1)
                code = int(raw) if raw.isdigit() else names.get(raw)
                if code is not None:
                    records.append((owner, code))
        if has_owner:
            return [code for owner, code in records if package and owner == package]
        return [code for _, code in records] if allow_unowned else []

    def playback_status(self, include_ui: bool = True,
                        expected_package: str = "", *, focus: str | None = None,
                        include_raw_media: bool = False) -> dict[str, Any]:
        """按目标包融合媒体 / 音频 / UI 证据，无参调用保持兼容。

        include_ui=False 完全禁止慢 UI dump。有目标媒体状态或已启动音轨时
        也跳过 UI；UI 控件上的“暂停”按钮不作为正在播放的依据。
        """
        # 一次窗口采样同时提供 activity 与前台包。
        focus = self.current_focus() if focus is None else focus
        match = re.search(r"([a-zA-Z][\w.]+)/[\w.$]+", focus)
        foreground = match.group(1) if match else ""
        pkg = expected_package or foreground
        ms_txt = self._shell_checked(["dumpsys", "media_session"]).stdout
        states = self._media_states(
            ms_txt, pkg, allow_unowned=not expected_package or pkg == foreground,
        )
        ms_playing = 3 in states
        code = 3 if ms_playing else (states[0] if states else None)
        state_desc = {0: "NONE", 1: "STOPPED", 2: "PAUSED", 3: "PLAYING",
                      4: "FAST_FORWARDING", 5: "REWINDING", 6: "BUFFERING",
                      7: "ERROR", 8: "CONNECTING", 9: "SKIPPING"}.get(code, "")
        if code is not None and not state_desc:
            state_desc = str(code)

        # 已确定目标媒体播放时无需再查询 pid / audio。
        audio_playing = False
        audio_paused = False
        if pkg and not ms_playing:
            result = self.adb.shell(["pidof", pkg])
            # pidof 无进程是正常的 exit 1；ADB 传输错误仍必须上抛。
            if result.returncode != 0 and not (
                    result.returncode == 1 and not result.stdout.strip()
                    and not result.stderr.strip()):
                raise AdbError(f"adb pidof {pkg} 失败({result.returncode}): "
                               f"{(result.stderr or result.stdout).strip()}")
            pids = set(result.stdout.split()) if result.returncode == 0 else set()
            pids = {pid for pid in pids if pid.isdigit()}
            if pids:
                audio_txt = self._shell_checked(["dumpsys", "audio"]).stdout
                piid2pid: dict[str, str] = {}
                last_state: dict[str, str] = {}
                for line in audio_txt.splitlines():
                    player = re.search(r"\bpiid\s*:\s*(\d+)", line)
                    if not player:
                        continue
                    piid = player.group(1)
                    owner = re.search(r"\buid/pid\s*:\s*\d+/(\d+)", line)
                    state = re.search(r"\bstate\s*:\s*(\w+)", line)
                    if "releasing player" in line.lower() or "released" in line.lower():
                        piid2pid.pop(piid, None)
                        last_state.pop(piid, None)
                        continue
                    if owner:
                        piid2pid[piid] = owner.group(1)
                    if state:
                        last_state[piid] = state.group(1).lower()
                audio_playing = any(
                    pid in pids and last_state.get(piid) == "started"
                    for piid, pid in piid2pid.items()
                )
                # Feiniu pauses the AudioTrack (state=paused) without a
                # MediaSession; a still-loaded paused track is positive evidence
                # of PAUSED, distinct from a released/absent track.
                audio_paused = (not audio_playing) and any(
                    pid in pids and last_state.get(piid) == "paused"
                    for piid, pid in piid2pid.items()
                )

        activity_is_player = bool(pkg and pkg == foreground and any(
            k in focus.lower() for k in ("player", "video", "media", "play")
        ))
        ui_playing = False
        ui_checked = False
        # No MediaSession state and no owned started AudioTrack, yet a player is
        # in the foreground: some players pause the track without registering a
        # session. Consult UI text, but never manufacture PLAYING from a stale
        # "继续播放" hint — a paused overlay marks PAUSED, not playing.
        if include_ui and activity_is_player and not states and not audio_playing:
            ui_checked = True
            texts = self.ui_dump()["texts"]
            paused = any(re.search(
                r"已暂停|暂停中|播放已暂停|继续播放|恢复播放|\bpaused\b|\bresume\b",
                text, re.IGNORECASE,
            ) for text in texts)
            ui_playing = not paused and any(re.search(
                r"正在播放|播放中|\bnow playing\b", text, re.IGNORECASE,
            ) for text in texts)
            if paused:
                state_desc = "PAUSED"

        playing = bool(ms_playing or audio_playing or ui_playing)
        if playing:
            state_desc = "PLAYING"
        elif audio_paused and not states:
            # Owned AudioTrack paused with no competing MediaSession state.
            state_desc = "PAUSED"
        result = {
            "playing": playing,
            "state": state_desc,
            "package": pkg,
            "activity": focus,
            "evidence": {
                "media_session_playing": ms_playing,
                "audio_track_started": audio_playing,
                "audio_track_paused": audio_paused,
                "activity_is_player": activity_is_player,
                "ui_playing": ui_playing,
                "ui_checked": ui_checked,
            },
        }
        result.update(self._session_details(ms_txt, pkg))
        if include_raw_media:
            result["_raw_media_session"] = ms_txt
        return result
