#!/usr/bin/env python3
"""Long-playback acceptance: 5 minutes of real TV playback, then Range/seek.

Uses a synthetic 15-minute MP4 so the session logic is exercised on a file long
enough that a 5-minute wait is still mid-playback. Runs against a dedicated
media service on its own port, leaving the production 8899 instance untouched.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG = Path("/tmp/v342_longplay.log")
MEDIA_ROOT = Path.home() / "acceptance"
FILE = MEDIA_ROOT / "long15.mp4"
WAIT_SECONDS = 300
PORT = 8898


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def range_probe(url: str, start: int, length: int = 2048) -> tuple[int, str, int]:
    request = urllib.request.Request(url, headers={
        "Range": f"bytes={start}-{start + length - 1}"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read()
            return response.status, response.headers.get("Content-Range", ""), len(body)
    except urllib.error.HTTPError as exc:
        return exc.code, "", 0


def main() -> int:
    from adbtv.cli import _ctx
    from adbtv.config import load_config
    from adbtv.media_server import MediaServer

    cfg = load_config()
    _, device, _, _, agent = _ctx(cfg)

    server = MediaServer([str(MEDIA_ROOT)], host="", port=PORT,
                         state_dir=Path(cfg["execution"]["state_dir"]).parent, cfg=cfg,
                         advertise_host=cfg.get("media", {}).get("advertise_host", "")).start()
    log(f"临时媒体服务 {server.host}:{server.port}（生产 8899 不受影响）")
    try:
        url = server.url_for(str(FILE))
        log(f"链接: ...{url[-46:]}")
        status, content_range, size = range_probe(url, 0)
        log(f"初始 Range: {status} {content_range} {size}B")

        woke = device.wake()
        log(f"唤醒: woke={woke.get('woke')}")
        launch = device.launch_video(url)
        log(f"播放器: ok={launch.get('ok')} pkg={launch.get('package')}")

        # 等到真正出声
        deadline = time.monotonic() + 60
        playing = False
        while time.monotonic() < deadline:
            state = device.playback_status(include_ui=False)
            if state.get("playing"):
                playing = True
                break
            time.sleep(2)
        log(f"开始出声: playing={playing}")
        if not playing:
            log("未开始播放，长片测试中止")
            return 2

        log(f"保持播放 {WAIT_SECONDS}s …")
        time.sleep(WAIT_SECONDS)

        state = device.playback_status(include_ui=False)
        log(f"5 分钟后: playing={state.get('playing')} state={state.get('state')}")

        # 用同一个链接再请求（验证会话滑动续期）
        status, content_range, size = range_probe(url, 600_000)
        log(f"5 分钟后同链接 Range: {status} {content_range} {size}B")

        # 暂停后再拖动
        paused = agent.run("暂停播放")
        log(f"暂停: {paused.get('status')} {paused.get('summary')}")
        status_after_pause, content_range2, size2 = range_probe(url, 900_000)
        log(f"暂停后 Range: {status_after_pause} {content_range2} {size2}B")

        resumed = agent.run("继续播放")
        log(f"继续: {resumed.get('status')}")
        status3, content_range3, _ = range_probe(url, 60_000)
        log(f"继续后 Range: {status3} {content_range3}")

        verdict = {
            "started": playing,
            "still_playing_after_5min": state.get("playing"),
            "range_after_5min": status,
            "range_while_paused": status_after_pause,
            "range_after_resume": status3,
            "pause_status": paused.get("status"),
            "resume_status": resumed.get("status"),
        }
        Path("/tmp/v342_longplay.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"结论: {verdict}")
        return 0 if status == 206 and status_after_pause == 206 else 1
    finally:
        server.stop()
        log("临时媒体服务已停止")


if __name__ == "__main__":
    sys.exit(main())