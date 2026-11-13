"""命令行入口：ai-tv <子命令>。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .adb import AdbClient, AdbError
from .agent import Agent
from .config import load_config, project_root
from .device import TVDevice
from .llm import LLMClient, LLMError
from .scheduler import Scheduler
from .tools import ToolBox
from . import __version__


def _ctx(cfg: dict[str, Any]):
    adb = AdbClient(cfg)
    device = TVDevice(adb, cfg)
    toolbox = ToolBox(device)
    llm = LLMClient(cfg)
    agent = Agent(device, toolbox, llm, cfg)
    return adb, device, toolbox, llm, agent


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_doctor(args, cfg):
    adb, device, toolbox, llm, agent = _ctx(cfg)
    print("== AI 电视管家 自检 ==")
    print(f"配置来源: {cfg.get('_source')}")
    print(f"adb 二进制: {cfg['adb'].get('binary')}")
    print(f"目标设备  : {cfg['adb'].get('serial') or '(未配置 host/serial)'}")
    try:
        from shutil import which
        import shlex
        bin0 = shlex.split(cfg['adb']['binary'])[0]
        print(f"adb 可用  : {which(bin0) or bin0}")
    except Exception:
        pass
    try:
        devs = adb.devices()
        print(f"adb 设备  : {devs if devs else '(无)'}  目标状态={adb.state()}")
    except Exception as e:  # noqa
        print(f"adb 设备  : 读取失败 {e}")
    if llm.enabled:
        try:
            r = llm.chat([{"role": "user", "content": "回复两个字：就绪"}])
            print(f"大模型    : provider={llm.provider} model={llm.model} -> {r!r}")
        except Exception as e:  # noqa
            print(f"大模型    : 调用失败 {e}")
    else:
        print("大模型    : 未启用（将使用离线确定性规划）")
    print(f"工具数量  : {len(toolbox.names())}")
    return 0


def cmd_config(args, cfg):
    from .server import redact_secrets
    _print(redact_secrets(json.loads(json.dumps(cfg, default=str))))
    return 0


def cmd_connect(args, cfg):
    adb = AdbClient(cfg)
    try:
        st = adb.connect(force=True)
        print(f"连接成功，状态={st}")
        _print(TVDevice(adb, cfg).info())
        return 0
    except AdbError as e:
        print(f"连接失败：{e}", file=sys.stderr)
        return 2


def cmd_devices(args, cfg):
    _print(AdbClient(cfg).devices())
    return 0


def cmd_info(args, cfg):
    adb, device, *_ = _ctx(cfg)
    try:
        _print(device.info())
        return 0
    except AdbError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 2


def cmd_apps(args, cfg):
    adb, device, *_ = _ctx(cfg)
    try:
        _print(device.list_apps())
        return 0
    except AdbError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 2


def cmd_cmd(args, cfg):
    if args.offline:
        cfg.setdefault("agent", {})["use_llm"] = False
    adb, device, toolbox, llm, agent = _ctx(cfg)
    res = agent.run(args.instruction)
    if args.json:
        _print(res)
    else:
        print(f"[模式] {res.get('mode')}  耗时 {res.get('elapsed', '?')}s")
        if res.get("timings"):
            print("[阶段耗时] " + json.dumps(res["timings"], ensure_ascii=False) + " 秒")
        for st in res.get("steps", []) or res.get("log", []):
            print("  ", json.dumps(st, ensure_ascii=False, default=str))
        print(f"[结果] {'成功' if res.get('ok') else '未成功'} — {res.get('summary', '')}")
    return 0 if res.get("ok") else 1


def cmd_tool(args, cfg):
    adb, device, toolbox, *_ = _ctx(cfg)
    payload = {}
    if args.args:
        payload = json.loads(args.args)
    try:
        res = toolbox.call(args.name, payload)
        _print(res)
        return 0
    except Exception as e:  # noqa
        print(f"工具调用失败：{e}", file=sys.stderr)
        return 2


def cmd_ui(args, cfg):
    adb, device, *_ = _ctx(cfg)
    _print(device.ui_dump())
    return 0


def cmd_shot(args, cfg):
    adb, device, *_ = _ctx(cfg)
    try:
        _print(device.screenshot(args.path or ""))
        return 0
    except Exception as e:  # noqa
        print(f"截图失败：{e}", file=sys.stderr)
        return 2


def cmd_key(args, cfg):
    adb, device, *_ = _ctx(cfg)
    _print({"sent": args.key, "out": device.key(args.key)})
    return 0


def cmd_launch(args, cfg):
    adb, device, *_ = _ctx(cfg)
    _print(device.launch_app(args.app))
    return 0


def cmd_mcp(args, cfg):
    from .mcp_server import MCPServer
    MCPServer(cfg).serve_stdio()
    return 0


def cmd_serve(args, cfg):
    from .server import serve
    serve(cfg)
    return 0


def cmd_schedule(args, cfg):
    from .device import TVDevice as _D
    sched = Scheduler(cfg["scheduler"]["store"])
    adb = AdbClient(cfg)
    device = _D(adb, cfg)
    toolbox = ToolBox(device)
    llm = LLMClient(cfg)
    agent = Agent(device, toolbox, llm, cfg)

    if args.action == "list":
        _print({"tasks": sched.tasks})
        return 0
    if args.action == "remove":
        print("已删除" if sched.remove(args.id) else "未找到该任务")
        return 0
    if args.action == "add":
        t = sched.add(command=args.command, kind=args.kind, when=args.at,
                      interval=args.every, cron=args.cron, delay=args.in_)
        _print({"added": t})
        return 0
    if args.action == "run":
        if args.once:
            def _exec(c):
                r = agent.run(c)
                return {"ok": r.get("ok"), "summary": r.get("summary")}
            ran = sched.run_due(_exec)
            _print({"ran": [{"id": r["task"]["id"], "ok": r["ok"],
                            "result": r.get("result")} for r in ran]})
            return 0
        print(f"[scheduler] 常驻循环，每 {cfg['scheduler']['poll_seconds']}s 检查一次 (Ctrl+C 退出)")
        try:
            sched.loop(lambda c: agent.run(c), int(cfg["scheduler"]["poll_seconds"]))
        except KeyboardInterrupt:
            pass
        return 0
    return 1


def cmd_demo(args, cfg):
    """用内置模拟电视跑通「打开应用→搜索→选择→播放→验证」全链路（无需真机）。"""
    root = project_root()
    from .execution import runtime_dir
    fake_state = str(runtime_dir(cfg).parent / "fake_tv_state.json")
    cfg = json.loads(json.dumps(cfg))
    import shlex
    from pathlib import Path
    cfg["adb"]["binary"] = shlex.join([sys.executable, str(Path(__file__).with_name("simulator.py"))])
    cfg["adb"]["serial"] = "fake-tv:5555"
    cfg["adb"]["fake_state"] = fake_state
    cfg["adb"]["auto_connect"] = True
    cfg["agent"]["use_llm"] = False
    # 重置模拟电视状态
    if os.path.exists(fake_state):
        os.remove(fake_state)
    adb, device, toolbox, llm, agent = _ctx(cfg)
    instruction = args.instruction or "打开哔哩哔哩，找个科技视频播放"
    print(f"== 模拟演示 ==\n指令：{instruction}\n")
    res = agent.run(instruction)
    for st in res.get("log", []):
        print("  ", json.dumps(st, ensure_ascii=False, default=str))
    print()
    print(f"[结果] {'✅ 成功' if res.get('ok') else '❌ 未成功'} — {res.get('summary')}")
    print(f"[播放状态] {json.dumps(res.get('playback', {}), ensure_ascii=False)}")
    return 0 if res.get("ok") else 1


def cmd_media(args, cfg):
    from .media_index import MediaIndex, index_from_config
    if not cfg.get("media", {}).get("enabled", False):
        print("媒体库索引未启用：在 config.yaml 设置 media.enabled: true 与 media.roots")
        return 0
    if args.action == "serve":
        from .media_server import MediaServer
        media = cfg.get("media", {}) or {}
        from .execution import runtime_dir
        server = MediaServer(media.get("roots") or [],
                             host=media.get("serve_host", ""),
                             port=int(media.get("serve_port", 8899)),
                             token=media.get("serve_token", ""),
                             link_ttl=int(media.get("link_ttl", 300)),
                             session_idle=int(media.get("session_idle", 1800)),
                             private_only=bool(media.get("private_only", True)),
                             state_dir=media.get("state_dir") or runtime_dir(cfg).parent,
                             cfg=cfg, advertise_host=media.get("advertise_host", ""))
        print(f"媒体服务 http://{server.advertise_host()}:{server.port}  "
              f"根目录={media.get('roots')}  仅只读、支持 Range")
        try:
            server.start(blocking=True)
        except OSError as exc:
            if getattr(exc, "errno", None) in (98, 48):   # EADDRINUSE
                print(f"媒体端口 {server.port} 已被占用：媒体服务应当只有一个实例在运行。\n"
                      f"若已在 Compose 中启用 media 服务，worker 无需再启动它。",
                      file=sys.stderr)
                return 2
            raise
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0
    if args.action == "rebuild":
        media = cfg["media"]
        index = MediaIndex(media.get("roots") or [], cache_path=media.get("cache") or None).scan()
        if media.get("cache"):
            index.save()
        _print({"stats": index.stats(), "cache": media.get("cache")})
        return 0
    index = index_from_config(cfg)
    if index is None:
        raise ValueError("无法加载媒体索引")
    if args.action == "search":
        _print({"query": args.query, "results": index.search(args.query, limit=args.limit)})
    elif args.action == "resolve":
        _print(index.resolve(args.query, args.season, args.episode))
    elif args.action == "stats":
        _print(index.stats())
    return 0


def cmd_voice(args, cfg):
    """电视语音旁听：从 logcat 提取小米 ASR 文本并交给 Agent。"""
    from .voice import VoiceListener
    listener_cfg = (cfg.get("voice", {}) or {}).get("listener", {}) or {}
    if not listener_cfg.get("enabled", False) and not args.force:
        # 以非零退出提示未启用，避免容器/服务"启动成功但什么也不做"。
        print("语音旁听未启用：在 config.yaml 设置 voice.listener.enabled: true（或用 --force 启用）",
              file=sys.stderr)
        return 2
    if args.action == "test":
        # 只读自检：确认能读到 logcat 并解析（不需要真人说话）。
        from .voice import parse_log_line
        import subprocess, shlex
        base = shlex.split(cfg["adb"].get("binary", "adb"))
        if cfg["adb"].get("serial"):
            base += ["-s", cfg["adb"]["serial"]]
        try:
            out = subprocess.run(base + ["logcat", "-v", "epoch", "-d", "-t", "400"],
                                 capture_output=True, text=True, timeout=20)
        except Exception as exc:
            print(f"无法读取 logcat：{exc}")
            return 2
        lines = out.stdout.splitlines()
        hits = [line for line in lines if "processAsrResult" in line]
        print(f"读取 {len(lines)} 行日志，其中 ASR 结果行 {len(hits)} 条")
        finals = [item for item in (parse_log_line(line) for line in hits) if item]
        print(f"可解析的最终识别 {len(finals)} 条")
        for item in finals[-3:]:
            # 默认只显示长度与哈希，避免把语音原文写进终端日志。
            print(f"  - len={len(item.text)} hash={__import__('adbtv.voice', fromlist=['hash_text']).hash_text(item.text)[:10]}"
                  f" at={item.source_at:.0f}")
        print("提示：说一句话后再运行本命令即可看到识别记录。")
        return 0

    show_text = bool(listener_cfg.get("log_full_text", False))

    def describe(utterance):
        if show_text:
            return utterance.text
        from .voice import hash_text
        return f"len={len(utterance.text)} hash={hash_text(utterance.text)[:10]}"

    # Build device/model/Agent exactly once per listener process. The V4 wrapper
    # dispatches only canonical finite commands; it never invokes Agent's planner.
    import copy
    voice_cfg = copy.deepcopy(cfg)
    intelligent = bool((voice_cfg.get("voice", {}).get("intelligence") or {}).get("enabled"))
    if args.action == "dialogue":
        intelligent = True
        voice_cfg.setdefault("voice", {}).setdefault("intelligence", {})["enabled"] = True
    if intelligent:
        from .voice_runtime import VoiceDialogue
        voice_cfg.setdefault("agent", {})["use_llm"] = False
        if not (voice_cfg.get("voice", {}).get("conversation") or {}).get("persist_agent_history", False):
            voice_cfg.setdefault("memory", {})["record_history"] = False
    adb, device, toolbox, llm, agent = _ctx(voice_cfg)
    tv_id = voice_cfg.get("adb", {}).get("serial") or voice_cfg.get("adb", {}).get("host") or "default-tv"
    dialogue = VoiceDialogue(voice_cfg, agent=agent, device=device) if intelligent else None
    from .voice_interrupt import InterruptChannel
    interrupts = InterruptChannel(voice_cfg, device=device,
                                  on_event=lambda evt: print(f"[打断] {evt}", flush=True))
    registry = None
    if dialogue is not None:
        from .voice_input import SharedSessionStore, VoiceInputRegistry
        shared_dir = (voice_cfg.get("voice", {}).get("conversation") or {}).get("shared_dir") or ""
        registry = VoiceInputRegistry(dialogue,
                                      store=SharedSessionStore(shared_dir) if shared_dir else None,
                                      tv_id=tv_id)
        print(f"[语音] 输入源: xiaomi_logcat + web_microphone（共享会话:{shared_dir or '内存'}）；"
              f"external_asr 未接入", flush=True)

    def handle(utterance):
        print(f"[语音] {describe(utterance)}", flush=True)
        if registry is not None:
            result = registry.submit_text(utterance.text, source="xiaomi_logcat")
            # Generated replies are optional; raw recognized input is never printed here.
            if (voice_cfg.get("voice", {}).get("conversation") or {}).get("print_replies", False):
                print(f"[回复] {result.get('reply', '')}", flush=True)
            print(f"[结果] {result.get('status')}", flush=True)
        else:
            result = agent.run(utterance.text)
            print(f"[结果] {result.get('status')} {result.get('summary')}", flush=True)
        return result

    if args.action == "dialogue":
        print("V4 文字会话。每行一轮；/exit 退出。此入口不收音、不启用 TTS。", flush=True)
        for line in sys.stdin:
            text = line.strip()
            if text == "/exit":
                break
            if not text:
                continue
            result = dialogue.handle(text, tv_id=tv_id, source="text")
            print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0

    def event(evt):
        kind = evt.get("kind")
        if kind == "utterance":
            print(f"[route] {describe_evt(evt)} -> {evt['route']} (handled={evt['handled']})",
                  flush=True)
        elif kind in ("history_ignored", "duplicate_ignored", "stale_dropped",
                      "queue_overflow"):
            print(f"[{kind}] {describe_evt(evt)}", flush=True)
        else:
            print(f"[voice] {evt}", flush=True)

    def describe_evt(evt):
        u = evt.get("utterance") or {}
        return f"len={u.get('text_len')} hash={(u.get('text_hash') or '')[:10]}"

    listener = VoiceListener(voice_cfg, handle, on_event=event,
                             on_interrupt=interrupts.handle)
    print("语音旁听已启动（Ctrl+C 退出）。请在电视上说一句指令。")
    print("说明：只接管白名单指令；小爱仍可能同时应答，冲突由状态核验兜底。")
    try:
        listener.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        print(f"[stats] {listener.stats()}")
    return 0


def cmd_task(args, cfg):
    from .execution import control_task, list_tasks, runtime_dir
    if args.action == "list":
        _print({"tasks": list_tasks(cfg)})
    elif args.action == "status":
        if not args.id or not args.id.isalnum():
            raise ValueError("需要有效 --id")
        _print(json.loads((runtime_dir(cfg) / "tasks" / (args.id + ".json")).read_text()))
    else:
        _print(control_task(cfg, args.id, args.action))
    return 0


def cmd_memory(args, cfg):
    if not cfg.get("memory", {}).get("enabled", False):
        print("记忆未启用：在 config.yaml 设置 memory.enabled: true")
        return 0
    from .memory import MemoryStore
    from .execution import runtime_dir
    path = cfg["memory"].get("path") or runtime_dir(cfg).parent / "memory.sqlite3"
    store = MemoryStore(path)
    if args.action == "stats":
        _print(store.stats())
    elif args.action == "history":
        _print(store.history(args.limit))
    elif args.action == "backup":
        _print({"backup": store.backup(args.path)})
    elif args.action == "cleanup":
        _print(store.cleanup(days=args.days, max_tasks=args.max_tasks))
    return 0


def cmd_skill(args, cfg):
    if not cfg.get("memory", {}).get("enabled", False):
        raise ValueError("Skills管理需要 memory.enabled: true")
    from .memory import MemoryStore
    from .skill_engine import SkillEngine
    from .execution import runtime_dir
    path = cfg["memory"].get("path") or runtime_dir(cfg).parent / "memory.sqlite3"
    memory = MemoryStore(path)
    engine = SkillEngine(memory, cfg)
    if args.action == "list":
        _print({"skills": memory.list_skills()})
    elif args.action == "validate":
        _print(engine.validate_in_simulator(args.id))
    elif args.action == "validate":
        if not args.id:
            raise ValueError("需要 --id")
        _print(engine.validate_in_simulator(args.id))
    elif args.action == "approve":
        if not args.id:
            raise ValueError("需要 --id")
        if not args.confirm:
            raise ValueError("审批生产Skill需要 --confirm；请先查看定义并隔离验证")
        _print(engine.approve(args.id, confirmed=True))
    elif args.action == "disable":
        if not args.id:
            raise ValueError("需要 --id")
        _print(memory.update_skill(args.id, status="disabled"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-tv", description="AI 电视管家（飞牛 NAS）")
    p.add_argument("--config", default=None, help="指定 config.yaml 路径")
    p.add_argument("--version", action="version", version=f"ai-tv {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="自检").set_defaults(func=cmd_doctor)
    sub.add_parser("config", help="查看生效配置").set_defaults(func=cmd_config)
    sub.add_parser("connect", help="连接并验证电视").set_defaults(func=cmd_connect)
    sub.add_parser("devices", help="列出 adb 设备").set_defaults(func=cmd_devices)
    sub.add_parser("info", help="电视信息").set_defaults(func=cmd_info)
    sub.add_parser("apps", help="列出已安装应用").set_defaults(func=cmd_apps)
    sub.add_parser("ui", help="导出当前界面").set_defaults(func=cmd_ui)

    c = sub.add_parser("cmd", help="用自然语言下达指令")
    c.add_argument("instruction")
    c.add_argument("--offline", action="store_true", help="强制离线确定性规划")
    c.add_argument("--json", action="store_true", help="输出完整 JSON")
    c.set_defaults(func=cmd_cmd)

    t = sub.add_parser("tool", help="直接调用某个工具")
    t.add_argument("name")
    t.add_argument("args", nargs="?", default="", help="JSON 参数，如 '{\"text\":\"搜索\"}'")
    t.set_defaults(func=cmd_tool)

    s = sub.add_parser("shot", help="截屏保存到 NAS")
    s.add_argument("path", nargs="?", default="")
    s.set_defaults(func=cmd_shot)

    k = sub.add_parser("key", help="发送按键")
    k.add_argument("key")
    k.set_defaults(func=cmd_key)

    l = sub.add_parser("launch", help="启动应用")
    l.add_argument("app")
    l.set_defaults(func=cmd_launch)

    sub.add_parser("mcp", help="以 MCP stdio 服务运行").set_defaults(func=cmd_mcp)
    sub.add_parser("serve", help="启动 HTTP API").set_defaults(func=cmd_serve)

    media = sub.add_parser("media", help="本机飞牛影视库索引（扫描媒体目录）")
    media.add_argument("action", choices=["rebuild", "search", "resolve", "stats", "serve"])
    media.add_argument("query", nargs="?", default="")
    media.add_argument("--season", type=int, default=None)
    media.add_argument("--episode", type=int, default=None)
    media.add_argument("--limit", type=int, default=10)
    media.set_defaults(func=cmd_media)

    voice = sub.add_parser("voice", help="电视语音旁听（小米 ASR 文本）")
    voice.add_argument("action", choices=["listen", "test", "dialogue"])
    voice.add_argument("--force", action="store_true", help="忽略配置开关临时启用")
    voice.set_defaults(func=cmd_voice)

    task = sub.add_parser("task", help="查看、暂停、继续或取消运行中的任务")
    task.add_argument("action", choices=["list", "status", "pause", "resume", "cancel"])
    task.add_argument("--id", default=None)
    task.set_defaults(func=cmd_task)

    memory = sub.add_parser("memory", help="查看、备份和清理脱敏长期记忆")
    memory.add_argument("action", choices=["stats", "history", "backup", "cleanup"])
    memory.add_argument("--path", default=None, help="备份目标文件")
    memory.add_argument("--limit", type=int, default=20)
    memory.add_argument("--days", type=int, default=30)
    memory.add_argument("--max-tasks", type=int, default=10000)
    memory.set_defaults(func=cmd_memory)

    skill = sub.add_parser("skill", help="管理需验证和审批的声明式 Skills")
    skill.add_argument("action", choices=["list", "validate", "approve", "disable"])
    skill.add_argument("--id", default=None)
    skill.add_argument("--confirm", action="store_true", help="明确确认审批")
    skill.set_defaults(func=cmd_skill)

    sc = sub.add_parser("schedule", help="定时任务")
    sc.add_argument("action", choices=["add", "list", "remove", "run"])
    sc.add_argument("--command", default=None)
    sc.add_argument("--kind", default="once", choices=["once", "interval", "cron"])
    sc.add_argument("--at", default=None, help="一次性时间 YYYY-MM-DD HH:MM")
    sc.add_argument("--in", dest="in_", type=int, default=None, help="N 秒后执行")
    sc.add_argument("--every", type=int, default=None, help="每隔 N 秒")
    sc.add_argument("--cron", default=None, help="5 段 cron")
    sc.add_argument("--id", default=None)
    sc.add_argument("--once", action="store_true", help="只执行当前到期任务后退出")
    sc.set_defaults(func=cmd_schedule)

    d = sub.add_parser("demo", help="内置模拟电视全链路演示（无需真机）")
    d.add_argument("instruction", nargs="?", default=None)
    d.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    try:
        if args.cmd in ("key", "launch", "tool", "connect", "shot", "ui", "info", "apps"):
            from .execution import device_lease
            with device_lease(cfg):
                return args.func(args, cfg)
        return args.func(args, cfg)
    except (ValueError, OSError) as e:
        print(f"[参数或文件错误] {e}", file=sys.stderr)
        return 2
    except AdbError as e:
        print(f"[ADB 错误] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
