#!/usr/bin/env python3
"""不依赖 pytest 的端到端测试：直接跑 `python3 tests/test_suite.py`。"""
import copy
import os
import sys
import tempfile
import unittest
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adbtv.config import DEFAULTS, _deep_merge
from adbtv.adb import AdbClient
from adbtv.device import TVDevice
from adbtv.tools import ToolBox, auto_play
from adbtv.agent import Agent, parse_instruction
from adbtv.llm import LLMClient
from adbtv.scheduler import Scheduler
from adbtv.mcp_server import MCPServer

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def fake_cfg(state_path):
    """Same assertions, but default to the in-process simulator.

    Spawning a Python process per adb call made this suite take ~20 s; the CLI
    simulator path is still exercised explicitly in `SimulatorParityTests`
    below, so both backends stay covered.
    """
    cfg = _deep_merge(DEFAULTS, {})
    mode = os.environ.get("FAKE_ADB_MODE", "inprocess")
    cfg["adb"].update({
        "serial": "fake-tv:5555",
        "fake_state": state_path,
        "auto_connect": True,
        "simulator": "inprocess" if mode == "inprocess" else "",
        "binary": f"{sys.executable} {ROOT}/tests/fake_adb.py",
    })
    cfg["agent"]["use_llm"] = False
    return cfg


class SimulatorParityTests(unittest.TestCase):
    """The on-disk simulator must still behave like the in-process one."""

    def test_cli_simulator_answers_like_inprocess(self):
        import copy
        tmp = tempfile.mkdtemp()
        state = os.path.join(tmp, "tv.json")
        fast = fake_cfg(state)
        slow = copy.deepcopy(fast)
        slow["adb"]["simulator"] = ""
        from adbtv.adb import AdbClient
        self.assertEqual(AdbClient(fast).shell(["getprop", "ro.product.model"]).stdout.strip(),
                         AdbClient(slow).shell(["getprop", "ro.product.model"]).stdout.strip())
        self.assertEqual(AdbClient(fast).shell(["devices"]).returncode, 0)


def main():
    tmp = tempfile.mkdtemp(prefix="aitv_test_")
    state = os.path.join(tmp, "tv.json")
    cfg = fake_cfg(state)
    adb = AdbClient(cfg)
    dev = TVDevice(adb, cfg)
    tb = ToolBox(dev)

    print("[1] 设备连接与基础信息")
    check("adb connect", adb.connect(force=True) == "device")
    info = dev.info()
    check("读取型号", info["model"].startswith("Xiaomi"), info)
    check("分辨率", "1920x1080" in info["resolution"], info)

    print("[2] 应用与界面")
    apps = dev.list_apps()
    check("列出 bilibili 包", "com.xiaodianshi.tv.yst" in apps["third"], apps["third"])
    launch = dev.launch_app("bilibili")
    check("启动应用", launch["ok"] and launch["package"] == "com.xiaodianshi.tv.yst", launch)
    dump = dev.ui_dump()
    check("UI dump 含搜索入口", "搜索" in dump["texts"], dump["texts"])
    check("按键 home", dev.key("home") is not None)

    print("[3] 自动播放全流程")
    res = auto_play(dev, "bilibili", "科技", cfg)
    check("auto_play 成功", res["ok"], res.get("summary"))
    check("播放状态=PLAYING", res["playback"].get("state") == "PLAYING", res["playback"])
    steps = [s["step"] for s in res["log"]]
    check("步骤包含搜索/输入/选择/验证",
          all(x in steps for x in ["launch_app", "tap_search_entry", "input_text",
                                   "tap_first_result", "verify_playback"]), steps)

    print("[4] 指令解析")
    app, q = parse_instruction("打开哔哩哔哩，找个科技视频播放", cfg)
    check("解析应用", app == "bilibili", app)
    check("解析关键词", "科技" in q, q)
    app2, q2 = parse_instruction("在爱奇艺播放美食节目", cfg)
    check("解析爱奇艺", app2 == "iqiyi", app2)

    print("[5] Agent 离线编排")
    agent = Agent(dev, tb, LLMClient(cfg), cfg)
    res2 = agent.run("打开哔哩哔哩，找个科技视频播放")
    check("Agent 结果成功", res2["ok"], res2.get("summary"))
    check("Agent 模式 offline", res2["mode"] == "offline", res2.get("mode"))

    print("[6] 调度器")
    store = os.path.join(tmp, "tasks.json")
    sch = Scheduler(store)
    t1 = sch.add("测试指令A", kind="once", delay=0)
    t2 = sch.add("测试指令B", kind="interval", interval=3600)
    t3 = sch.add("测试指令C", kind="cron", cron="0 8 * * *")
    check("任务数=3", len(sch.tasks) == 3, len(sch.tasks))
    check("cron 有 next_run", t3["next_run"] is not None)
    due = sch.due()
    ran = sch.run_due(lambda c: {"ok": True})
    check("到期任务被执行", any(r["task"]["id"] == t1["id"] for r in ran), ran)
    check("一次性任务执行后禁用", next(t for t in sch.tasks if t["id"] == t1["id"])["enabled"] is False)

    print("[7] MCP 协议")
    mcp = MCPServer(cfg)
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    check("initialize", init["result"]["serverInfo"]["name"] == "adb-tv-mcp", init)
    tl = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    check("tools/list 15 个", len(tl["result"]["tools"]) == 15, len(tl["result"]["tools"]))
    call = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "tv_info", "arguments": {}}})
    check("tools/call tv_info", "Xiaomi" in call["result"]["content"][0]["text"], call)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
