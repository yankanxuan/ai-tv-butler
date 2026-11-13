# AI TV Butler V4.0.0b8 Voice Beta · 飞牛 NAS 电视智能体

ADB 电视控制程序：自然语言或电视语音 -> 感知 -> 规划 -> 白名单操作 -> 证据核验 -> 有界恢复。支持小米 Android TV、本机飞牛影视库直连、电视 ASR 旁听、15 个 MCP 工具、CLI、调度、SQLite 记忆、审批 Skills 和认证 Web 控制台。

## 功能与版本

- **V2.1**：统一 `ScreenState`；XML/窗口/媒体/音频感知，可选视觉回退；任务状态、跨进程设备互斥、超时/取消/暂停；分类恢复；应用、内容、播放、进度分级核验。
- **V2.2**：SQLite 长期记忆、路径可靠性、恢复统计、明确偏好；JSON Skills 先隔离验证、再人工审批；保留旧 Flow 和快速路径。
- **V3**：认证浏览器控制台、异步任务/SSE、截图与历史、模型配置、Skills/调度管理、经验统计；可选浏览器麦克风输入；无特权 Docker 部署文件。
- **V3.2**：本机飞牛影视库索引（片名/季/集 -> 真实文件），Range 媒体服务，电视直连播放（指定剧集 2.3 秒）。见 [媒体库直连](docs/MEDIA_LIBRARY.md)。
- **V3.3**：飞牛TV 内播放提速（整屏滑动查找、点击即验证），续作名保护避免播错内容。
- **V3.4**：电视语音操控——旁听小米 ASR 文本，白名单路由，真人语音端到端验收通过。见 [电视语音操控](docs/VOICE_CONTROL.md)。
- **V3.4.1**：稳定性修复——语音不再重放历史/持久化去重/有界队列；路由改意图组合；媒体改用短时按文件签名链接；Compose 增加 voice 服务；测试提速约一倍。见 [变更记录](CHANGELOG.md)。
- **V3.4.2**：可靠性补丁——媒体鉴权不再匿名放行（播放会话 + 共享签名）、媒体服务独立成进程、语音队列计数修复、长期播放可续期、发布包与本地配置分离。见 [变更记录](CHANGELOG.md)。

不依赖前端框架或数据库服务；Python 标准库 + PyYAML。模型服务可使用 Anthropic/OpenAI 兼容接口，非必需。

- **V4.0.0b5（开源优先集成）**：Alpine.js（本地 vendored，CSP 构建）重构语音对话、Pydantic 结构化校验（可回落）、Playwright 真实浏览器 e2e。见 [开源集成](docs/OPEN_SOURCE.md)、[第三方清单](THIRD_PARTY_NOTICES.md)。
- **V4.0.0b4（语音闭环修复）**：共享会话完整替换与过期校验、控制台真正接入 VoiceDialogue、TTS 任务化交付与认证播放。见 [闭环修复](docs/VOICE_V404_RELIABILITY.md)。
- **V4.0.0b3（语音可靠性补丁）**：跨容器多轮确认、TTS 线程自愈、跨容器任务打断、复杂语义与 100 条真实模型样本。见 [可靠性说明](docs/VOICE_V403_RELIABILITY.md)、[测试](docs/VOICE_V403_TESTS.md)。
- **V4.0.0b2（Voice Beta，第一至第五阶段）**：三级语义路由、多轮会话、语音回执（浏览器/手机出声，电视文字）、语音任务打断、可插拔输入。见 [语音 Beta](docs/VOICE_V40_PHASE12.md)、[测试与测量](docs/VOICE_V40_BETA_TESTS.md)。

V3.4.3 仅含可靠性补丁：媒体密钥并发初始化、readiness、电视可达地址发现和公开包安全。
详见 [更新与回滚](docs/V3_4_3_UPDATE.md)、[测试与验收边界](docs/V3_4_3_TEST_RESULTS.md)。

## 快速开始

```bash
cd ~/ai-tv-butler
make test
./bin/ai-tv doctor
./bin/ai-tv demo

# 真实电视（先按 docs/DEPLOY.md 完成ADB授权）
./bin/ai-tv cmd "打开飞牛"
./bin/ai-tv cmd "打开飞牛播放穿成野神我靠香火斩诸神第一部第一集" --json
./bin/ai-tv task list

# 认证控制台
./bin/ai-tv serve
cat state/runtime/console.token
# 浏览器打开 http://127.0.0.1:8770，粘贴token
```

默认只监听本机回环地址。手机局域网访问请按 [部署文档](docs/DEPLOY.md) 显式设置局域网 IP、强 token 和防火墙，不应直接暴露公网。`/health` 是唯一的无认证健康探针。

## V3 控制台

提供：

