# V3 交付清单与验收记录

本文件记录 V2.1 → V2.2 → V3 升级的实际交付内容、测试结果、真机验收状态与未解决问题。所有“已验证”项均基于真实执行的测试或真机操作；未真实运行的一律标注“待实机验收”。

## 一、基线与回滚

- 升级前项目无 Git 仓库。已创建私有源码快照：
  - `~/ai-tv-butler-backups/pre-v21-20261110-171130.tar.gz`
  - SHA256 `2a7e7bbea1b69ef04fd194df738783c5dfbcfc3bbbdd4eca0eb7724394f8f709`
- 升级基线测试：原 22 项集成检查 + 17 项快速路径测试全部通过。
- 回滚方法见 [DEPLOY.md](DEPLOY.md) 的“回滚”节（`scripts/release.py --restore` 解包到新目录，不覆盖现有项目）。

## 二、变更文件清单

### 新增
- `adbtv/perception.py` — 统一 `ScreenState` 感知（XML+窗口+媒体/音频+可选视觉），缓存/指纹/动态等待/点击前重定位。
- `adbtv/verifier.py` — `GoalSpec`/`VerificationResult`，应用/内容/播放/进度分级核验，SUCCESS/PARTIAL/FAILED/UNKNOWN。
- `adbtv/execution.py` — 任务生命周期、跨进程设备互斥锁、暂停/继续/取消、超时与动作上限、原子 JSON。
- `adbtv/recovery.py` — 失败分类与有界恢复、人工接管。
- `adbtv/memory.py` — SQLite 脱敏长期记忆（任务/路径/页面/恢复/偏好/Skills），WAL、原子迁移、备份、清理。
- `adbtv/skill_engine.py` — 声明式白名单 Skills；隔离验证 + 人工审批；不执行任意代码。
- `adbtv/auth.py` — 控制台令牌生成与常量时间校验。
- `adbtv/web/index.html` — 单页认证控制台（无前端框架）。
- `adbtv/intent.py` — 常用指令本地解析（遥控键/飞牛片名季集）。
- `adbtv/feiniu.py` — 飞牛按页面导航（首页/列表/详情/季/文件/播放器），骨架屏检测。
- `scripts/release.py`、`scripts/migrate.py` — 快照/回滚与配置迁移。
- `Dockerfile`、`docker-compose.yml` — 无特权容器部署。
- 测试：`tests/test_execution_recovery.py`、`test_perception_verifier.py`、`test_memory_skills.py`、`test_web_console.py`、`test_tool_security.py`、`test_scheduler_reliability.py`、`test_agent_v21.py`、`test_fast_path.py`。

### 修改
- `adbtv/agent.py` — 接入感知/核验/恢复/记忆/Skills；目标解析保留片名季集；权威确定性路径失败不再跌入 LLM 循环；status 规范为字符串。
- `adbtv/device.py` — `playback_status` 多证据融合 + `audio_track_paused` 检测；UI 解析含父子/焦点；ADB 错误检查。
- `adbtv/adb.py` — 远端 shell 参数 `shlex.join` 引用；checkpoint 钩子（线程局部，防递归）；`CmdResult.check_returncode`。
- `adbtv/tools.py` — 工具运行时参数校验；`run_flow` 失败即中止；截图入口限制。
- `adbtv/mcp_server.py` — 严格 JSON-RPC 信封/通知/错误码；tools/call 设备互斥。
- `adbtv/scheduler.py` — 跨进程领取、租约、有限重试、并发合并、cron 校验/时区。
- `adbtv/server.py` — 认证控制台 API、异步任务/SSE、模型配置写回、设备读取互斥。
- `adbtv/cli.py` — `task`/`memory`/`skill` 子命令；操作类命令设备互斥。
- `adbtv/config.py` — `execution`/`perception`/`recovery`/`memory`/`voice` 配置；server 默认回环。
- `config.yaml`、`config.example.yaml`、`README.md`、`docs/ARCHITECTURE.md`、`Makefile`、`pyproject.toml`。

## 三、自动化测试结果（真实运行）

```
make test
  → python3 tests/test_suite.py        22 项集成检查通过
  → unittest discover tests            132 项单元测试通过（V3.0 时点；后续版本已增至 288 项）
```

覆盖：ADB 连接/重连、UI XML 解析、视觉不可用回退、页面变化检测、动态元素定位、播放状态验证（含暂停/跨应用/无证据负例）、错误分类与有界恢复、任务暂停/继续/取消、设备并发互斥、MCP 工具调用与畸形输入、SQLite 持久化/迁移/脱敏/并发、Skills 验证-审批-撤销、调度跨进程领取/租约/重试/cron、认证控制台匿名拒绝/异步任务/SSE/审批确认、目标解析保留片名季集、权威路径不跌入 LLM。

