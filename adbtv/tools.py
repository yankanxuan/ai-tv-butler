"""工具层：Agent 与 MCP 共用的工具定义与分发。"""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .device import TVDevice


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict[str, Any]
    handler: Callable[[TVDevice, dict[str, Any]], Any]
    required: list[str] = field(default_factory=list)

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.params,
                "required": self.required, "additionalProperties": False}

    def validate(self, args: Any) -> dict[str, Any]:
        return validate_schema(args, self.input_schema(), self.name)

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": copy.deepcopy(self.input_schema())}


def validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> Any:
    """Validate the JSON Schema subset used by tools and configured flow steps.

    Return a fresh normalized value with defaults; do not coerce input types.
    bool is deliberately excluded from JSON integer/number values.
    """
    kind = schema.get("type")
    valid = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: type(v) is int,
        "number": lambda v: type(v) in (int, float) and math.isfinite(v),
        "boolean": lambda v: type(v) is bool,
        "null": lambda v: v is None,
    }
    if kind not in valid or not valid[kind](value):
        raise ValueError(f"{path}: 需要 {kind} 类型")
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise ValueError(f"{path}: 不允许的值")
    if kind == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ValueError(f"{path}: 未知参数 {', '.join(sorted(map(str, unknown)))}")
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValueError(f"{path}: 缺少必要参数 {', '.join(missing)}")
        result = copy.deepcopy(value)
        for key, subschema in properties.items():
            if key not in result and "default" in subschema:
                result[key] = copy.deepcopy(subschema["default"])
            if key in result:
                result[key] = validate_schema(result[key], subschema, f"{path}.{key}")
        return result
    if kind == "array":
        for bound, op in (("minItems", lambda n, b: n < b), ("maxItems", lambda n, b: n > b)):
            if bound in schema and op(len(value), schema[bound]):
                raise ValueError(f"{path}: 数组长度超出限制")
        return [validate_schema(v, schema["items"], f"{path}[{i}]") for i, v in enumerate(value)]
    if kind == "string":
        if "\x00" in value:
            raise ValueError(f"{path}: 不允许 NUL 字符")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ValueError(f"{path}: 文本长度超出限制")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{path}: 文本格式无效")
    if kind in ("integer", "number"):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path}: 数值超出范围")
    return value


SEARCH_WORDS = ["搜索", "搜片", "查找", "搜一下", "search"]
EXCLUDE_LABELS = {
    "搜索", "取消", "搜索视频", "搜片", "查找", "首页", "我的", "设置", "返回",
    "全部", "电视剧", "电影", "综艺", "动漫", "直播", "频道", "分类", "推荐",
    "search", "cancel", "back", "home",
}


def _pick_search_entry(dev: TVDevice, elements=None):
    for e in dev.elements() if elements is None else elements:
        label = e.label
        low = label.lower()
        rid = e.resource_id.lower()
        cls = e.cls.lower()
        if (label in SEARCH_WORDS or "搜索" in label or "search" in low
                or "search" in rid or "edittext" in cls or "searchview" in cls):
            return e
    return None


def _pick_edit_box(dev: TVDevice, elements=None):
    for e in dev.elements() if elements is None else elements:
        cls = e.cls.lower()
        rid = e.resource_id.lower()
        if "edittext" in cls or "searchview" in cls or "autocompletetextview" in cls:
            return e
    return None


def _pick_first_result(dev: TVDevice, box, elements=None) -> Any:
    snapshot = dev.elements() if elements is None else elements
    els = [e for e in snapshot if e.clickable and e.label and "edittext" not in e.cls.lower()]
    cands = []
    for e in els:
        if e.label in EXCLUDE_LABELS:
            continue
        if box and e.y <= box.y:
            continue
        cands.append(e)
    if not cands:
        # 退路：任何 clickable 且文本较长
        cands = [e for e in els if len(e.label) >= 3][:3]
    cands.sort(key=lambda e: (e.y, e.x))
    return cands[0] if cands else None