- 电视状态与截图（DRM 视频层可能黑屏，不以此否定系统播放证据）。
- 文字任务、异步后台执行、实时规划/操作/恢复事件。
- 暂停、继续、取消；进度不只靠按钮命令成功。
- 脱敏任务历史与成功/恢复/模型调用统计。
- Skills 的隔离验证、审批和停用。
- 调度新增/删除；模型供应商/模型名/视觉模型/API Key 配置。

访问令牌为空时自动生成到 `state/runtime/console.token`（0600），不会显示在公共日志中。模型配置写回受保护的 YAML，API Key 不回传浏览器、不入记忆库。

可选 `voice.enabled: true` 使用**手机或电脑浏览器麦克风**的 Web Speech；不支持时仍可文字输入。

电视原生语音已接入：小米电视会把云端 ASR 的识别文本写入 `logcat`，NAS 无需 root 即可旁听并转交 Agent（`ai-tv voice listen`），已真机验收。NAS 仍无法获取原始 PCM，也不做本地 ASR 或唤醒词——唤醒与识别都由电视完成。详见 [电视语音操控](docs/VOICE_CONTROL.md)。

## 记忆与 Skills

```yaml
memory:
  enabled: true
  path: ""  # state/memory.sqlite3
```

```bash
./bin/ai-tv memory stats
./bin/ai-tv memory history
./bin/ai-tv memory backup --path state/memory-backup.sqlite3
./bin/ai-tv memory cleanup --days 30 --max-tasks 10000
./bin/ai-tv skill list
./bin/ai-tv skill validate --id <候选ID>
./bin/ai-tv skill approve --id <候选ID> --confirm
```

Skills 是声明式白名单工具调用，不是可执行源码。候选成功后仍需**隔离验证 + 人工明确批准**，才可匹配触发；修改定义会撤销审批。页面匹配失败、工具失败、验证不完整时不会伪装成功。

记忆采用 SQLite WAL、原子迁移、备份和清理。密码/token、原始 XML、图片、输入框内容等会脱敏或省略。模型不可用、记忆不可用时保留受限确定性流程，并在结果中给出原因。

## 现有 CLI / MCP / 调度

```bash
./bin/ai-tv doctor / info / apps / ui
./bin/ai-tv key home
./bin/ai-tv launch bilibili
./bin/ai-tv cmd "打开哔哩哔哩，找个科技视频播放"
./bin/ai-tv cmd "暂停"
./bin/ai-tv task pause --id <任务ID>
./bin/ai-tv task resume --id <任务ID>
./bin/ai-tv task cancel --id <任务ID>

./bin/ai-tv mcp
./bin/ai-tv schedule add --command "打开飞牛播放一部短剧" --kind cron --cron "0 8 * * *"
./bin/ai-tv schedule run
```

MCP 工具共 15 个：`tv_info` `tv_list_apps` `tv_current_app` `tv_launch_app` `tv_key` `tv_input_text` `tv_tap` `tv_swipe` `tv_ui_dump` `tv_click_text` `tv_screenshot` `tv_wait_text` `tv_playback_status` `tv_auto_play` `tv_run_flow`。

模型只能调用白名单工具，不提供任意 Shell。工具参数和 MCP 请求在代码层验证；电视端 shell 参数单独引用。工具入口的截图只能写入 `agent.log_dir` 下不存在的 PNG 文件，不能直接覆盖 NAS 文件。直接 CLI 截图的设备用法保留。

调度 JSON 支持 once/interval/cron、跨进程领取、过期租约、有限重试、并发合并；支持 `max_retries`、`timezone`、`lease_seconds` 的可选字段。崩溃窗口无法保证副作用 exactly-once，恢复记录明确标记结果未知。

## 本机媒体库直连播放

飞牛影视与本项目同机部署时，可直接读取影视库文件让电视播放，不再依赖 ADB 抓界面：

```yaml
media:
  enabled: true
  roots: ["/vol1/1000/影视"]
  serve: true
  advertise_host: "192.0.2.10"
```

```bash
ai-tv media stats
ai-tv media resolve 野神 --season 1 --episode 1
ai-tv media serve          # 常驻媒体服务（支持 Range）
```

实测：指定剧集播放 81 秒 → **2.3 秒**，不存在的剧 0.1 秒明确失败。详见 [媒体库直连](docs/MEDIA_LIBRARY.md)。

## 电视语音操控

电视自带的语音链路（唤醒/ASR/NLP）会把识别文本写进 logcat，NAS 侧旁听即可复用小米云端 ASR，电视端零安装、无 root：

```yaml
voice:
  listener:
    enabled: true
```

```bash
ai-tv voice test      # 自检：确认能读到 logcat 并解析
ai-tv voice listen    # 常驻旁听，命中白名单的指令交给 Agent 执行
```

白名单优先：播放/暂停/音量/打开应用等由 Agent 接管；天气/时间/智能家居等**让行给小爱**，避免双重执行。只处理 `is_final` 结果并按 requestId 去重。详见 [电视语音操控](docs/VOICE_CONTROL.md)。