> 自动化测试使用内置模拟电视与内存设备，不依赖真机，可重复执行。

## 四、真机验收（小米 MiTV-ASTP0 / Android 9 / 192.0.2.20:5555）

### 已真机验证（飞牛媒体服务正常时，本日早段）
- 打开飞牛TV：前台包核验 SUCCESS。
- 通用短剧播放：首页 → 短剧 → 首部 → 播放，`audio_track_started` 确认，约 11.8s。
- 指定片名季集：《穿成野神我靠香火斩诸神》第一季第一集，播放器标题 `S01E001` 确认，约 22.5s。

### 已真机验证（本次升级后）
- 打开飞牛：SUCCESS，5.5s，前台包核验。
- 权威确定性路径失败时**不再跌入 180s LLM 循环**：内容未加载时约 27s 快速失败，状态 PARTIAL（应用已确认、播放未知），未伪造成功。
- `resolve_package` 标签映射：飞牛TV/飞牛/红果 → 正确包名（单元测试 + 真机列表）。
- 控制台认证：匿名请求 401，令牌请求 200，token 文件 0600（本地 HTTP 集成测试 + 真机回环验证）。
- 任务历史写入 SQLite，统计可读。

### 待实机验收（受环境阻塞，非代码缺陷）
- **飞牛短剧/指定剧播放（当前）**：本次验收时段飞牛首页持续停留在 `skeleton_view` 加载占位，媒体库内容未加载（飞牛后端/媒体服务未响应）。同一代码路径在本日早段飞牛服务正常时已成功播放。代码侧已新增骨架屏检测，会等待完整导航预算并给出明确“内容仍在加载/媒体服务未响应”诊断，而非伪造成功。待飞牛媒体服务恢复后复测。
- **暂停/继续（真机内容）**：逻辑已修复（播放中“继续”为无操作、不误暂停；暂停态用 DPAD center 切换；`audio_track_paused` 作为暂停证据），单元测试覆盖。真机端到端暂停/继续需有可播放内容，待飞牛服务恢复后复测。
- **浏览器语音输入**：Web Speech 入口已实现，需手机/电脑浏览器麦克风与 HTTPS/localhost 授权，待真实浏览器验收。
- **视觉模型回退**：当前本地网关纯文本、无 vision_model；视觉路径有单元测试，待配置支持视觉的云端模型后真机验收。
- **Docker 镜像构建**：当前环境 Docker daemon 不可访问，Dockerfile/compose 未实际构建，待部署环境验收。
- **任务书要求的 30 任务 ×3 次 90% 真机成功率**：尚未完整执行，不以模拟结果冒充。

## 五、未解决问题与已知限制

1. 飞牛媒体库内容加载依赖其后端服务；服务未响应时无法播放（环境依赖，已优雅失败并诊断）。
2. 遥控器“人工接管”检测为启发式（前台应用/焦点在无 Agent 动作期间变化即暂停）；ADB 不提供按键来源，不能保证 100% 识别冲突。
3. 指定内容核验依赖播放器 UI 文本或归属明确的媒体元数据；DRM/SurfaceView 黑屏且无元数据时保守返回 PARTIAL/UNKNOWN，不伪造成功。
4. 进度核验需真实数值位置；飞牛等不注册 MediaSession 的应用可能无 position，`require_progress` 时返回 UNKNOWN。
5. 调度崩溃窗口无法保证副作用 exactly-once；过期租约明确记录“结果未知”并有限重试。
6. Skills 隔离验证使用模拟电视，非生产兼容性保证；生产首次使用仍需真机验收。

## 六、安全要点（已实现）

- 控制台默认仅回环、强制令牌；空令牌自动生成 0600 文件，绝不匿名放行。
- 模型只能调用白名单工具，无任意 shell；工具参数与 MCP 请求代码层校验；电视端 shell 参数单独引用。
- 工具入口截图仅限 `agent.log_dir` 内不存在的 PNG，不能覆盖 NAS 任意文件。
- 屏幕/工具输出标记为不可信数据，防提示注入；模型 `done` 不绕过核验。
- 记忆库递归脱敏，不存原始 UI/图片/密码/令牌/输入框内容。
- Skills 为 JSON 数据，需隔离验证 + 人工审批，修改定义撤销审批；不修改生产源码。
- 容器无特权、只读根、cap_drop、no-new-privileges。

## 七、部署与版本

- 部署/迁移/Docker/回滚见 [DEPLOY.md](DEPLOY.md)。
- 架构与安全边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。
- 当前版本 `3.0.0`（`adbtv/__init__.py`、`pyproject.toml`）。
