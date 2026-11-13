# 优化进度记录（按 OPTIMIZATION_GOALS.md 实施）

> 本文件为 2026-11-10 的 V3.1 历史优化快照，仅记录当时实际执行的修改与验证；下列“本轮/当前/待验收”均指该历史阶段。V4.0.0b8 当前 Docker、ASR、恢复和测试状态统一见 [b8 交付报告](V408_DELIVERY.md) 与 [最后一轮工程验收](B8_FINAL_ENGINEERING.md)，不得用本页旧待办覆盖当前验收。
>
> 当时未运行的项明确标注“待验收”。

## 已完成的 P0/P1（本轮）

### 控制台与真实入口
- 匿名首页可加载登录表单；设备/任务/截图/配置/记忆接口仍强制认证（`server.py`）。
- 实时进度改为带 `X-Auth-Token` 的流式读取，移除 URL 查询令牌；未知任务 404、终态关闭、断线手动恢复、连接数上限与心跳。
- 模型配置保存修复 `tempfile.mkstemp`，原子写入、非法值不改原文件、凭证递归脱敏；无配置文件时写入运行时目录。
- 任务执行改为固定 worker + 有界队列，排队超时/满队列明确 429，排队取消立即终态；任务 cfg 快照隔离。
- 控制台可读取独立调度器心跳，在线状态不伪造；控制台可选内嵌调度（`scheduler.embedded`，默认关闭）。

### 目标保真
- 新增 `intent.parse_request`：完整请求计划（open/play/search/pause/resume/home/back/volume/query/next/prev/seek/seek_to/delay/sequence），保留书名号标题大小写与季集，明确应用继承，未知整体拒绝，不执行前缀。
- 复合请求分解为顺序步骤，不再只执行播放前缀；`播放X十分钟后暂停` 生成 play + delay(pause)。

### 播放与遥控核验
- 已释放/停止的 AudioTrack 不再作为在播/暂停证据；新音轨可覆盖旧轨道。
- 指定内容：播放器 XML 或归属明确媒体元数据确认身份后，可结合目标播放器活动音轨判定在播；身份未知仍保守 PARTIAL/UNKNOWN。
- 新增 `device.transport / skip_episode / seek_to`：仅在确认播放器与状态下控制，飞牛用 CENTER、其他用媒体键、已满足状态 no-op；不支持的能力返回 UNSUPPORTED 不盲按。
- 新增 home/volume/search 语义核验：home 校验实际桌面包，volume 读取前后音量（含边界），search 校验查询出现在界面。
- 中文输入：检查并临时切换 ADBKeyboard，广播后核验输入框真实文本，结束恢复原输入法；不可用时明确失败。

### 恢复与调度
- 失败结果统一进入分类恢复；`retry=true` 后真正再次执行步骤（有限次数/时间预算），超限交人工。
- 调度：CANCELLED 终态不重试；独立心跳线程覆盖长任务；`worker_status()` 按新鲜度判定在线；安全 `update(enabled/command)`。
- 回归盲区修复：真实 managed 路径、匿名浏览器入口、无隐含认证头 SSE、配置保存、组合核验、恢复器集成均有测试。

## 实际验证结果

- `make test`：22 项集成检查 + **288 项单元测试**，全部通过。
- 模拟电视（内置 `adbtv.simulator`）实测：
  - 打开B站→搜索科技→播放：SUCCESS（约 8.1s）
  - 暂停 1.6s、继续 1.8s、查询当前播放 0.8s，均 SUCCESS
  - 中文输入“科技”：走 ADBKeyboard，输入框文本核验通过，原输入法已恢复
- 真实小米电视 MiTV-ASTP0（Android 9，192.0.2.20:5555）实测：
  - 打开B站 / 打开飞牛 / 回到桌面 / 音量加 / 音量减 / 当前应用 / 当前播放：均 SUCCESS
  - 飞牛短剧播放：PARTIAL（媒体库内容未加载，播放证据不足）——环境依赖，未伪造成功
- 部署：10 项部署测试通过；`docker compose config --quiet` 通过；无网络 wheel 构建与只读安装树资源检查通过。

## 未完成 / 待验收

- Docker 镜像实际构建与容器运行：当前 daemon socket 权限不足，**未构建**，待部署环境验收。
- systemd 服务实际安装启动：未执行。
- 真实浏览器（非 Node 脚本）交互与移动端：待人工验收。
- 飞牛媒体库接口/索引（P2）：尚未核实可用接口，未实现。
- 多轮会话上下文默认关闭（`memory.store_context`），仅基础设施就绪。
- 任务书 30 项 ×3 次真机成功率：**未执行**，不得以模拟结果代替。
- 飞牛媒体服务无响应时播放会保守 PARTIAL；需服务恢复后复测真实播放/暂停/继续。

## 回滚

- 本轮前快照：`ai-tv-butler-backups/ai-tv-butler-pre-optimization-20261110.tar.gz`（SHA256 `8cb598d9…`）
- 本轮进度快照：`ai-tv-butler-backups/ai-tv-butler-v3.1.0-optimization-wip.tar.gz`（SHA256 `f45b8f44…`）
- 恢复：`python3 scripts/release.py --restore <archive> --destination <empty-dir>`