## 验证原则

`SUCCESS / PARTIAL / FAILED / UNKNOWN` 由证据决定。指定内容需播放器 UI 或归属明确的媒体元数据；详情页选中了标题、模型回答、搜索候选、单独音频均不能证明“指定视频已经正确播放”。必要时 `execution.verify_progress: true` 要求实际进度递增。

模型不可用时，不会把未知指令降级成“播放热门”。模型不可用、ADB 断开、页面异常或多次恢复失败均会停止并报告，不无限循环。

## 文档

- [架构与安全边界](docs/ARCHITECTURE.md)
- [部署、迁移、Docker 和回滚](docs/DEPLOY.md)
- [b8 当前交付状态](docs/V408_DELIVERY.md) 与 [最后一轮工程验收](docs/B8_FINAL_ENGINEERING.md)
- [b8 标准 CER 真人重评](docs/ASR_COMPARISON_B8.md)
- [V4.1 开源语音选型与阶段计划](docs/VOICE_V41_OPEN_SOURCE_PLAN.md)
- [V4.1 第一阶段：离线 ASR 实测与真实片名候选](docs/ASR_COMPARISON_V41.md)
- [V4.1 独立实验服务与按键录音](docs/ASR_LAB_SERVICE_V41.md)
- [V4.1 Phase 2：HTTPS 按键语音与片名/季集闭环](docs/V41_PHASE2_DELIVERY.md)
- [V4.1 Final3：取消、退出、确认复核与上传边界修复](docs/V41_FINAL3_DELIVERY.md)
- [V4.1 Final4：并发确认、幂等提交与媒体密钥测试稳定性](docs/V41_FINAL4_DELIVERY.md)
- [V4.1 Final5：会话生命周期与持久化幂等](docs/V41_FINAL5_DELIVERY.md)
- [V4.1 CatalogFix1：资源库总览与在线源提示](docs/V41_CATALOG_BROWSE_FIX.md)
- [V4.2 自然语言助理：只读媒体工具与工具调用循环](docs/V42_ASSISTANT.md)
- [V3 历史交付清单与实际验收](docs/V3_DELIVERY.md)
- [性能与加速](docs/PERFORMANCE.md)
- [优化目标与验收任务书](docs/OPTIMIZATION_GOALS.md)
- [优化进度与验收](docs/OPTIMIZATION_PROGRESS.md)
- [飞牛媒体库直连播放](docs/MEDIA_LIBRARY.md)
- [电视语音操控](docs/VOICE_CONTROL.md)
- [容器与部署补充说明](deploy/README.md)
- [变更记录](CHANGELOG.md)

## 验证

```bash
make test
# b8 最后一轮的实际测试数量与环境见 docs/B8_FINAL_ENGINEERING.md
# 独立 ASR 评测回归需在评测 venv 安装 poc/requirements-eval.txt
```

真机测试尚未覆盖任务书要求的全部 30 项各三次验收，不能用模拟测试声称已达到 90% 真机成功率。视觉、浏览器语音和 Docker 构建依赖实际环境，详见交付记录。

## 目录结构

```
ai-tv-butler/
├── bin/ai-tv                  # CLI 入口（make link 软链到 PATH）
├── adbtv/                     # 核心包（21 个模块 + web/index.html）
│   ├── config.py adb.py apps.py device.py    # 配置 / ADB / 应用 / 设备
│   ├── perception.py verifier.py             # 感知 / 证据核验
│   ├── intent.py feiniu.py tools.py          # 快速路径 / 飞牛导航 / 白名单工具
│   ├── agent.py recovery.py execution.py     # 编排 / 恢复 / 任务生命周期
│   ├── llm.py mcp_server.py server.py auth.py# 模型 / MCP / 认证控制台
│   ├── memory.py skill_engine.py scheduler.py# SQLite 记忆 / Skills / 调度
│   └── cli.py                                # 命令行
├── tests/                     # 模拟电视 + 9 套测试（154 项断言）
├── scripts/                   # demo / mcp / 调度 / systemd / release / migrate
├── docs/                      # ARCHITECTURE / DEPLOY / PERFORMANCE / V3_DELIVERY
├── state/ logs/               # 运行期（.gitkeep 占位，gitignore）
├── config.yaml config.example.yaml
├── Dockerfile docker-compose.yml
└── Makefile pyproject.toml requirements.txt .gitignore
```

## 升级与回滚

```bash
python3 scripts/release.py pre-v3
python3 scripts/migrate.py --config config.yaml
make test
# 回滚到新目录，不覆盖现有项目：
python3 scripts/release.py --restore <备份.tar.gz> --destination ~/ai-tv-butler-rollback
```

完整步骤见 [部署与回滚](docs/DEPLOY.md)。
