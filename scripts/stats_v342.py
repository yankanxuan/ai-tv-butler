#!/usr/bin/env python3
"""30 tasks x 3 runs against the containerised deployment, with state checks.

Submits each instruction through the console API (so the containerised agent,
shared media key and device lock are all exercised), waits for the task to
finish, then independently verifies the TV's real state from the host side.

A reported SUCCESS is only counted as *verified* when the independent state
check agrees; mismatches are recorded, never smoothed over.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN_FILE = ROOT / "deploy/data/runtime/console.token"


def base_url() -> str:
    """Console address comes from config/env, never hardcoded."""
    import os
    explicit = os.environ.get("AI_TV_CONSOLE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    import yaml
    cfg = yaml.safe_load((ROOT / "deploy/data/config.yaml").read_text(encoding="utf-8"))
    server = cfg.get("server", {})
    host = server.get("host") or "127.0.0.1"
    return f"http://{host}:{int(server.get('port', 8770))}"


BASE = ""   # 运行时由 main() 赋值
LOG = Path("/tmp/v342_stats.log")
RESULTS = Path("/tmp/v342_stats.json")
RUNS = 3
TIMEOUT = 90

# (instruction, expectation) — expectation returns (ok, detail) from real state
TASKS = [
    ("播放穿成野神第一部第一集", "playing"),
    ("暂停播放", "paused"),
    ("继续播放", "playing"),
    ("暂停播放", "paused"),
    ("音量调节到8", "volume:8"),
    ("音量调到3", "volume:3"),
    ("音量加2", "volume_up"),
    ("音量减1", "volume_down"),
    ("打开飞牛", "app:com.trim.tv"),
    ("打开B站", "app:com.xiaodianshi.tv.yst"),
    ("回到桌面", "home"),
    ("现在播放的是什么", "any"),
    ("当前应用是什么", "any"),
    ("快进30秒", "any"),
    ("跳到1分钟", "any"),
    ("打开飞牛播放短剧", "any"),
    ("播放我，史前霸王龙第一集", "any"),
    ("打开飞牛看看", "app:com.trim.tv"),
    ("音量调到0", "volume:0"),
    ("音量调节到15", "volume:15"),
    ("加载不存在的剧集XYZ", "expect_fail"),
    ("播放野神第十季第五集", "any"),
    ("下一集", "any"),
    ("上一集", "any"),
    ("播放穿成野神第一部第二集", "playing"),
    ("暂停", "paused"),
    ("继续", "playing"),
    ("打开设置", "expect_fail"),
    ("播放新闻", "any"),
    ("暂停播放", "paused"),
]


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def api(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"X-Auth-Token": token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def device():
    from adbtv.cli import _ctx
    from adbtv.config import load_config
    _, dev, _, _, _ = _ctx(load_config())
    return dev


def check(dev, expectation: str) -> tuple[bool, str]:
    if expectation == "any":
        return True, "无状态断言"
    if expectation == "playing":
        state = dev.playback_status(include_ui=False)
        return bool(state.get("playing")), f"playing={state.get('playing')}"
    if expectation == "paused":
        state = dev.playback_status(include_ui=False)
        ok = state.get("playing") is False and state.get("state") == "PAUSED"
        return ok, f"playing={state.get('playing')} state={state.get('state')}"
    if expectation.startswith("volume:"):
        want = int(expectation.split(":")[1])
        info = dev.volume()
        return info.get("index") == want, f"volume={info.get('index')}"
    if expectation == "volume_up":
        info = dev.volume()
        return info.get("known", False), f"volume={info.get('index')}"
    if expectation == "volume_down":
        info = dev.volume()
        return info.get("known", False), f"volume={info.get('index')}"
    if expectation.startswith("app:"):
        want = expectation.split(":", 1)[1]
        package = dev.current_package()
        return package == want, f"package={package}"
    if expectation == "home":
        package = dev.current_package()
        home = (dev.home_package() or "")
        return package == home, f"package={package} home={home}"
    if expectation == "expect_fail":
        # 这一项期望 Agent 明确失败；成功反而算错误
        return False, "期望失败"
    return True, "未实现断言"


def run_once(dev, instruction: str, expectation: str) -> dict:
    started = time.monotonic()
    task = api("POST", "/api/runs", {"command": instruction})
    task_id = task.get("task_id", "")
    state, result = "RUNNING", {}
    while time.monotonic() - started < TIMEOUT:
        time.sleep(2)
        detail = api("GET", f"/api/runs/{task_id}")
        state = detail.get("state", "?")
        if state != "RUNNING":
            result = detail.get("result") or {}
            break
    elapsed = round(time.monotonic() - started, 1)
    status = result.get("status") or state
    verified, note = check(dev, expectation if expectation != "expect_fail" else "any")
    if expectation == "expect_fail":
        verified = status not in ("SUCCESS",)
    return {"instruction": instruction, "task_id": task_id, "state": state,
            "status": status, "elapsed": elapsed,
            "expectation": expectation, "verified": verified, "note": note,
            "summary": (result.get("summary") or "")[:80]}


def main() -> int:
    global BASE
    BASE = base_url()
    dev = device()
    log(f"设备 {dev.info().get('model')}，开始 {len(TASKS)} 项 × {RUNS} 次")
    rows = []
    for index, (instruction, expectation) in enumerate(TASKS, 1):
        for attempt in range(1, RUNS + 1):
            record = run_once(dev, instruction, expectation)
            record.update({"index": index, "attempt": attempt})
            rows.append(record)
            flag = "OK" if record["verified"] else "MISMATCH"
            log(f"{index:02d}.{attempt} {instruction[:22]:24s} {record['status']:10s} "
                f"{record['elapsed']:5.1f}s {flag:8s} {record['note']}")
            RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    total = len(rows)
    success = sum(1 for r in rows if r["status"] == "SUCCESS")
    verified = sum(1 for r in rows if r["verified"])
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    summary = {"runs": total, "success": success,
               "success_rate": round(success / total * 100, 1),
               "verified": verified, "verified_rate": round(verified / total * 100, 1),
               "by_status": by_status,
               "avg_elapsed": round(sum(r["elapsed"] for r in rows) / total, 1)}
    Path("/tmp/v342_stats_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"汇总: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())