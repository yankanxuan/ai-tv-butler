#!/usr/bin/env python3
"""Probe console, scheduler, voice listener or media service health."""
import json
import os
import sys
import urllib.request

# 允许直接从仓库运行（容器里已安装，此路径不留副作用）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from adbtv.config import load_config


def main(service="console"):
    cfg = load_config()
    if service == "voice":
        from adbtv.voice import VoiceListener
        # 监听器存活由实例方法给出（需要 cfg）；无实例时回落到心跳文件。
        status = VoiceListener.worker_status(cfg)
        adb_connected = False
        try:
            from adbtv.adb import AdbClient
            adb_connected = AdbClient(cfg).state() == "device"
        except Exception:
            adb_connected = False
        print(json.dumps({"service": "voice", "listener_online": bool(status.get("online")),
                          "listener_status": status.get("status"),
                          "adb_connected": adb_connected,
                          "log_age_seconds": ((status.get("stats") or {}).get("last_line_at") or 0)
                          and round(__import__("time").time()
                                    - float((status.get("stats") or {}).get("last_line_at") or 0), 1),
                          "stats": status.get("stats")}, ensure_ascii=False))
        if not status.get("online"):
            raise SystemExit(1)
        if not adb_connected:
            raise SystemExit(2)
        return
    if service == "media":
        media = cfg.get("media", {}) or {}
        from adbtv.media_address import resolve_media_host
        host = resolve_media_host(cfg, host=media.get("serve_host", ""),
                                  advertise_host=media.get("advertise_host", ""),
                                  allow_loopback=True)
        port = int(media.get("serve_port", 8899))
        url = f"http://{host}:{port}/readyz"
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                url, timeout=3) as response:
            if response.status != 200:
                raise SystemExit(1)
        return
    if service == "scheduler":
        from adbtv.scheduler import Scheduler
        status = Scheduler(cfg["scheduler"]["store"]).worker_status()
        if not status["online"]:
            raise SystemExit(1)
        return
    if service != "console":
        raise ValueError("unknown healthcheck service")
    server = cfg.get("server", {})
    host = server.get("host", "127.0.0.1")
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{int(server.get('port', 8770))}/health"
    # Local health must not accidentally use a configured HTTP proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=3) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", nargs="?", default="console",
                        choices=("console", "scheduler", "voice", "media"))
    main(parser.parse_args().service)