def auto_play(dev: TVDevice, app: str, query: str, cfg: dict[str, Any], *,
              play: bool = True, expected_title: str = "") -> dict[str, Any]:
    """高层流程：打开应用 -> 进入搜索 -> 输入关键词 -> 选第一个结果 -> 验证播放。"""
    log: list[dict[str, Any]] = []

    def step(name: str, **kw):
        rec = {"step": name, **kw}
        log.append(rec)
        return rec

    launch = dev.launch_app(app)
    step("launch_app", **launch)
    if not launch.get("ok"):
        return {"ok": False, "stage": "launch", "log": log, "error": "应用启动失败"}

    # Poll readiness immediately instead of paying fixed sleeps on every page.
    poll = float(cfg.get("ui", {}).get("fast_poll_interval", 0.2))
    timeout = float(cfg.get("ui", {}).get("wait_timeout", 20))

    def wait_element(pick, seconds=timeout):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            snapshot = dev.elements()
            found = pick(snapshot)
            if found:
                return found
            time.sleep(poll)
        return None

    entry = wait_element(lambda els: _pick_search_entry(dev, els))
    if entry:
        dev.tap(entry.x, entry.y)
        step("tap_search_entry", element=entry.label, center=[entry.x, entry.y])
        fresh = dev.elements()
        if _pick_edit_box(dev, fresh) is None:
            focused = next((e for e in fresh if e.focused), None)
            if focused and (focused.bounds == entry.bounds or
                            (entry.resource_id and focused.resource_id == entry.resource_id)):
                dev.key("center")
                step("activate_focused_search_entry")
    else:
        dev.key("search")
        step("press_search_key")

    box = wait_element(lambda els: _pick_edit_box(dev, els))
    if box is None:
        return {"ok": False, "stage": "search", "log": log, "summary": "未找到搜索输入框"}
    dev.tap(box.x, box.y)
    step("focus_search_box", element=box.cls, center=[box.x, box.y])
    txt = dev.input_text(query)
    step("input_text", query=query, **txt)
    if not txt.get("ok"):
        return {"ok": False, "stage": "input", "log": log, "summary": "输入失败"}
    dev.key("enter")
    def select_result(els):
        if not expected_title:
            return _pick_first_result(dev, box, els)
        expected = re.sub(r"[\s《》]", "", expected_title).casefold()
        by_id = {e.node_id: e for e in els}
        for e in els:
            if re.sub(r"[\s《》]", "", e.text or e.desc).casefold() != expected or "edittext" in e.cls.lower():
                continue
            node = e
            while node is not None and not (node.clickable or node.focusable):
                node = by_id.get(node.parent_id)
            if node and node.y > box.y:
                return node
        return None
    picked = wait_element(select_result, seconds=8)
    if not play:
        step("search_results", found=bool(picked))
        return {"ok": bool(picked), "search_only": True, "query": query, "log": log,
                "summary": "已展示搜索结果，未播放" if picked else "尚未确认搜索结果"}
    if picked:
        dev.tap(picked.x, picked.y)
        step("tap_first_result", element=picked.label, center=[picked.x, picked.y])
    else:
        step("no_result_found")
        return {"ok": False, "stage": "selection", "log": log,
                "summary": "没有找到匹配的搜索结果，未播放替代内容"}

    # 验证播放
    ok = False
    play_ev = {}
    deadline = time.time() + int(cfg.get("ui", {}).get("wait_timeout", 20))
    while time.time() < deadline:
        play_ev = dev.playback_status()
        if play_ev.get("playing"):
            ok = True
            break
        time.sleep(1.5)
    step("verify_playback", playing=bool(play_ev.get("playing")), evidence=play_ev.get("evidence"))

    return {
        "ok": ok,
        "app": app,
        "query": query,
        "log": log,
        "playback": play_ev,
        "summary": ("已自动打开并播放：" + (picked.label if picked else query)) if ok
                   else "已执行到搜索结果，但未确认到播放状态（可能需人工微调选择器）",
    }


