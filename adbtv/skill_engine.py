"""Reviewed declarative skills over the existing whitelist tools and Flow engine.

No eval, source generation, imports, or production code edits are supported.
Learned skills are drafts until sandbox validation and explicit approval.
"""
from __future__ import annotations

from dataclasses import asdict
import copy
import json
import re
import tempfile
import time
from pathlib import Path

from .execution import checkpoint


class SkillEngine:
    def __init__(self, memory, cfg):
        self.memory = memory
        self.cfg = cfg

    def select(self, goal, instruction, state):
        # Prefer only verified, explicitly approved declarative paths whose
        # trigger and preconditions actually match the present observation.
        import hashlib
        key = hashlib.sha256(json.dumps(asdict(goal), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous = self.memory.best_path(key, goal.package) if self.memory is not None else None
        preferred = ""
        if previous and previous.get("steps"):
            preferred = previous["steps"][0].get("skill_id", "")
        rows = self.memory.list_skills() if self.memory is not None else []
        rows.sort(key=lambda row: row.get("id") != preferred)
        for row in rows:
            if row.get("status") != "approved":
                continue
            definition = row.get("definition", {})
            if self.matches(definition, goal, instruction, state):
                return {"id": row["id"], "kind": "approved", "definition": definition}
        if goal.action in ("open", "launch") and goal.package:
            return {"id": "builtin.open", "kind": "open"}
        # Transport controls are their own capability and must never be routed
        # through media-library navigation (which would launch/resume a title).
        if goal.action in ("pause", "resume"):
            return {"id": "builtin." + goal.action, "kind": goal.action}
        if goal.package == "com.trim.tv" and goal.action in ("play",):
            return {"id": "builtin.feiniu", "kind": "feiniu"}
        if goal.action in ("play", "search") and goal.package:
            return {"id": "builtin.search", "kind": "search"}
        if goal.action in ("back", "volume_up", "volume_down", "volume_set"):
            return {"id": "builtin." + goal.action, "kind": goal.action}
        if goal.action == "home":
            return {"id": "builtin.home", "kind": "home"}
        return None

    @staticmethod
    def matches(definition, goal, instruction, state):
        trigger = definition.get("trigger", {})
        if trigger.get("action") and trigger["action"] != goal.action:
            return False
        package = trigger.get("package") or definition.get("package")
        if package and package != goal.package:
            return False
        if any(word not in instruction for word in trigger.get("contains", [])):
            return False
        pre = definition.get("preconditions", {})
        if pre.get("package") and pre["package"] != state.app:
            return False
        if pre.get("fingerprint") and pre["fingerprint"] != state.page_fingerprint:
            return False
        return all(text in state.texts for text in pre.get("texts", []))

    def execute(self, skill, goal, instruction, device, toolbox, perception):
        checkpoint()
        kind = skill["kind"]
        if kind == "open":
            if device.current_package() == goal.package:
                return {"ok": True, "summary": "目标应用已在前台", "log": []}
            return device.launch_app(goal.package)
        if kind in ("pause", "resume"):
            # Delegate to the device capability: only a positively identified
            # player is toggled, using the app-appropriate key and no-op when the
            # desired state already holds. An ack is not semantic success.
            result = device.transport(kind)
            if not result.get("acknowledged"):
                return {"ok": False, "summary": result.get("reason") or "当前无法确认播放器状态，未发送按键",
                        "playback_control": result}
            return {"ok": True, "summary": "已发送播放器" + ("暂停" if kind == "pause" else "继续") + "按键",
                    "playback_control": result}
        if kind == "volume_set":
            applied = device.set_volume(goal.level if goal.level is not None else 0)
            return {"ok": bool(applied.get("ok")), "summary": "已设置音量", "applied": applied}
        if kind in ("back", "volume_up", "volume_down"):
            device.key(kind)
            return {"ok": True, "summary": "已发送按键：" + kind}
        if kind == "home":
            device.key("home")
            return {"ok": True, "summary": "已发送电视首页按键"}
        if kind == "feiniu":
            from .feiniu import FeiniuPlayer
            from .intent import Intent, parse_fast
            intent = parse_fast(instruction) or Intent("play", app="feiniu_tv", title=goal.title,
                                                       season=goal.season, episode=goal.episode,
                                                       drama="短剧" in instruction)
            return FeiniuPlayer(device, self.cfg).run(intent)
        if kind == "search":
            from .tools import auto_play
            from .agent import parse_instruction
            _, query = parse_instruction(instruction, self.cfg)
            search_query = goal.query or goal.title or query
            return auto_play(device, goal.package, search_query, self.cfg,
                             play=goal.action == "play", expected_title=goal.title)
        definition = skill["definition"]
        self.validate_definition(definition, toolbox)
        state = perception.observe(fresh=True, include_playback=False)
        if not self.matches(definition, goal, instruction, state):
            raise ValueError("Skill前置条件已变化")
        deadline = time.monotonic() + definition.get("timeout", 60)
        records = []
        substitutions = {"${title}": goal.title, "${package}": goal.package,
                         "${season}": goal.season, "${episode}": goal.episode}
        for step in definition["steps"]:
            checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError("Skill执行超时")
            args = {k: substitutions.get(v, v) if isinstance(v, str) else v
                    for k, v in step.get("args", {}).items()}
            args = toolbox.validate(step["tool"], args)
            if step["tool"] == "tv_click_text":
                perception.click(text=args["text"])
                result = {"ok": True}
            elif step["tool"] == "tv_tap":
                perception.click(coords=(args["x"], args["y"]))
                result = {"ok": True}
            else:
                result = toolbox.call(step["tool"], args)
            records.append({"tool": step["tool"], "args": args, "result": result})
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(result.get("summary") or result.get("error") or "Skill步骤失败")
            perception.invalidate()
        return {"ok": True, "summary": "Skill步骤已执行，等待目标验证", "steps": records}

    @staticmethod
    def validate_definition(definition, toolbox):
        if not isinstance(definition, dict):
            raise ValueError("Skill必须是JSON对象")
        allowed = {"name", "package", "trigger", "preconditions", "steps", "success", "recovery", "timeout", "max_retries"}
        if set(definition)-allowed:
            raise ValueError("Skill包含不支持的字段，不允许源码或命令")
        if not isinstance(definition.get("name"), str) or not 1 <= len(definition["name"]) <= 100:
            raise ValueError("Skill名称无效")
        steps = definition.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
            raise ValueError("Skill需要1..30个步骤")
        if type(definition.get("timeout", 60)) not in (int, float) or not 1 <= definition.get("timeout", 60) <= 180:
            raise ValueError("Skill超时须为1..180秒")
        if type(definition.get("max_retries", 0)) is not int or not 0 <= definition.get("max_retries", 0) <= 2:
            raise ValueError("Skill重试上限无效")
        for key in ("trigger", "preconditions", "success", "recovery"):
            if not isinstance(definition.get(key, {}), dict):
                raise ValueError(f"Skill {key} 必须是对象")
        trigger = definition.get("trigger", {})
        if set(trigger)-{"action", "package", "contains"}:
            raise ValueError("Skill触发器只支持action/package/contains")
        if not (trigger.get("package") or definition.get("package")):
            raise ValueError("Skill必须限制适用应用")
        for key in ("contains",):
            words = trigger.get(key, [])
            if not isinstance(words, list) or any(not isinstance(w, str) or len(w)>200 for w in words):
                raise ValueError("无效Skill触发词")
        for step in steps:
            if not isinstance(step, dict) or set(step)-{"tool", "args"}:
                raise ValueError("Skill步骤只允许tool/args")
            if step.get("tool") not in toolbox.names():
                raise ValueError("Skill含非白名单工具")
            args = step.get("args", {})
            # Validate literal values now; template values are validated again
            # after substitution, immediately before every production call.
            if not isinstance(args, dict):
                raise ValueError("Skill参数须为对象")
            template = {"${title}": "sample", "${package}": "com.example.tv",
                        "${season}": 1, "${episode}": 1}
            test_args = {k: template.get(v, v) if isinstance(v, str) else v for k, v in args.items()}
            toolbox.validate(step["tool"], test_args)
        return True

    def propose(self, definition, toolbox, source_task=""):
        self.validate_definition(definition, toolbox)
        return self.memory.propose_skill(definition, source_task)

    def validate_in_simulator(self, skill_id):
        """Execute an isolated fake-ADB run; never validate against production TV."""
        from .adb import AdbClient
        from .config import DEFAULTS
        from .device import TVDevice
        from .tools import ToolBox
        from .perception import Perception
        from .verifier import GoalSpec, Verifier
        row = self.memory.get_skill(skill_id)
        if row is None:
            raise ValueError("Skill不存在")
        definition = row["definition"]
        root = Path(__file__).resolve().parent.parent
        import sys
        with tempfile.TemporaryDirectory(prefix="aitv-skill-") as tmp:
            cfg = copy.deepcopy(DEFAULTS)
            import shlex
            cfg["adb"].update(binary=shlex.join([sys.executable, str(Path(__file__).with_name("simulator.py"))]),
                              serial="fake-tv:5555", fake_state=str(Path(tmp)/"tv.json"))
            cfg["agent"]["log_dir"] = str(Path(tmp)/"logs")
            dev = TVDevice(AdbClient(cfg), cfg)
            box = ToolBox(dev)
            self.validate_definition(definition, box)
            perception = Perception(dev, cfg=cfg)
            condition = definition.get("success", {})
            if not condition.get("action"):
                raise ValueError("Skill必须定义可验证的success.action")
            allowed = {"action", "package", "title", "season", "episode", "require_progress"}
            if set(condition)-allowed:
                raise ValueError("未知成功条件")
            goal = GoalSpec(**condition)
            before = perception.observe(fresh=True)
            self.execute({"kind": "approved", "definition": definition}, goal,
                         " ".join(definition.get("trigger", {}).get("contains", [])), dev, box, perception)
            after = perception.observe(fresh=True)
            verified = Verifier().verify(goal, before, after)
            report = {"environment": "isolated_fake_adb", "status": verified.status.value,
                      "ok": verified.ok, "verification": verified.to_dict(), "at": time.time()}
            if not verified.ok:
                self.memory.update_skill(skill_id, validation=report)
                raise ValueError("模拟环境未满足Skill成功条件，禁止审批")
            return self.memory.update_skill(skill_id, status="validated", validation=report)

    def approve(self, skill_id, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("需要用户明确确认审批Skill")
        row = self.memory.get_skill(skill_id)
        if row is None or row.get("status") != "validated":
            raise ValueError("Skill必须先在隔离模拟设备验证")
        validation = row.get("validation") or {}
        if validation.get("environment") != "isolated_fake_adb" or validation.get("status") != "SUCCESS":
            raise ValueError("缺少可核验的隔离验证记录")
        return self.memory.update_skill(skill_id, status="approved", approval={"confirmed": True, "explicit": True, "at": time.time()})
