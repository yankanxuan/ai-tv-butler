# V4.0.0b8 交付报告（真实运行 / 受限项分开）

日期：2026-11-12。基线：b7 公开包实测 **570 tests OK (skipped=2)**、22 项模拟集成通过。
本轮为增量修复，未重设计 Agent、ADB、媒体服务或 Web 架构。

## 1. 依赖可复现性

| 项 | 结果 |
|---|---|
| 运行时锁 | `requirements-runtime.lock`：6 包、**215 个 wheel 哈希**，`only-binary=:all:` + `--require-hashes` |
| 构建锁 | `requirements-build.lock`：setuptools 75.8.0 / wheel 0.45.1 |
| 测试锁 | `requirements-test.lock`：playwright 1.65.0 + greenlet 3.5.6，**仅测试** |
| 三处一致性 | pyproject 依赖、requirements.txt、Dockerfile 都指向同一锁（有测试断言） |
| 干净环境验证 | 新建 venv 按锁安装：`pip check` 无冲突；版本实测 PyYAML 6.0.3 / pydantic 2.14.3 / pydantic-core 2.50.4 / typing-extensions 4.16.0 |
| 未使用大依赖 | 干净运行时内确认 **未安装** sherpa-onnx、onnxruntime、piper-tts、torch、mcp、uiautomator2 |
| 多架构 | PyYAML 与 pydantic-core 均有 cp311 manylinux x86_64 与 aarch64 wheel（可离线哈希校验）；ARM 未实机运行 |

Alpine.js：3.14.9 与 `@alpinejs/csp` 3.14.9 本地 vendored，**MIT**；已用上游 `v3.14.9/LICENSE.md` 原文替换此前转述文本，sha256 `5f05c492…ac657d`；两个 JS 哈希记录在 `adbtv/web/vendor/SHA256SUMS`，并有测试逐项核对。

## 2. 记忆可靠性

b7 的 `asdict` 修复不完整：拒绝规划时 `plan=None`，后续 `plan.package` 仍会写 `memory_warning`；未知且未执行的指令还会写入伪造的失败路径。已改为统一规范化 `plan_view`。

实测（直接查询 SQLite，非 mock）：

| 场景 | 结果 |
|---|---|
| 离线两条成功任务 | tasks 2 条、paths attempts 2 / successes 2 / failures 0 |
| 模型形态 GoalSpec（确定性夹具） | 1 任务、1 路径，上下文正确 |
| 执行失败 | successes 0 / failures 1，best_path None |
| 拒绝 / 未知指令 | 1 条 UNKNOWN 任务，**无** path 记录，无 memory_warning |
| 数据库创建被拒 | 任务仍成功，仅 warning，零写入 |
| PRAGMA query_only 真实只读 | 同上，零写入 |
| 数据库损坏 | 原文件保留，构造抛 DatabaseError，不静默覆盖 |
| 备份恢复 | `integrity_check=ok`，恢复后持久化继续（2/2/0） |

合计 22 项记忆测试通过。**限制**：模型用例是显式边界夹具，验证"模型形态目标"的持久化，不代表生产已跑真实 LLM 规划。

## 3. Web 安全

- 内联脚本迁出为 `adbtv/web/console.js`（随包分发），`script-src` 收紧为 **`'self'`**，不再允许 `unsafe-inline` / `unsafe-eval`。
- 保留 Alpine **CSP 构建**；脚本以 `defer` 顺序注册 `alpine:init` 后再启动 Alpine。
- `/static/<name>` 只提供显式白名单文件；API 仍全部要求令牌。
- 修复真实缺陷：候选按钮 1-based 但 `pick()` 再加 1，点第一个会发"第二部"。
- 真实 Chromium 验证（模拟麦克风，**非真实收音**）：登录拒绝、语音走 `/api/voice`、候选→集数→确认→SUCCESS、带令牌取 WAV、匿名 401、取消不触发 Agent、普通任务仍走 `/api/runs`。
- 共 **9 项 Playwright 用例通过**；另有 CSP 探针证明内联脚本被拒、`new Function` 抛 EvalError。

## 4. 真人遥控器语音验收（真实麦克风）

用真实遥控器麦克风、由人工在电视前说话，捕获 18 条真实 ASR（第一轮）：

- 成功的 4 条：打开飞牛 ×3、查询当前播放内容 ×1（电视当时处于 PLAYING 的一条也被记录到）。
- 其余多为 `clarify`：真实口语与严格词表不匹配（见下）。

原始识别文本仅存于 `0700/0600` 私有目录，公开报告只含哈希、长度、路由、状态与电视实际状态。

**第一轮暴露的真实缺陷（已修）**：

