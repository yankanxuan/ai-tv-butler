#!/usr/bin/env python3
"""Verify the containerised voice listener end to end from the host side.

The listener runs inside the voice container, so its stdout lives in `docker
logs` which this environment cannot read. Instead we watch the shared heartbeat
file: every executed utterance bumps `executed` and `last_utterance_at`, which
proves the chain logcat -> parse -> route -> agent actually ran.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HEARTBEAT = ROOT / "deploy/data/runtime/voice_worker.json"
LOG = Path("/tmp/v342_voice.log")
WAIT = 900


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def stats() -> dict:
    try:
        return json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    before = stats()
    base = (before.get("stats") or {})
    log(f"基线: executed={base.get('executed')} expired={base.get('expired')} "
        f"status={before.get('status')}")
    log(f"请对电视说话（最多等 {WAIT}s）…")
    baseline_executed = int(base.get("executed") or 0)
    baseline_last = float(base.get("last_utterance_at") or 0)
    deadline = time.monotonic() + WAIT
    seen = []
    while time.monotonic() < deadline:
        time.sleep(3)
        current = stats()
        now_stats = current.get("stats") or {}
        executed = int(now_stats.get("executed") or 0)
        last = float(now_stats.get("last_utterance_at") or 0)
        if last != baseline_last:
            baseline_last = last
            seen.append({"at": last, "executed": executed,
                         "expired": now_stats.get("expired")})
            log(f"捕获到语音: executed={executed} expired={now_stats.get('expired')}")
        if len(seen) >= 3:
            break
    final = stats()
    verdict = {
        "utterances_seen": len(seen),
        "executed_total": (final.get("stats") or {}).get("executed"),
        "executed_delta": (final.get("stats") or {}).get("executed", 0) - baseline_executed,
        "expired_total": (final.get("stats") or {}).get("expired"),
        "listener_status": final.get("status"),
        "details": seen,
    }
    Path("/tmp/v342_voice.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"结论: {verdict}")
    return 0 if seen else 1


if __name__ == "__main__":
    sys.exit(main())