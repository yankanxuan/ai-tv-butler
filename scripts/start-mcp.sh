#!/usr/bin/env bash
# 以 MCP stdio 服务方式运行 adb-tv-mcp（供 MCP 客户端拉起，一般不用手动执行）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 -m adbtv.mcp_server