# ---------------- 工具注册表 ----------------
def build_tools() -> list[ToolSpec]:
    tools = [
        ToolSpec("tv_info", "读取电视型号、系统、分辨率等基本信息。", {}, 
                 lambda d, a: d.info()),
        ToolSpec("tv_list_apps", "列出电视上已安装的应用包名（third=第三方）。", {},
                 lambda d, a: d.list_apps()),
        ToolSpec("tv_current_app", "查看当前前台应用（包名）。", {},
                 lambda d, a: {"package": d.current_package(), "focus": d.current_focus()}),
        ToolSpec("tv_launch_app", "按应用名或包名启动一个电视应用。",
                 {"package": {"type": "string", "description": "应用别名或包名，如 bilibili / com.xiaodianshi.tv.yst"}},
                 lambda d, a: d.launch_app(a["package"])),
        ToolSpec("tv_key", "发送遥控器按键，如 home/back/up/down/left/right/center/play/pause/menu。",
                 {"key": {"type": "string", "description": "按键名或 KEYCODE_xxx"}},
                 lambda d, a: {"ok": True, "sent": a["key"], "out": d.key(a["key"])}),
        ToolSpec("tv_input_text", "向当前焦点输入框输入文本（ASCII 直接输入；中文需安装 ADBKeyBoard）。",
                 {"text": {"type": "string"}}, lambda d, a: d.input_text(a["text"])),
        ToolSpec("tv_tap", "点击屏幕坐标。",
                 {"x": {"type": "integer"}, "y": {"type": "integer"}},
                 lambda d, a: {"ok": True, "tapped": [a["x"], a["y"]], "out": d.tap(a["x"], a["y"])}),
        ToolSpec("tv_swipe", "滑动屏幕。",
                 {"x1": {"type": "integer"}, "y1": {"type": "integer"},
                  "x2": {"type": "integer"}, "y2": {"type": "integer"},
                  "duration": {"type": "integer", "default": 300}},
                 lambda d, a: {"ok": True, "out": d.swipe(a["x1"], a["y1"], a["x2"], a["y2"], a.get("duration", 300))}),
        ToolSpec("tv_ui_dump", "导出当前界面元素与文本（用于观察/定位/验证）。", {},
                 lambda d, a: d.ui_dump()),
        ToolSpec("tv_click_text", "查找包含指定文字/描述/资源id的元素并点击。",
                 {"text": {"type": "string"}},
                 lambda d, a: _click_text(d, a["text"])),
        ToolSpec("tv_screenshot", "截取当前电视画面保存到 NAS。",
                 {"path": {"type": "string", "description": "可选保存路径"}},
                 lambda d, a: _safe_screenshot(d, a.get("path", ""))),
        ToolSpec("tv_wait_text", "等待界面上出现指定文字（最多 timeout 秒）。",
                 {"text": {"type": "string"}, "timeout": {"type": "integer", "default": 20}},
                 lambda d, a: _wait_text(d, a["text"], a["timeout"])),
        ToolSpec("tv_playback_status", "检测当前是否正在播放（media_session + 界面证据）。", {},
                 lambda d, a: d.playback_status()),
        ToolSpec("tv_auto_play", "高级流程：打开应用并自动搜索关键词、选择首个结果、验证播放。",
                 {"app": {"type": "string"}, "query": {"type": "string"}},
                 lambda d, a: auto_play(d, a["app"], a["query"], d.cfg)),
        ToolSpec("tv_run_flow", "按应用预配置的 flow 步骤执行（适合媒体库/继续观看类应用，如飞牛TV）。",
                 {"app": {"type": "string", "description": "应用别名，如 feiniu_tv"}},
                 lambda d, a: run_flow(d, a["app"], d.cfg)),
    ]
    for tool in tools:
        tool.required = [key for key, spec in tool.params.items()
                         if "default" not in spec and key != "path"]
        for key, spec in tool.params.items():
            if spec["type"] == "string":
                spec.update(minLength=0 if key == "path" else 1,
                            maxLength=4096 if key in ("text", "query", "path") else 256)
            if key in ("x", "y", "x1", "y1", "x2", "y2"):
                spec.update(minimum=0, maximum=32767)
            elif key == "duration":
                spec.update(minimum=1, maximum=10000)
            elif key == "timeout":
                spec.update(minimum=1, maximum=120)
    return tools


