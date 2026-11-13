#!/usr/bin/env bash
# 离线演示：用内置模拟电视跑通「打开应用→搜索→选择→播放→验证」全链路
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 bin/ai-tv demo "$@"
