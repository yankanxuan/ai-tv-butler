#!/usr/bin/env python3
"""Open-source candidate research (metadata only, no installs, no devices).

Queries PyPI for version, license and wheel size so the choice is based on facts
about the artefact we would actually ship — not on a project README's claims.

    python3 scripts/research_oss_poc.py            # needs network
    https_proxy=http://127.0.0.1:7890 python3 scripts/research_oss_poc.py

Exit code 0 even when the network is unavailable: this is research tooling, and
an offline run reports "unknown" rather than inventing data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

PACKAGES = {
    "alpinejs": {"kind": "frontend", "note": "vendored locally, MIT"},
    "pydantic": {"kind": "python", "note": "structured validation, MIT"},
    "playwright": {"kind": "test", "note": "e2e tests only, Apache-2.0"},
    "sherpa-onnx": {"kind": "candidate", "note": "optional ASR, Apache-2.0"},
    "melotts": {"kind": "candidate", "note": "optional TTS, weights need review"},
    "mcp": {"kind": "candidate", "note": "official MCP SDK, MIT"},
    "uiautomator2": {"kind": "candidate", "note": "device-side helper, MIT"},
}


def fetch(name: str, timeout: float = 12.0) -> dict:
    url = f"https://pypi.org/pypi/{name}/json"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def summarize(name: str, data: dict) -> dict:
    info = data.get("info", {})
    wheels = []
    for entry in data.get("urls", []):
        filename = entry.get("filename", "")
        if not filename.endswith(".whl"):
            continue
        if "cp311" in filename or "py3-none" in filename:
            wheels.append({"file": filename, "mb": round(entry.get("size", 0) / 1048576, 1)})
    wheels.sort(key=lambda item: item["mb"])
    return {"name": name, "version": info.get("version"),
            "license": (info.get("license") or "").strip()[:60] or None,
            "requires_python": info.get("requires_python"),
            "smallest_wheel": wheels[0] if wheels else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()
    rows = []
    for name, meta in PACKAGES.items():
        row = {"name": name, **meta}
        try:
            row.update(summarize(name, fetch(name, args.timeout)))
        except Exception as exc:  # noqa: BLE001 - offline is a valid outcome
            row.update(version=None, license=None, error=f"{type(exc).__name__}: {exc}"[:80])
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps({
        "note": "元数据仅代表上游发布物；性能与模型许可证必须在本机实测/逐个确认",
        "proxy_used": bool(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")),
        "packages": len(rows),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())