def _wait_text(dev: TVDevice, text: str, timeout: int) -> dict[str, Any]:
    found = dev.wait_for_text(text, timeout)
    return {"ok": bool(found), "found": bool(found)}


def _safe_screenshot(dev: TVDevice, path: str = "") -> dict[str, Any]:
    """Tool callers may create PNGs only inside the configured artifact dir.

    Exclusive creation prevents overwriting NAS data, including existing files
    inside the log directory. Direct local CLI device.screenshot is unchanged.
    """
    root = Path(dev.cfg.get("agent", {}).get("log_dir") or "logs").expanduser().resolve()
    candidate = Path(path).expanduser() if path else Path(f"shot_{uuid.uuid4().hex}.png")
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not target.is_relative_to(root) or target == root or target.suffix.lower() != ".png":
        raise ValueError("截图路径必须位于 agent.log_dir 内，且使用 .png 扩展名")
    data = dev.adb.screenshot_bytes()
    root.mkdir(parents=True, exist_ok=True)
    # Walk from a directory fd, rejecting symlinks on every component. This
    # closes the check/open race if a directory changes after resolve().
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, dir_flags)
    try:
        relative = target.relative_to(root)
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, dir_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(relative.name, flags, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    finally:
        os.close(directory)
    return {"ok": True, "path": str(target), "bytes": len(data)}


def _resolve_alias(name: str, cfg: dict[str, Any]) -> str:
    key = (name or "").lower()
    aliases = cfg.get("apps", {})
    if key in aliases:
        return key
    for k, v in aliases.items():
        if v.get("package") == name:
            return k
        if v.get("label") and name and name in str(v.get("label")):
            return k
    return key


def _validate_flow(flow: Any) -> list[dict[str, Any]]:
    tools = {tool.name: tool for tool in build_tools()}
    actions = {
        "launch": ({}, []),
        "sleep": ({"seconds": {"type": "number", "minimum": 0, "maximum": 120, "default": 2}}, []),
        "key": (tools["tv_key"].params, ["key"]),
        "tap": (tools["tv_tap"].params, ["x", "y"]),
        "wait_text": (tools["tv_wait_text"].params, ["text"]),
        "wait_play": ({"timeout": {"type": "integer", "minimum": 1, "maximum": 120, "default": 15}}, []),
    }
    if not isinstance(flow, list) or not flow or len(flow) > 100:
        raise ValueError("flow 必须为包含 1–100 个步骤的列表")
    normalized = []
    for i, step in enumerate(flow):
        if not isinstance(step, dict) or not isinstance(step.get("action"), str) or step["action"] not in actions:
            raise ValueError(f"flow[{i}]: 未知 action")
        properties, required = actions[step["action"]]
        schema = {"type": "object", "properties": {
            "action": {"type": "string", "enum": [step["action"]]}, **properties},
            "required": ["action", *required], "additionalProperties": False}
        normalized.append(validate_schema(step, schema, f"flow[{i}]"))
    return normalized


def run_flow(dev: TVDevice, app: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """按 config.yaml 里该应用配置的 flow 步骤执行（适合无法用统一搜索逻辑的应用）。"""
    alias = _resolve_alias(app, cfg)
    appcfg = cfg.get("apps", {}).get(alias) or {}
    pkg = appcfg.get("package") or app
    if alias in ("feiniu_tv", "feiniu_drama") and cfg.get("agent", {}).get("smart_navigation", True):
        from .feiniu import FeiniuPlayer
        from .intent import Intent
        return FeiniuPlayer(dev, cfg).run(Intent("play", app="feiniu_tv", drama=alias == "feiniu_drama"))
    flow = appcfg.get("flow")
    if not flow:
        raise ValueError(f"应用「{app}」没有配置 flow 步骤（config.yaml → apps.{alias}.flow）")
    try:
        flow = _validate_flow(flow)
    except ValueError as exc:
        return {"ok": False, "app": alias, "log": [], "error": str(exc),
                "summary": f"flow 参数无效: {exc}"}
    log: list[dict[str, Any]] = []

    def emit(rec):
        log.append(rec)

    ev = None
    for st in flow:
        act = st.get("action")
        if act != "wait_play":
            ev = None
        if act == "launch":
            launched = dev.launch_app(pkg)
            emit({"step": "launch_app", **launched})
            if not launched.get("ok"):
                return {"ok": False, "app": alias, "log": log, "summary": "flow 应用启动失败"}
        elif act == "sleep":
            time.sleep(float(st.get("seconds", 2)))
        elif act == "key":
            dev.key(st["key"]); emit({"step": "key", "key": st["key"]})
        elif act == "tap":
            dev.tap(st["x"], st["y"]); emit({"step": "tap", "center": [st["x"], st["y"]]})
        elif act == "wait_text":
            ok = dev.wait_for_text(st["text"], st.get("timeout"))
            emit({"step": "wait_text", "text": st["text"], "found": bool(ok)})
            if not ok:
                return {"ok": False, "app": alias, "log": log,
                        "summary": f"flow 等待文字超时: {st['text']}"}
        elif act == "wait_play":
            ok = False
            ev: dict[str, Any] = {}
            deadline = time.time() + int(st.get("timeout", 15))
            while time.time() < deadline:
                ev = dev.playback_status()
                if ev.get("playing"):
                    ok = True
                    break
                time.sleep(1.2)
            emit({"step": "verify_playback", "playing": ok, "evidence": ev.get("evidence")})
            if not ok:
                return {"ok": False, "app": alias, "log": log, "playback": ev,
                        "summary": f"flow 执行到搜索/选择，但未确认到播放"}
        else:
            return {"ok": False, "app": alias, "log": log, "summary": f"未知 flow action: {act}"}
    if ev is None:
        ev = dev.playback_status()
    return {"ok": bool(ev.get("playing")), "app": alias, "log": log, "playback": ev,
            "summary": (f"已播放 {alias}") if ev.get("playing") else "flow 执行完毕但未确认播放"}


def _click_text(dev: TVDevice, text: str) -> dict[str, Any]:
    kw = text.lower()
    for e in dev.elements():
        if kw in f"{e.text}\n{e.desc}\n{e.resource_id}".lower():
            dev.tap(e.x, e.y)
            return {"ok": True, "clicked": e.label, "center": [e.x, e.y]}
    raise ValueError(f"界面上找不到包含「{text}」的元素")


class ToolBox:
    def __init__(self, device: TVDevice,
                 before_call: Callable[[str, dict[str, Any]], None] | None = None,
                 after_call: Callable[[str, dict[str, Any], Any], None] | None = None):
        self.device = device
        self.before_call = before_call
        self.after_call = after_call
        self._tools = {t.name: t for t in build_tools()}

    def specs(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def validate(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate without executing; shared by perception's guarded actions."""
        if not isinstance(name, str) or name not in self._tools:
            raise ValueError(f"未知工具: {name}")
        return self._tools[name].validate({} if args is None else args)

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        normalized = self.validate(name, args)
        tool = self._tools[name]
        if self.before_call is not None:
            self.before_call(name, copy.deepcopy(normalized))
        result = tool.handler(self.device, normalized)
        if self.after_call is not None:
            self.after_call(name, copy.deepcopy(normalized), result)
        return result

    def describe(self) -> str:
        lines = []
        for t in self._tools.values():
            params = ", ".join(t.params) if t.params else "无"
            lines.append(f"- {t.name}({params}): {t.description}")
        return "\n".join(lines)
