"""AI 电视管家（跑在飞牛 NAS 上）。

模块划分：
  config   —— 读取 config.yaml / 环境变量
  adb      —— 对 adb 命令行的薄封装（可指向真机或内置模拟器）
  device   —— 电视高层操作（按键、输入、启动应用、UI dump、截图、验证）
  apps     —— 常见 TV 应用包名映射与解析
  tools    —— 供 Agent / MCP 复用的工具定义与分发
  llm      —— 本地 / 云端大模型客户端（Anthropic 与 OpenAI 兼容两种）
  agent    —— 指令 -> 规划 -> 调工具 -> 验证 的编排循环
  mcp_server —— 基于 stdio 的 MCP(JSON-RPC 2.0) 服务
  scheduler —— 简单可靠的定时 / 一次性任务调度
  server   —— 可选 HTTP API
  cli      —— 命令行入口
"""

__version__ = "4.4.0"
