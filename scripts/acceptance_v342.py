#!/usr/bin/env python3
"""Wait for the TV's ADB port, then run the V3.4.2 real-device acceptance suite.

This is intentionally separate from the test suite: it drives the real TV and
records what actually happened, including a five-minute long-playback probe.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

LOG = Path("/tmp/v342_acceptance.log")


def serial() -> str:
    """Device serial comes from config or AI_TV_SERIAL — never hardcoded."""
    import os
    explicit = os.environ.get("AI_TV_SERIAL", "").strip()
    if explicit:
        return explicit
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from adbtv.config import load_config
    adb = load_config().get("adb", {}) or {}
    value = (adb.get("serial") or "").strip()
    if not value:
        raise SystemExit("未配置 adb.serial，且未设置 AI_TV_SERIAL")
    return value


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def wait_for_device(target: str, limit_seconds: int = 1800) -> bool:
    deadline = time.monotonic() + limit_seconds
    while time.monotonic() < deadline:
        subprocess.run(["adb", "disconnect", target], capture_output=True)
        result = subprocess.run(["adb", "connect", target], capture_output=True, text=True)
        if "connected" in (result.stdout + result.stderr):
            log("ADB 已连接")
            return True
        time.sleep(10)
    return False


def main() -> int:
    script = Path(__file__).resolve().parent
    root = script.parent
    sys.path.insert(0, str(root))
    from adbtv.cli import _ctx
    from adbtv.config import load_config

    target = serial()
    log(f"等待电视 ADB 可用…（{target}；在电视上开启 设置 → 账号与安全 → ADB 调试）")
    if not wait_for_device(target):
        log("超时：仍未连上，验收未执行")
        return 1

    cfg = load_config()
    _, device, _, _, agent = _ctx(cfg)
    log(f"电源状态: {device.power_state()}")

    results = []

    def run(instruction: str, label: str):
        started = time.monotonic()
        outcome = agent.run(instruction)
        elapsed = round(time.monotonic() - started, 1)
        status = outcome.get("status")
        playback = device.playback_status(include_ui=False)
        record = {"case": label, "instruction": instruction, "status": status,
                  "elapsed": elapsed, "playing": playback.get("playing"),
                  "state": playback.get("state"), "package": playback.get("package"),
                  "summary": outcome.get("summary")}
        results.append(record)
        log(f"{label:16s} {instruction:26s} -> {status:9s} {elapsed:5.1f}s "
            f"playing={record['playing']} state={record['state']}")
        return outcome

    # 1) 唤醒
    woke = device.wake()
    log(f"唤醒结果: {woke}")

    # 2) 直连播放 / 暂停 / 继续
    run("播放穿成野神第一部第一集", "direct_play")
    run("暂停播放", "pause")
    run("继续播放", "resume")

    # 3) 长片播放：保持播放 5 分钟后再拖动
    log("开始 5 分钟长片播放测试（期间不操作电视）…")
    playing = device.playback_status(include_ui=False).get("playing")
    if playing:
        time.sleep(300)
        now = device.playback_status(include_ui=False)
        log(f"5 分钟后状态: playing={now.get('playing')} state={now.get('state')}")
        seek = agent.run("跳到10分钟")
        after = device.playback_status(include_ui=False)
        results.append({"case": "seek_after_5min", "status": seek.get("status"),
                        "elapsed": seek.get("elapsed"), "playing": after.get("playing"),
                        "state": after.get("state"), "summary": seek.get("summary")})
        log(f"5 分钟后拖动 -> {seek.get('status')} playing={after.get('playing')}")
    else:
        log("未处于播放状态，跳过长片测试")

    # 4) 收尾：暂停，避免一直播放
    run("暂停播放", "final_pause")

    summary = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}
    Path("/tmp/v342_acceptance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log("验收完成，结果写入 /tmp/v342_acceptance.json")

    from adbtv.voice import VoiceListener
    log(f"语音监听健康: {VoiceListener.worker_status(cfg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())