1. 唤醒词前缀未剥离：`小爱同学播放第一个` 无法解析。
2. 重复动词：`播播放我的世界` 无法解析。
3. 口语序数：`先放第一个`、`我要看第一个` 不识别（只承认"第一个"）。
4. 无逗号组合句：`打开飞牛播放短剧` 被错误切成"飞/牛播放短剧"。
5. 口语确认词：`确认播放`、`可以`、`行`、`没问题` 未覆盖。
6. 类型请求：`播放一部短剧` 未映射为片库类型搜索。

修复后上述六类均通过单元回归（`RealSpeechVariantsTests`）。

**ASR 识别错误**：`找穿成眼神`（应为"野神"）——系统未猜测替代内容，按安全策略追问，符合"不许编造内容"。

### 第三轮真人复验（修复后在 b8 容器链路上实测）

| 真机识别结果 | 意图 | 判定 |
|---|---|---|
| `先放第一个` | select | ✅ 修复生效（上轮为 unknown） |
| `我要看第一个` | select | ✅ 修复生效 |
| `打开飞牛播放短剧` | sequence | ✅ 修复生效（上轮被错切成"飞/牛播放短剧"） |
| `确定确定播放` | confirm | ✅ 修复生效（重复词被折叠） |
| `查一下分流里边有什么好看的短剧然后播放` | search | ✅ 长句仍能收敛为片库检索 |
| `播一部短剧` | play | ⚠ 真实 ASR 漏掉"放"字 → 已补单字动词，回归通过 |
| `打开飞牛播放器`、`打开飞` | open | ⚠ ASR 识别偏差（多字/截断），按追问处理 |
| `播放TRY` | play | ⚠ ASR 严重误识别，按追问处理，不猜测内容 |

所有 `clarify` 状态均因这些句子在独立上下文里缺少候选项，属预期追问，不是失败。
本轮再次证明：**ASR 文本质量**是主要瓶颈，而非路由规则。

## 4.5 真人 ASR 标准 CER 重评（最后一轮工程修复）

2026-11-12 12:28:53 +08:00 对现有 12 条真人录音重新执行 sherpa-onnx 推理，
使用 JiWER 4.0.0 / RapidFuzz 3.14.3 的标准 Levenshtein CER 替换 SequenceMatcher。

| 条件 | S / D / I | 错误 / 参考字符 | CER | 指令召回 | RTF | 专名命中 |
|---|---:|---:|---:|---:|---:|---:|
| 近场 clean（7） | 3 / 0 / 0 | 3 / 46 | 6.5217% | 5/7 | 0.2405 | 1/2 |
| 远场（3） | 3 / 1 / 0 | 4 / 21 | 19.0476% | 1/3 | 0.3060 | 1/2 |
| 噪声（2） | 1 / 0 / 13 | 14 / 8 | 175.0000% | 1/2 | 0.4427 | 1/1 |
| 合计（12） | 7 / 1 / 13 | 21 / 75 | 28.0000% | 7/12 | 0.2967 | 3/5 |

本轮重算结果碰巧与旧百分比四舍五入后相同，不是沿用旧结果。
冷初始化 4.3115 s，第一条非空部分结果 302.8 ms，第 2–12 条平均推理 1.2769 s，
进程生命周期峰值 RSS 157.61 MiB。快速文件回放没有实时节拍，不能声称 302.8 ms 是麦克风端到端延迟。
四项准确率门槛均未达标，保持小爱生产默认。
详见 [完整重评及复现](ASR_COMPARISON_B8.md) 与 [脱敏原始计数](evidence/asr_reassessment_b8.json)。

## 5. sherpa-onnx 独立 PoC（未接入生产）

- 模型：`csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01`，revision `a5f60fe0…`；模型 26,342,340 B（sha256 `68c9c943…`）、tokens 13,366 B（sha256 `6fed8c6c…`），均下载后校验通过。
- 许可：ONNX 卡本身未声明 license；其指向的权重来源 `icefall-streaming-zipformer-small-ctc-zh-2025-04-01` README 明确 **Apache-2.0**。PoC 文档保留此派生关系与不确定性。
- 音频：**用户明确授权**的 10 条本地 Piper 合成短句（非真人、非既有录音、未上传）。

实测（x86_64，2 CPU 线程）：

| 指标 | 结果 |
|---|---|
| 音频总时长 | 14.5128125 s |
| 推理墙钟 | 5.037669586 s |
| RTF | **0.347** |
| CPU 时间 | 7.317242288 s |
| 进程峰值 RSS | 112,545,792 B（≈107.33 MiB，生命周期峰值非增量） |
| 冷启动（工厂） | 5.219963135 s / CPU 3.906167575 s |
| 加权 CER | **10/60 = 16.67%** |

