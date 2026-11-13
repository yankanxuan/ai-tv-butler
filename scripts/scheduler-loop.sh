#!/usr/bin/env bash
# 常驻调度循环：每分钟检查到期任务并执行
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 bin/ai-tv schedule run
