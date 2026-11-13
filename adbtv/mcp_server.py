"""MCP 服务：基于 stdio 的 JSON-RPC 2.0（newline-delimited）。

实现 MCP 的 initialize / tools/list / tools/call / ping 等基本方法，
把 adb 电视控制能力暴露给任意 MCP 客户端（如 Cherry Studio、Claude Desktop 等）。
"""
from __future__ import annotations

import json
import math
import sys
from typing import Any

from .adb import AdbClient
from .config import load_config
from .device import TVDevice
from .tools import ToolBox
from . import __version__

PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.adb = AdbClient(cfg)
        self.device = TVDevice(self.adb, cfg)
        self.toolbox = ToolBox(self.device)
        self._initialized = False

    # ---------- JSON-RPC 分发 ----------
    def handle(self, req: Any) -> dict[str, Any] | None:
        # Invalid envelopes are requests with an unknown id, not notifications.
        if not isinstance(req, dict):
            return self._error(None, -32600, "Invalid Request")
        req_id = req.get("id")
        valid_id = (req_id is None or type(req_id) in (str, int)
                    or type(req_id) is float and math.isfinite(req_id))
        if (req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str)
                or not valid_id):
            return self._error(req_id if valid_id else None, -32600, "Invalid Request")
        method = req["method"]
        is_notification = "id" not in req

        def ok(result: Any) -> dict[str, Any] | None:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        def err(code: int, msg: str) -> dict[str, Any] | None:
            return None if is_notification else self._error(req_id, code, msg)

        params = req.get("params", {})
        if not isinstance(params, dict):
            return err(-32602, "Invalid params: expected object")
        try:
            if method == "initialize":
                self._initialized = True
                return ok({
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "adb-tv-mcp", "version": __version__},
                })
            if method in ("notifications/initialized", "initialized", "ping"):
                return ok({})
            if method == "tools/list":
                return ok({"tools": self.toolbox.specs()})
            if method == "tools/call":
                if (not isinstance(params.get("name"), str)
                        or not isinstance(params.get("arguments", {}), dict)
                        or set(params) - {"name", "arguments"}):
                    return err(-32602, "Invalid params: name and object arguments required")
                try:
                    from .execution import device_lease
                    # Reads also acquire the device lease: another request must
                    # not change the screen between one task's observations.
                    with device_lease(self.cfg):
                        result = self.toolbox.call(params["name"], params.get("arguments", {}))
                    text = json.dumps(result, ensure_ascii=False, default=str)
                    failed = isinstance(result, dict) and result.get("ok") is False
                    return ok({"content": [{"type": "text", "text": text}], "isError": failed})
                except Exception as e:  # noqa
                    return ok({"content": [{"type": "text", "text": f"错误: {e}"}], "isError": True})
            return err(-32601, f"Method not found: {method}")
        except Exception as e:  # noqa
            return err(-32603, f"Internal error: {e}")

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    def serve_stdio(self) -> None:
        # 注意：stdout 仅用于协议消息；日志一律走 stderr。
        # 用 readline() 逐行读取（不要用 for line in sys.stdin：管道保持打开时会缓冲阻塞）。
        while True:
            line = sys.stdin.readline()
            if line == "":
                break
            line = line.strip()
            if not line:
                continue
            try:
                def reject_constant(value: str) -> Any:
                    raise ValueError(f"Invalid JSON constant: {value}")
                req = json.loads(line, parse_constant=reject_constant)
            except (json.JSONDecodeError, ValueError):
                resp = self._error(None, -32700, "Parse error")
            else:
                resp = self.handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> None:
    cfg = load_config()
    server = MCPServer(cfg)
    print(f"[adb-tv-mcp] 启动，目标={cfg['adb'].get('serial') or cfg['adb'].get('host')}",
          file=sys.stderr)
    server.serve_stdio()


if __name__ == "__main__":
    main()