专有名词错误明显（"穿成野神"被识别为近似音），因此**不替换小爱**，仅保留 `external_asr` 适配位。

## 6. Docker 部署、历史镜像回滚与本轮备份恢复核验

Docker 权限到位后，用脚本完成真实部署与回滚演练，全部为实际 Docker 输出。

**部署（2026-11-12 11:07 起）**

| 项 | 实测 |
|---|---|
| 私有备份 | `~/ai-tv-butler-backups/pre-b8-20261112T110705/`（镜像 169 MB + 数据 58 MB + 配置，0600/0700） |
| 构建 | `compose build` 成功，镜像 ID `sha256:0fcb7301…` |
| 四服务 | console / scheduler / media / voice 全部 `healthy` |
| 版本 | 四容器 `ai-tv 4.0.0b8` |
| 依赖 | PyYAML 6.0.3、pydantic 2.14.3、pydantic-core 2.50.4、typing-extensions 4.16.0 |
| 媒体密钥 | `media_key_sha256 6300f53d…` 部署前后一致 |
| 修复入镜 | 容器内 `VoiceRouter.normalize("小爱小爱播放三体") == "播放三体"` |

**回滚演练（实际执行）**

1. 用记录的实际镜像 ID 回滚到 `ai-tv-butler:pre-b8-20261112T110705-*`；
2. **回滚后重新读取**四容器 health 与版本，而非依赖命令退出码：四个 `healthy`、`ai-tv 4.0.0b8`；
3. 校验运行镜像 ID 与记录一致（`sha256:d2682f93…`）→ `rollback-verification.json` 写入 `ROLLBACK_VERIFIED`；
4. 当时恢复检查仅确认文件存在/密钥长度，不能作为数据库完整性验收。最后一轮修复已补实际配置解析、配置所指密钥与 SQLite 的严格验证，并对同一备份重新执行临时恢复（见下文）；
5. 恢复新 b8 镜像后再次核验：`healthy`、镜像 ID `sha256:0fcb7301…`、媒体密钥未变。

上述部署和镜像回滚为 11:07–11:12 的历史实机记录，旧镜像虽然是 pre-b8 tag，实际报告版本仍是 4.0.0b8，不能称为已回到 b7。
本轮只读复验四服务 running/healthy、版本及内置 healthcheck 均通过；实际镜像仍为 `sha256:0fcb7301cf2bd1d4484a8be9cc8f0586c81017d41af7891335e9d5d8ca5f0341`。
本轮工程修改是宿主源码与评测/运维脚本修改，未重建生产镜像，也未再次执行镜像回滚。

同一真实备份（`pre-b8-20261112T110705`，数据归档 58,604,747 B）使用修复后的 `rollback_drill_b8.py` 在临时目录重新恢复：
配置有效、实际媒体密钥 32 字节且与 manifest SHA-256 相符、实际 `memory.sqlite3` 只读 `PRAGMA integrity_check=ok`。
检查命令未传 `--apply`；其成功仅证明这份数据可恢复，不等于本轮切换了运行镜像。
归档提取拒绝路径穿越、符号/硬链接、设备/FIFO、重复成员等，缺关键文件或完整性不为唯一 `ok` 都阻止成功及后续 Docker 操作。
部署脚本原有 7 项测试继续通过；新增恢复回归覆盖配置、实际数据库路径、密钥、损坏归档及安全提取。

## 7. 未完成 / 限制（当前状态）

- 独立 ASR 未达到准确率门槛；12 条录音仅小样本，同一音频无法在小爱进行 A/B。本轮未测真实独立麦克风持续收音或端到端延迟。
- 第二轮真人语音窗口 `observed=0`，该轮仍为未观测；第三轮真实遥控器已观测 9 条，见第 4 节，不把两轮混算。
- 断线重连仅有单元测试，尚未在真实断网场景触发验收。
- ARM64 未实机运行；Windows/其他 NAS 型号未验收。
- Playwright 麦克风为模拟 SpeechRecognition，不能当真人收音；历史 b8 的 9 项 Chromium e2e 不代表本轮重新采集了语音。
- 本轮没有 V4.1 新功能或生产 ASR 切换；[开源选型与阶段计划](VOICE_V41_OPEN_SOURCE_PLAN.md) 先完成评审及许可/音源/硬件验证，再开发。
- 本次工作目录是已解包源码，无 Git 元数据，源码改动在当前目录交付。

## 8. 最后一轮回归与证据

最终测试数量、退出码及范围统一见 [工程修复验收记录](B8_FINAL_ENGINEERING.md)。
原 630 项基线与新增测试合并执行，不能把历史通过数直接当本轮结果。
