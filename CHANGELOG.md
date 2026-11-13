# 变更记录

本文件记录各版本实际完成并验证过的内容。未验证项会明确标注。

## 4.3.1（2026-11-13）AI 管家事实可靠性修复

基于 4.3.0 的增量修复，只动事实校验与播放进度识别，不重构 ADB / 媒体服务器 / SQLite / MCP / 语音 / 播放链路。

**P0 事实字段错配**

- 根因：`AssistantLoop.grounded()` 把一次运行里所有字段的数字合并成一个集合，只验证"这个数字出现过"，于是 `total=2, movies=1, series=1, episodes=4` 时，"本地共有 2 部电影，都能播放"被判 SUCCESS。
- 现在：每个观测先转成**结构化事实台账**（类型化：movies / series / total / episodes / seasons / 真实集号 / 真实季号 / 片名），声称的数量按其对白里的字段绑定校验：`N 部电影` 只能对上 movies，`N 部剧集` 只能对 series，`N 部作品` 只能对 total，`N 集` 只能对 episodes。不得再用总数冒充电影数、用集数冒充作品数。
- 列表 `total` 只会作为"找到 N 部"的依据，绝不当作媒体库总量；翻页条数不再能冒充总量。
- 无法证明的模型回答一律回退到**原始工具数据的确定性渲染**（不是再叠加正则）。

**P0 中文自然语言绕过**

- 无任何成功工具观测时，回答只要出现阿拉伯/中文数字、书名号片名，或"推荐/看/播放 + 未观测片名"的无书名号片名，即判不可信并回退。
- 新增中文数字解析（十二/两/二十三…），区分"带单位"与"不带单位"的数量表述。
- 事实由工具提供、模型只组织语言；`answer_source=local_catalog` 现在只统计**成功的工具观测**，失败的工具调用（trace status=error）不再能为 `local_catalog` 背书。

**P1 播放进度工具事实识别**

- `catalog_get_playback_progress` 增加显式结果 Schema（`source ∈ device_status/recent_task/none`、`available`、`title/season/episode/state/updated_at/evidence`）。
- 区分设备实时观测、最近任务记录、无记录三种来源；该工具返回的真实进度（title/season/episode）现在能被 `grounded()` 正确识别，不再误判 `ungrounded_answer`。
- 仅记录到"第 N 集"时不臆造精确播放时间：没有真实 `updated_at` 时，回答里出现 `HH:MM` / `点X分` 之类时间点会被判不可信。
- 无记录时如实回答"还没有记录"，有记录时正确回答，均为 `SUCCESS`，不再降级为 `NEEDS_CLARIFICATION`。

**追加修复：列表候选后的单词序号回答（网页端复现）**

- 现象：`找一部短剧` → 列出候选并问"请选择第几部"，用户答 `3` 却得到"大模型响应超时"、`NEEDS_CLARIFICATION`。
- 根因（两层）：① 每回合 `_observe_context()` 调 `update_observation()` 会清空 `waiting_question`；② 网页端每回合通过 `SharedSessionStore` 执行 `hydrate()`，而 `hydrate()` 无条件 `waiting_question=None`，共享快照里的挂起问题被丢弃。`_context_decision()` 又只在文本是"第3部/第3个"时识别序号，裸 `3`/`第三` 失去上下文后交给模型，模型 4s 超时即回退通用澄清。
- 现在：`handle()` 在设备观测**之前**捕获 `waiting_question`、回填路由并传给 `_context_decision()`；`ConversationManager.hydrate()` 在严格校验（kind 白名单 + 整数 details）后恢复 `waiting_question`，使跨进程共享快照保留挂起问题。挂起问题为 `selection`/`episode` 时，裸阿拉伯/中文数字（可带 部/个/项/集）直接解析为"选第 N 部"或"第 N 集"，不再依赖模型；越界序号仍提示重选。

**版本**

- `adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` 统一为 `4.3.1`；`test_image_version_matches_code_version` 断言同步为 `4.3.1`。

**测试**

- 新增 `tests/test_assistant_v431.py`（20 项对抗测试）与 `tests/test_pending_selection_v431.py`（8 项，含共享存储往返），见 `docs/V431_DELIVERY.md`。
- 全套单元测试 **983 通过 / 0 失败**（原 955 + 新增 28）；22 项模拟电视集成通过。
- 真实飞牛 NAS 媒体库 + 真实 LLM 复验，脱敏记录见 `docs/evidence/V431_REAL_LLM_VERIFICATION.json`。

## 4.3.0（2026-11-12）自然语言助理可靠性

**P0 事实校验（修复编造资源数据）**

- 根因：`AssistantLoop.grounded()` 存在 `value <= max(listed, 20)` 逃生口，模型**零工具调用**时任何 ≤20 的数字都能通过，`"本地有12部电影"` 被判为 SUCCESS。
- 现在：无成功工具观测 ⇒ 回答不得包含任何片名或数量；有观测 ⇒ 每个《片名》必须精确匹配观测条目，每个"N 部/集/个"必须是观测值或本次返回列表的序号。删除逃生口。
- 新增可审计 `answer_source`：`local_catalog` / `device_status` / `unverified`，写入响应与 `metrics`。
- 工具异常/索引未启用/无权限/空库分别给出具体说明；`catalog_status` 用 `ok/empty/unavailable` 区分"确实没有"与"查询失败"。
- 推荐与播放提议只接受真实入库条目；未入库片名一律拒绝。

**P0 自然语言**

- 资源类问句（有哪些资源/资源库里有什么/有哪些电视剧）直接查库，不再要求用户给片名。
- "播放第二个"选作品、"播放第二集"选集，二者不混淆；"我说的是第二部，不是第二集"按纠正处理。
- "继续播放刚才那部"可用当前选择或真实播放历史；"我上次看到了哪里"无记录时如实说明。
- "现在播放的是什么"是事实查询，不再被误判为播放控制。

**P1 超时与预算**

- `LLMClient.chat(timeout=...)` 新增按调用传参；HTTP 超时取 `min(配置, 剩余预算)`，90s 配置不再越过 14s 预算。
- 错误分类：`LLMTimeout` / `LLMHTTPError(status, retryable)` / `LLMConnectionError`。
- 只读模型调用在 429/5xx 且预算允许时重试一次；有副作用操作不自动重试。
- 诊断新增 `budget_seconds`、`failed_stage`、真实 `total_latency_ms`。

**P1 统一资源工具**

- 新增 `catalog_status/summary/list/search/recommend/get_episodes/get_playback_progress`；旧 `library_*` 为同一实现的别名（不平行造系统）。
- `catalog_get_playback_progress` 来源为 `device_status`（设备观测 + 会话历史），不由模型编造。

**版本**

- 代码、`pyproject.toml`、`docker-compose.yml` 统一为 `4.3.0`；镜像 tag 与代码版本精确一致。

**测试**

- 955 项单元测试通过、0 跳过（新增 30 项）；22 项模拟电视集成通过；发布包审计通过。
- 真实电视播放验证：**未完成**（电视离线，`adb connect` 返回 No route to host），标记 NOT_TESTED。

## V4.2（2026-11-12）自然语言助理：只读媒体工具 + 工具调用循环

- 新增 `adbtv/library_tools.py`：`library_summary/browse/search/recommend/recent/health` 六个只读工具，全部读真实 MediaIndex，有界且不泄露路径。
- 修复类型识别依赖目录名"电影"的问题：改按 NFO → 集号/多季结构 → 单文件判定；多个无集号视频只计数不虚构集号。
- `media_index` 收录"单个无集号视频"为第 1 集，使电影可检索、可播放（此前不可见）。
- 新增 `adbtv/assistant_loop.py`：模型动作白名单 + JSON Schema 子集校验 + 多步工具调用（≤4 步/14s）+ 事实对齐（片名/计数）+ 确定性回退；播放只产生 proposal，走既有确认链路。
- 修复真实模型"先推理后 JSON"导致解析失败的问题（平衡括号提取最后一个对象）。
- `ConversationManager` 增加结构化 `assistant` 状态（白名单、有界）；长期偏好默认不落盘。
- `voice_intelligence` 新增 `assistant/describe/recall` 意图与播放状态事实查询规则；`voice_runtime` 接入助理回合、指代播放、候选说明、查询事实答复、回复控制字符清理。
- 响应 `metrics` 增加 model_name/decision_source/tool_name/tool_status/llm_latency_ms/tool_latency_ms/total_latency_ms/fallback_reason 诊断。
- 回归：925 项单元测试通过、0 跳过（新增 47 项）；22 项模拟电视集成通过；生产 15 条自然语言实测（12 条完全通过，播放验证因电视待机/ADB 超时返回 PARTIAL，未记成功）。
- 手机真人麦克风与在线资源站仍未验收。

## V4.1 Final5 CatalogFix1（2026-11-12）

- 新增只读 `browse_library` 意图与本地馆藏 `overview`：真实媒体索引统计、最多 8 个带 ID 候选、无文件不报告为空。
- 在线资源请求明确 UNSUPPORTED，不编造 fnmedia-ext/在线片库；`query_media/pause/open` 等原路由保留。
- 模型降级回复区分未启用、忙碌、超时、API 错误和 schema 错误。
- 878 项单元测试通过、0 跳过；22 项模拟集成通过；生产四服务健康。
- 已部署生产镜像 `sha256:6e2a11d5…`；旧镜像 tag `pre-catalogfix-20261112T204103`；媒体密钥和历史保留。
- 仍缺少 Android Chrome/iPhone Safari 真人麦克风和 fnmedia-ext 在线接口。

## V4.1 Final5（实验，未改生产版本号，2026-11-12）

- 登录会话改为 30 分钟空闲 + 8 小时绝对上限；task_id 不再延长登录；任务追踪与授权会话分离，过期/注销不取消电视任务。
- 新增重新登录后的受控历史任务查询 `/api/history`，只返回同一控制台令牌哈希主体记录。
- 控制台新增 SQLite 持久化幂等：`(owner=token hash,key)` 唯一、命令哈希冲突保护、pending/committed 恢复、7 天保留、受认证查询 `/api/idempotency`；旧客户端无键行为不变。
- 任务创建与幂等记录使用 SQLite `BEGIN IMMEDIATE` 事务，失败回滚并清理半创建任务；服务重启可重放原 task_id。
- Cookie 改为每个受认证响应按服务端剩余 TTL 重发，初始/活跃 1800 秒、绝对 8 小时封顶。
- 控制台启动恢复：仅重放 QUEUED 原 task_id；RUNNING/PAUSED 标 INTERRUPTED/UNKNOWN；终态/缺失任务不重放；recovery owner 防重复认领。
- 启动恢复改用实例 flock 租约与单一 recovery leader，不依赖 PID；覆盖 Docker PID 1 复用、双实例并发、存活租约异常退出接管。
- 旧 recovery_owner_pid 无实例 ID 记录保守迁移为 INTERRUPTED/UNKNOWN，保留 task_id/审计，不重放；新任务只认实例租约。
- 回归：869 项单元测试通过、0 跳过；22 项模拟电视集成通过；100 轮双线程确认压力测试通过。
- 媒体密钥 16×10 子进程测试共享 45 秒总截止并 finally 回收；Final4/Final5 多次约 21–23 秒通过，未观察到死锁。
- 已实际备份生产 `/data`、构建 Final5 镜像并部署四容器；重启幂等、媒体密钥/任务历史、QUEUED/RUNNING 故障注入均通过。手机真人麦克风仍未验收。

## V4.1 Final4（实验，未改生产版本号，2026-11-12）

- `TitleSessionStore` 增加统一 RLock，确认/取消/清理/会话读写在同一个锁内；同一候选并发确认只成功一次。
- `Portal.confirm` 增加每会话锁、提交状态机与 `X-Idempotency-Key`；两个并发确认只创建一个控制台任务，重复确认返回原 task_id 并查询状态。
- 超时/连接中断/5xx 记为 `submit_unknown`，保留幂等键且不自动重试；识别/新确认在未知状态被阻止。已提交任务运行期间禁止开始新识别。
- 取消、退出、过期清理与确认共用会话锁；logout 返回 active_task_id，确认中的任务不会被静默丢失。
- 媒体密钥 16 进程×10 轮测试改为共享 45 秒截止时间与 finally kill/reap；连续 3 次 21–23 秒通过，未观察到文件锁死锁。
- 回归：836 项单元测试通过、0 跳过；22 项模拟电视集成通过；100 轮双线程确认压力测试通过。
- Android Chrome / iPhone Safari 真人麦克风仍未验收，未标记通过。

## V4.1 Final3（实验，未改生产版本号，2026-11-12）

- 取消改为可核对状态机：失败/超时/未确认保留 task_id，只有控制台最终 CANCELLED 才报告 `cancelled`；前端支持安全重试，不无条件清理。
- 新增 `POST /api/logout`：服务端删除内存会话、清 HttpOnly Cookie；旧 Cookie/CSRF 失效，前端退出实际调用。
- 片名确认前复核媒体路径、存在性、大小、mtime 和候选快照；删除/变更/重新匹配时拒绝并清空选择，要求重选。
- `TitleSessionStore` 增加守护清理线程；服务关闭时停止。
- 网关与 ASR 服务超限上传先有界排空再 413，捕获 BrokenPipe/Reset；客户端早退不杀死服务。
- 回归：828 项单元测试通过、0 跳过；22 项模拟电视集成通过；真实控制台取消、真实 logout、真实 HTTPS 2,000,001 B→413 已验证。
- 手机 Android Chrome / iPhone Safari 真人麦克风仍未验收，未标记通过。

## V4.1 Phase 2（实验，未改生产版本号，2026-11-12）

- 新增独立 HTTPS 按键语音网关 `poc/voice_portal.py` 与手机页面：标准 `getUserMedia`/`MediaRecorder`/Web Audio 转 16 kHz PCM16 WAV，控制台令牌登录（内存会话）、HttpOnly/Secure Cookie、CSRF、独立 ASR 令牌、大小/时长/速率/并发限制、超时与重试。默认关闭。
- 新增 `poc/title_session.py`：作品级候选分组、季/集二级选择、审核别名、当前播放上下文、10 分钟候选过期、一次性确认；保留原始 ASR hypothesis，不自动执行，不自动写别名。
- 修复真实媒体库多季合并错误：使用 `season_hint`，同一作品为 11 季 / 1305 集；近似 `穿成一神…` 命中真实作品 90.9091。
- `poc/asr_lab_server.py` 新增认证 `/transcribe-upload`；实验镜像更新为 `ai-tv-asr-lab:v4.1-phase2`，默认关闭。
- `poc/asr_extended_eval.py`：增加意图、片名 Top-1/Top-3、误建议、拒识、P50/P95。现有 12 条仍为单说话人；多说话人数据未获得，报告明确不冒充。
- 实测：100/100 连续识别、P50 1.084 s/P95 2.186 s；容器峰值内存 139,436,032 B；8 并发 1 成功 + 7 个 409；视频同播 100 次后仍播放；ASR 停止时网关 504、控制台 `/api/voice/status` 200，重启恢复。
- 实机：文件回放（非手机）经网关确认后在小米电视播放《穿成野神我靠香火斩诸神》S01E001，任务 SUCCEEDED、播放器 PLAYING；取消延迟任务后 CANCELLED，未打开飞牛。真实手机麦克风未验收。
- 回归：815 项单元测试通过、0 跳过；22 项模拟电视集成通过；公开包审计通过。

## V4.1 第一阶段 PoC（实验，未改生产版本号，2026-11-12）

- 新增 `poc/asr_lab.py`：固定 `models-v41.lock.json` 的三类模型（CTC / 流式 Transducer / FunASR Paraformer 的 sherpa-onnx 导出）与 Silero VAD v5.1.2；显式 `prepare` 下载、SHA-256/许可证据门禁、60 s WAV 上限、私有报告。
- 新增 `poc/asr_compare_v41.py`：单模型独立进程顺序配对，严格拒绝缺授权、坏报告、样本变化、重复 profile；不把失败报告成完成。
- 实际同批 12 条授权真人录音重评：CTC 28.0% CER/RTF 0.265；Transducer 58.7%/1.275；Paraformer 28.0%/1.658；Silero VAD 另测但无稳定收益。小爱保持默认。
- 新增 `poc/title_candidates.py`：复用 MediaIndex + RapidFuzz，仅真实存在剧名候选，最多 5 项，始终确认，不改写原识别，不自动播放。
- 新增独立默认关闭 Docker 实验服务 `poc/asr_lab_server.py`、`docker-compose.asr-lab.yml`、`deploy/asr-lab/`；实际构建运行通过，授权真人录音 HTTP 识别与真实剧名候选通过，未授权/穿越拒绝。
- 新增 `poc/push_to_talk.py`：arecord/ffmpeg 按键录制后离线识别；已有 WAV 导入实测。真实麦克风未验收：本机无采集设备。
- 新增 `poc/requirements-v41.lock`、`poc/models-v41.lock.json`、许可证据与独立报告；生产 `adbtv`、主镜像、四容器、runtime lock 未修改。
- Docker 实测修复：`max-file: 1` 不可用于 local 日志驱动（改 2）；只读工作区源文件需在镜像内 `chmod a+rX /app`；`internal: true` 网络无法发布回环端口（改普通 bridge，撤回无外网声明）。

## 4.0.0b8 最后一轮工程修复（2026-11-12）

- `poc/asr_compare.py` 使用 JiWER 4.0.0 / RapidFuzz 标准 Levenshtein CER；统一归一化、保存完整编辑及整数 S/D/I、按字符加权聚合。新增 12 项评测回归，包含重复字符反例和短串穷举。
- 对原 12 条授权真人录音重新推理与计分：近场 3/46、远场 4/21、噪声 14/8、总计 21/75；专名 3/5。完整新测量见 `docs/ASR_COMPARISON_B8.md`，小爱继续生产默认。
- 恢复演练严格验证备份实际配置、实际媒体密钥及实际 SQLite，只读 integrity_check；缺失、损坏、无关文件替代、外部路径及不安全归档均阻断。新增 27 项恢复回归。真实原备份重新临时恢复通过，未写回生产卷。
- 统一 Docker 当前已部署事实、历史镜像回滚与本轮检查边界；本轮源码未重建生产镜像。最终回归记录见 `docs/B8_FINAL_ENGINEERING.md`。
- 完成开源优先的 V4.1 独立语音技术选型和分阶段计划：`docs/VOICE_V41_OPEN_SOURCE_PLAN.md`；本轮尚未开发 V4.1 新功能。

## 4.0.0b8（2026-11-12）开源优先与部署验收

**依赖可复现**

- 新增 `requirements-runtime.lock`（6 包 / 215 wheel 哈希）、`requirements-build.lock`、`requirements-test.lock`（Playwright 仅测试）。
- Dockerfile 改为 `--only-binary=:all: --require-hashes`，项目本体 `--no-deps --no-build-isolation` 安装，`pip check` 收尾；
  `INSTALL_PIPER` 默认 0，标准镜像不含 TTS/ASR 大依赖。
- pyproject 依赖、requires-python（>=3.10）、package-data（`web/*.js`）与锁一致，并有测试断言三处一致。
- Alpine.js 3.14.9 / CSP 构建本地 vendored，MIT 原文许可证已按上游 v3.14.9 恢复，哈希记录于 `web/vendor/SHA256SUMS`。

**记忆可靠性**

- 修复 b7 遗漏：`plan_view` 统一用于 package/context 持久化；拒绝与"未知未执行"指令不再产生 `memory_warning`
  或伪造失败路径。22 项记忆测试直接查 SQLite 验证任务/路径/成功失败计数、只读、损坏与备份恢复。

**Web 安全**

- 内联脚本外置为 `adbtv/web/console.js`，`script-src 'self'`（去掉 unsafe-inline/eval），保留 Alpine CSP 构建。
- 修复候选索引 off-by-one（点第一个发送"第二部"）；新增 9 项真实 Chromium e2e 与 CSP 探针。

**真人语音暴露并修复 6 类口语缺陷**

- 唤醒词前缀（小爱同学）、重复动词（播播放）、口语序数（先放第一个/我要看第一个）、
  无逗号组合句（打开飞牛播放短剧）、口语确认（确认播放/可以/行/没问题）、类型请求（播放一部短剧）。

**断线重连**

- 语音监听循环在重启 logcat 前先 `AdbClient.ensure()`（复用同一客户端），失败只上报 `adb_reconnect_failed`，
  成功计数 `adb_reconnects` 并写入 stats。

**PoC 与部署**

- 独立 `poc/`：sherpa-onnx 紧凑中文 CTC 模型（权重派生 Apache-2.0，ONNX 卡无 license 字段已如实记录），
  用户授权的 10 条本地合成样本实测 RTF 0.347 / CER 16.67% / 峰值 RSS ≈107 MiB，未接入生产。
- `scripts/deploy_b8.py`：镜像/数据备份 + 构建 + 四服务健康校验 + 失败自动回滚；后续已于 11:07–11:12 完成真实部署和镜像回滚，最后一轮再读取四容器健康/版本；详情见 `docs/V408_DELIVERY.md`。

## 4.0.0b7（2026-11-12）记忆路径哈希修复

容器 b6 实测语音闭环时，任务记录里出现 `memory_warning: asdict() should be called on dataclass instances`：
离线规划的 `plan` 是 dict，而记忆路径哈希无条件调用了 `asdict(plan)`。

- 修复：`asdict` 仅在 `is_dataclass(plan)` 时调用，dict 路径直接序列化；`json.dumps` 加 `default=str` 兜底。
- 新增回归测试 `PlanKeyHashingTests`：dict 方案执行后不再产生 asdict 警告。
- 该警告此前不影响命令执行，但会污染任务记录与记忆表；现已消除。

验证：22 项模拟集成 + 570 项单元测试通过（2 项 pydantic 用例在无 pydantic 环境显式 skip）。

## 4.0.0b6（2026-11-12）容器链路实测修复

b5 已在飞牛 NAS 重建并跑通 `/api/voice`，但真实容器链路暴露两处缺陷：

- **按片名搜索未生效**：`找穿成野神` / `搜索一下三体` 落到模型追问。原因是 `_context_decision`
  只识别"类型/电影/电视剧"，纯片名查找走不到 search 规则。已补：
  - `找/搜/搜索/查 + <片名>` → `search(query=…)`；
  - 量词与"一下"被剥离（`找个电影` → `kind=movie`，不污染 query）；
  - 交替式按最长优先匹配（`搜索` 不会被 `搜` 抢先）。
- **`/api/voice/status` 缺 `voice_beta`**：容器已启用 Beta 时仍返回 `None`。已补显式布尔。
- **设备忙时语音接口不再排队等待**：`/api/voice` 现在视频/任务占用电视时立即返回 409 并提示重试，
  而不是让浏览器等到前一个无关任务结束（真实浏览器 e2e 复现：前一测试残留任务导致 15s 超时）。
- TTS 依赖治理：`piper-tts` 为 **GPL-3.0-or-later**，改为构建参数 `INSTALL_PIPER=1` 才装入镜像，
  默认不随镜像分发；模型放 `deploy/data/piper/`，缺失时任务明确 FAILED 而非假装播报。
- 部署配置首次启用 Beta（`voice.intelligence.enabled`、`conversation.shared_dir=/data/runtime/voice_sessions`、
  `interrupt` 跨容器取消），并保留 `config.yaml.bak-…` 备份。

验证：22 项模拟集成 + 563 项单元测试通过（2 项 pydantic 用例在无 pydantic 环境显式 skip）；
Playwright e2e 6/6（Chromium 1254）。容器当前仍运行 b5，需重建 b6 才能获得上述两处修复。

## 4.0.0b5（2026-11-11）开源优先集成

**Alpine.js 3.14.9（本地 vendored，MIT）**

- 语音对话区改为 Alpine 组件：多轮消息、真实候选项、选择/确认/取消、会话状态、TTS 任务状态、WAV 播放。
- 使用官方 **CSP 构建**，未放开 `unsafe-eval`；移除 `x-model`/带参表达式，改为 `Alpine.data` + 原生事件。
- 静态文件随仓库提供（`adbtv/web/vendor/`），无 CDN；`/vendor/<file>` 只提供白名单类型。
- 原有 `/api/runs` 普通任务、SSE、Skills、调度等全部保留。

**Pydantic 2.14.3（可选后端，MIT）**

- 新增 `adbtv/schemas.py`：VoiceDecision / PendingAction / SessionSnapshot / TTSJob / VoiceResponse。
- 已安装时用 Pydantic 模型，缺失时用**等价内置校验**；两套均通过同一批合法/非法用例。
- Schema 只校验形状，权限、白名单、确认与二次校验仍在原模块（并保留原测试）。

**Playwright 1.65.0（仅测试，Apache-2.0）**

- 新增 `tests/e2e/test_voice_ui.py` 6 项真实浏览器用例，全部通过。
- 真实浏览器发现并修复：CSP 拦截本地脚本、Alpine 需要 eval、`/api/config` 缺 Beta 开关、登录前初始化。
- 测试机需指定 `AI_TV_CHROMIUM`（Playwright 1.65 期望 Chromium 1254，本机为 1248）。

**修复与文档**

- CSP 改为 `script-src 'self' 'unsafe-inline'`、`media-src 'self' blob:`、`object-src 'none'`、`base-uri 'none'`。
- 新增 `THIRD_PARTY_NOTICES.md`、`docs/OPEN_SOURCE.md`、`scripts/research_oss_poc.py`、`requirements-test.txt`。
- 调研（未接入）：sherpa-onnx、MeloTTS、MCP SDK、uiautomator2；权重许可证与设备端安装需先确认。

**验证**

- 22 项模拟集成 + 560 项单元测试通过（2 项 pydantic 用例在无 pydantic 环境显式 skip；venv 中 7/7 全过）。
- Playwright e2e 6/6 通过（真实 Chromium）。
- 未做：真人麦克风、电视扬声器、4.0.0b5 镜像构建。

## 4.0.0b4（2026-11-11）语音交互闭环修复

**跨容器会话状态（P1）**

- `hydrate()` 完整替换所有共享字段；快照缺 `selected_content` 时**显式清空**旧选择（此前残留）。
- 快照持久化 `expires_at` 空闲到期时间，恢复时严格校验；过期/不可解析一律失效并清空，不复活旧上下文。
- 待确认操作保留独立 60 秒有效期；会话 ID 与一次性确认机制不变。
- 损坏快照（非字典、非法 id、含 shell 的 pending）安全丢弃，不抛异常。

**Web 控制台接入真正 VoiceDialogue（P1）**

- 新增独立「语音对话」区：网页麦克风识别后发 `/api/voice`（不再只是填普通任务框）。
- 显示自然语言回复、真实候选（可点击选择）、确认/取消、会话 ID 与状态、执行/验证 JSON；支持连续多轮。
- 普通任务输入保持 `/api/runs`，原功能未改。

**异步 TTS 交付（P2）**

- 每次请求唯一 `job_id`，状态机 QUEUED→SYNTHESIZING→READY/FAILED；`GET /api/voice/tts/<id>` 认证查询。
- READY 返回 `audio_url`；网页用带 `X-Auth-Token` 的 fetch 取 WAV 播放，不用未认证直链。
- 合成成功 ≠ 已播放：`played` 恒 0，`status()` 声明 `tv_audio=false`。
- Piper/模型缺失 → FAILED 并说明原因，线程继续；队列满 `tts_queue_full` 不谎报成功。
- 文档控制台端口统一为 8770（原示例误写 8771）。

**验证**

- 22 项模拟集成 + **552** 项单元测试通过（b3 为 524，新增 28 项）。
- 真实 HTTP（模拟电视）：voice→job→READY；音频无令牌 401、带令牌 200；状态接口 `tv_audio=false`。
- 未做：电视扬声器播报、真人麦克风容器端到端、4.0.0b4 镜像构建与四容器健康、真实浏览器自动化。

## 4.0.0b3（2026-11-11）智能语音可靠性补丁

**P0-1 跨容器多轮确认**

- 待确认操作从进程内存迁入共享会话快照（`pending_action`），voice/console 共享同一 `session_id`，跨容器可确认。
- 结构化意图仅保存规范化命令 + nonce + 候选 id + 创建/到期时间；拒绝 shell 字符，写入前校验。
- 一次性消费、60s 到期（不超过会话 TTL）、新请求作废旧提议。
- `SharedSessionStore.transaction()` 用 flock 覆盖整轮读改写，防并发覆盖。
- 确认前重新校验：应用仍配置、候选仍在结果、媒体库仍可解析、设备状态仍需要该操作。

**P0-2 TTS 后台线程**

- 修复 `audio_base_url` 为空时 `_loop` 的 KeyError 崩溃；缺 URL 记为不可交付，线程继续。
- 显式状态：synthesized / deliverable / delivered / played（played 恒 false）。
- 计数单点化，`spoken==delivered` 不再重复；队列满返回 `tts_queue_full`；溢出与失败分开计数；异常记 `last_error` 后续继续处理。
- 新增 `status()` 声明电视端音频不可用、交付通道与认证方式。

**P1-1 跨容器任务打断**

- 任务记录写入 `device`+`role`；打断按设备身份选择目标，可取消同电视上 console/scheduler 发起的任务。
- 设备不匹配一律不动；本机设备未知时仅限本进程。
- 角色白名单与 `prefix_only` 授权生效，未授权返回 UNAUTHORIZED。
- 慢速媒体 ADB 移入独立高优先级线程，默认不等待（PENDING），不阻塞 ASR。

**P1-2 复杂语义**

- 确定性处理：相似推荐（真实元数据）、换第二个、继续刚才那集、打开X+音量/静音/暂停/搜索组合、先找再播需先列真实候选。
- 家居+电视混合句改追问；观察缓存 1.5s 避免重复 dump。
- Schema 新增 swap / resume_episode 意图与 like 参数。

**验证**

- 22 项模拟集成 + **524** 项单元测试通过（V4.0.0b2 为 496）。
- 100 条额外真实模型样本：整体 95%（search 93.3%、play 85%、clarify 100%、sequence 100%），32 条真实模型调用全部 Schema 合法；LLM p50 2938ms。
- 真实小米电视（文字注入）：跨容器确认 SUCCESS/PLAYING；暂停 5.1s、继续 4.7s；取消 166.7ms→CANCELLED。
- 清理：`play` 常需确认后，旧对话测试同步补了确认轮。

**未完成（不得宣称已实现）**

- 真人麦克风 ASR 端到端；4.0.0b3 镜像构建与容器健康；电视端出声。
- 详见 docs/VOICE_V403_RELIABILITY.md 与 docs/VOICE_V403_TESTS.md。

## 4.0.0b2（2026-11-11）Voice Beta：第三至第五阶段

**第三阶段：语音回执**

- 新增 `adbtv/tts.py`（Piper 中文合成，argv 调用、200 字上限、超时、哈希文件名、0600、有界缓存）与 `adbtv/voice_response.py`（策略 + 交付）。
- 先验电视端安全性：WAV 意图只到蓝牙、`tinyplay` 打不开 PCM、MiVideoPlayer 会抢占画面 → **不实现电视出声**，改用控制台 `/api/voice/audio/<name>` 下发到手机/浏览器，电视保持文字。
- 首次合成实测约 9.4 s（模型加载），默认 `tts.async=true` 后台合成，`respond()` 1 ms 返回。
- 策略：默认关闭；视频播放中不播例行确认、失败仍播；确认类留文字；`ack_on_start` 可选。

**第四阶段：语音任务打断**

- 新增 `adbtv/voice_interrupt.py`，接入现有任务控制文件（Agent 既有 checkpoint）。
- 打断在 ASR 读取线程处理，不进队列、不调模型；封闭词表精确匹配。
- 严格区分取消 Agent 与停止视频；裸"停下"追问。仅控制本进程任务，无任务如实 NO_TASK。
- 真机实测：取消延迟 54 ms，任务以 CANCELLED 退出；TTS 合成中打断 0.8 ms。

**第五阶段：可插拔输入**

- 新增 `adbtv/voice_input.py`：`xiaomi_logcat`、`web_microphone` 已实现，`external_asr` 显式拒绝（HTTP 501）。
- `SharedSessionStore` 以 flock+原子写在共享 `/data` 保存会话快照，使控制台与 voice 容器共享上下文（仍不含原始语音）。
- 状态接口明确 `continuous_listening=false`：NAS 无麦克风，小米路径每轮仍需唤醒或语音键。

**第六阶段：性能**

- 简单命令与让行不调模型；单请求在途，超时后仍不会堆积线程。
- 逐轮记录 asr/queue/observe/route/llm/adb/tts/interrupt 耗时；Agent 与设备构建一次复用。
- 控制台 `/api/voice` 复用长驻上下文，不按语音重建。

**验证**

- 22 项模拟集成 + **496** 项单元测试通过。
- 161 条中文语料 161/161 路由契约一致（夹具，非模型准确率）。
- 真实模型 8 条小样本 7/8；真机播放/暂停/继续 5/6（1 次为设计确认拦截）；打断 54 ms。

**未完成（不得宣称已实现）**

- 电视端出声（实测不可用）；真人 ASR 端到端；4.0.0b2 镜像构建与容器健康；步骤级任务修改；大规模模型准确率。
- 详见 docs/VOICE_V40_PHASE12.md 与 docs/VOICE_V40_BETA_TESTS.md。

## 4.0.0b1（2026-11-11）Voice Beta：第一、二阶段

**第一阶段：三级语音路由（新增 adbtv/voice_intelligence.py）**

- 简单控制与明确家居/信息域走本地规则，不调用 LLM；模糊、组合与上下文表达进入模型。
- 模型输出严格校验七字段 Schema、有限 intent、参数白名单与范围，拒绝重复键、额外字段、布尔/NaN 置信度与任意 shell 参数；执行前转换为固定命令并重新解析。
- 模型超时 4 秒、单请求在途、失败安全追问；置信度低、副作用动作、模型代替用户确认均转追问/确认。
- 保留 prefix_only/allow/deny 操作者策略；明确记录 `native_suppressed=false`，等待+状态观测只降低小爱冲突风险，不宣称独占控制。

**第二阶段：多轮会话（新增 adbtv/conversation.py、adbtv/voice_runtime.py）**

- 每台电视独立会话，空闲 180 秒过期、问题 60 秒独立过期；只存结构化元数据，不存原始语音。
- 三组多轮流程（悬疑→国产→第二部→第一集；飞牛→三体→暂停→继续；科幻电影→不要这个→换一个）通过；候选只来自真实媒体库/NFO，模型不能注入结果，缺集不替换，不明指代只追问。
- 接入既有监听：ASR 线程只解析/入队，模型与对话在工作线程；新增逐轮 `asr/queue/observe/route/llm/adb` 耗时记录。
- 语音 Beta 下 `memory.record_history=false` 可停止写任务历史，同时保留偏好与已审批 Skills。

**验证**

- 22 项模拟集成 + 466 项单元测试通过；V3.4.3 为 418 项，无退化。
- 161 条中文语料：161/161 路由契约一致（确定性夹具，非模型准确率）。
- 真实模型 8 条小样本：7/8 Schema 合法且符合预期，p50 2808ms；组合指令进入安全追问，未执行。
- 真实小米电视（文字输入，非真人 ASR）：打开飞牛 + 播放→确认→暂停→继续，5/6 SUCCESS，独立观测状态一致。

**未包含（后续阶段，不得宣称已完成）**

- TTS（第三阶段）、语音高优先级打断（第四阶段）、浏览器/外部 ASR 共享会话（第五阶段）。
- 新版镜像未构建验证；真人 ASR 端到端与大规模模型准确率未测；组合指令准确率偏低。
- 详见 docs/VOICE_V40_PHASE12.md 与 docs/VOICE_V40_PHASE12_TESTS.md。

## 3.4.3（2026-11-11）最终稳定性修复

- 首次密钥初始化使用持久锁文件 + flock，唯一临时文件完整写入/fsync/原子替换，严格保持 32 字节二进制，移除 strip 和失败后临时随机密钥兜底。
- 会话锁、读取、保存失败明确报错，不签发无法持久化的成功链接。
- 分开 /livez 与 /readyz，兼容 /health 指向 readiness；目录/密钥不可用返回 503，响应不暴露绝对路径。支持 fnOS ACL 实际访问权限。
- 新增统一媒体 IPv4 解析，优先通向电视的 NAS 地址，绑定/Agent 探活/播放 URL 一致，失败明确要求 advertise_host，电视 URL 不回退到 loopback。
- public 排除私有 .env 变体，只保留 .env.example；检测通用凭据和用户主目录，归档实际内容二次审计后原子发布。
- 验证：22 项模拟集成 + 418 项单元测试通过；16 进程并发初始化连续 10 轮完全一致；HTTP 服务重启后原会话 206。用户重建 Docker 后，独立核验新版媒体 readiness=200/version=3.4.3、console 正常、scheduler/voice 新鲜心跳；通过容器 API 的真机播放/暂停/继续均 SUCCESS。Docker 内部 Health 字段因 socket 权限不可直接读取，证据边界见测试记录。
- 本轮不增加 AI 功能。完整结果和 NAS 更新步骤分别见 docs/V3_4_3_TEST_RESULTS.md 与 docs/V3_4_3_UPDATE.md。

## 3.4.2（2026-11-11）可靠性补丁

**P0 媒体鉴权绕过**

- 修复 `_authorized()` 在未配置 token 时 `return True` 的匿名放行：实测无令牌、无签名可 200 读取媒体文件。
- 改为「播放会话」授权：访问必须携带 `?sid=&sig=`（签名绑定单个文件）或显式配置并校验通过的长期令牌。未配置令牌不再等于开放。
- 空/通配绑定地址不再原样使用：`""`、`0.0.0.0`、`::`、`*` 一律解析为电视可达的内网 IP，失败才回落到 `127.0.0.1`。
- 新增 `/health`（公开、只读）供容器与 worker 探测。

**P0 多进程媒体端口争用**

- Agent 不再自行绑定 8899：`_direct_play` 改为通过共享签名密钥签发 URL，并先探测中央媒体服务；服务未运行时报 FAILED 并给出启动指引，不抢端口。
- 签名密钥与会话文件持久化到运行目录（`media_key` / `media_sessions.json`），跨进程共享并用 `flock` 保护，多个 worker 生成的 URL 都被同一服务接受。
- Compose 新增独立 `media` 服务（`media serve`）；console/scheduler/voice 不再执行媒体服务命令。
- 第二个实例绑定同一端口时给出明确提示（EADDRINUSE → 退出码 2）。
- 新增 12 路并发播放测试与"再绑定即失败"测试。

**P1 语音队列与历史重放**

- 修复 `_enqueue()` 丢弃最旧任务后未调用 `task_done()` 导致 `unfinished_tasks` 只增不减。
- 修复 `_drain()` 过期分支漏掉 `task_done()`；新增"所有分支都结束计数"测试。
- `run_forever()` 统一走 `_logcat_args()`（此前实际执行路径未带 `-T`，与设计不一致）。
- 时间戳异常兜底：未来时间（时钟漂移）不再被当作新鲜；缺失时间戳不误判为过期。
- 新增队列满载、过期、处理器异常、断连恢复、时间戳异常测试。

**P1 长期播放与拖动**

- 播放链接由"固定过期"改为"会话 + 滑动空闲窗口"（`session_idle` 默认 1800s）：持续请求会续期，长片中暂停后再拖动仍可 206 播放；空闲超时才失效。
- 首次访问必须发生在 `link_ttl`（默认 300s）内，避免长期有效的陈旧链接。
- 新增"10 分钟后仍可拖动"与 Range 206 测试。

**P1 配置与发布包保护**

- 新增 `scripts/package.py`：`public`（源码，绝不含 `config.yaml`、运行状态、密钥）与 `private`（个人备份）两种模式，并在打包前做敏感内容审计（内网地址 / API Key / 控制台令牌 / 媒体令牌）。
- 全仓库把真实局域网地址替换为文档保留网段（192.0.2.0/24），出口网卡探测改用公共地址。
- `.gitignore` 显式忽略 `config.yaml`、`config.local.yaml`、`deploy/data/`、`*.token`。
- 新增打包审计测试（含"故意植入密钥必须被拦下"）。

**其他**

- 电视休眠会导致播放器不响应：新增 `power_state()`/`wake()`，执行动作前自动唤醒；状态未知时不盲发电源键（避免把正在播放的电视关掉）。小米电视实测 `KEYCODE_WAKEUP` 无效、`KEYCODE_POWER` 有效。
- 版本号升至 3.4.2；Compose 镜像同步。

**验证**

- 自动化：22 项集成检查 + 368 项单元测试通过。
- 真机：媒体服务绑定 192.0.2.10:8899（非 0.0.0.0）、签名 URL 探测成功。
- 真机验收（2026-11-11 16:06–16:24，MiTV-ASTP0）：
  - 直连播放 → SUCCESS 3.1s，暂停 → SUCCESS 3.1s（PAUSED），继续 → SUCCESS 3.0s，再暂停 → SUCCESS（状态一致）。
  - 长片（合成 15 分钟）：播放 5 分钟后仍 `PLAYING`；同一签名链接再次 Range **206**（会话滑动续期生效）；暂停后 Range **206**、继续后 **206**。
  - 冷启动首次播放实测需要 20s+ 才出声，因此新增 `ui.direct_play_timeout: 45`（此前用 20s 会误报 PARTIAL）。
  - `暂停播放` 在无播放内容时如实返回 UNKNOWN（不假成功）。
- Docker 实际运行（2026-11-11 16:40–17:00，飞牛 NAS 上 `docker compose -p ai-tv-butler up -d`）：
  - 四个服务全部 healthy：console `/health` ok、media `/health` ok（roots=`/media`）、scheduler 心跳 `pid=1 idle`、voice 心跳 `listening` 且 ADB 已连接。
  - 通过控制台 API 端到端验证：播放 → SUCCESS（`action=direct_play`）、暂停 → SUCCESS、继续 → SUCCESS、再暂停 → SUCCESS；真机同步 `playing=True/PLAYING`。
  - **部署缺陷已修**：媒体库原先只挂给 `media` 容器，而建索引的是 `console` 容器 → 容器内索引 0 条、播放退化成 UI 搜索并失败。现改为**四个服务都只读挂载** `/media`，索引恢复 45 部剧，播放回到直连。新增回归测试防止复发。
  - 语音端到端（容器内监听）：真人说 3 句，心跳 `last_utterance_at` 变化 3 次、`executed` 由 0 增至 3、`dropped` 恒为 0；电视最终状态与所说指令一致（前台 `com.trim.tv`、音量 10）。新增脱敏事件日志 `runtime/voice_events.jsonl`（只存哈希/长度/路由结论，不含原文），便于不读容器日志也能审计路由。
  - 容器内实测语音队列 `expired: 1`（历史帧被过期过滤丢弃），证明重放防护在真实环境生效。
- 30 项 × 3 次真机统计（经控制台 API，容器内 Agent）：首次 90 次 = **51 SUCCESS / 21 FAILED / 9 UNSUPPORTED / 9 UNKNOWN**。
  失败逐项归因，其中 **30 次是代码缺陷**，已修复：
  - `TVDevice.set_volume` 在重构中被丢失 → 4 项"音量设到 N"全部 `AttributeError`（真机复验：8/3/15 均 SUCCESS）。
  - `Agent._query_summary` 丢失 `@staticmethod` → 所有查询类指令报"参数个数不对"（真机复验：查询 SUCCESS）。
  - 相对快进在播放器不提供进度时抛 `RuntimeError` → 改为 **UNSUPPORTED** 并说明原因（能力缺失 ≠ 执行失败）。
  - 新增 4 项回归测试锁住这三类问题。
  - **重建镜像后重跑 30×3（2026-11-11 17:23–17:28）**：90 次 = **SUCCESS 69（76.7%）/ UNSUPPORTED 12 / UNKNOWN 9 / FAILED 0**；
    独立状态核验 **90/90 一致**，平均 3.5s。按"可执行任务"计（排除播放器不支持与主动拒绝）成功率 **85.2%**。
    12 次 UNSUPPORTED 全部来自小米播放器缺少 transport 控制（快进/跳转/上下集）；9 次 UNKNOWN 为明确拒绝不存在的内容与未配置应用。

## 3.4.1（2026-11-11）稳定性修复版

**语音监听稳定性**

- 修复重启后重放 logcat 历史 ASR 结果并执行旧指令的问题：改用 `-v epoch`、按设备时间戳过滤过期结果、启动后过滤历史重放。
- 去重状态持久化到 `runtime/voice_state.json`（只存 requestId 或文本哈希，不存原文），重启后同一句不再执行。
- `on_utterance` 改到有界队列 + 固定工作线程执行，长任务不再阻塞 logcat 读取；队列满时丢弃最旧。
- 心跳文件 `runtime/voice_worker.json` + `healthcheck.py voice`，按新鲜度判断在线。
- 终端/日志默认只输出 `len=` 与 `hash=`，不写语音原文（`log_full_text` 可选开启）。
- 修复非法 UTF-8 日志行导致读取线程崩溃；修复断连时管道未关闭的句柄泄漏。
- 新增队列溢出、过期、重复、断线重连、心跳生命周期测试。

**语音路由**

- 路由由纯子串匹配改为「应用 + 动作 + 对象」组合判断：
  - 「关闭客厅的灯」「打开空调」等智能家居指令不再被接管；
  - 「打开B站播放新闻」「播放新闻」不再被误拒；
  - 「播放某部剧」这类无"视频"字眼的强媒体动词可直接接管；
  - 「打开设置」等仅打开动词、无应用/介质的请求仍让行；
  - 「放在桌上的手机」不再被当成播放。
- 新增口语归一化：去除尾部语气词（吧/啊/呢…），"看看/看一下/搜一下"归一为动词；补「小电视」别名。
- 明确能力边界：**无法阻止小爱同时执行**；对可能双重执行的指令标记 `conflict_risk`，执行后一律用状态核验，不宣称独占接管。

**媒体服务器鉴权**

- 修复 `url_for()` 不带凭据、开启 `serve_token` 后电视必然 401 的集成缺陷。
- 改为按文件、短时（默认 300s）HMAC 签名链接，兼容 Range（206）；换文件/篡改/过期均拒绝。
- 默认不再绑定 `0.0.0.0`，并新增 `private_only` 仅允许回环/私有网段。
- 新增无签名拒绝、有效令牌、过期令牌、Range 206、路径穿越、按文件绑定测试。

**部署**

- Compose 新增可选 `voice` 服务（`voice listen --force`），与 console/scheduler 共享 `/data`、ADB 授权与设备锁；镜像版本升至 3.4.1。
- voice 健康检查基于心跳新鲜度；说明 Docker 不会因 unhealthy 自动重启（需 autoheal 或人工重启）。
- `voice listen` 在未启用配置时以退出码 2 明确失败，避免容器"启动成功但空转"。

**配置与测试**

- 删除 `config.py` 中重复的 `feiniu_tv` 定义（后者曾静默覆盖带 `flow` 的版本，导致旧版兜底路径失效）。
- 新增进程内模拟器 `InProcessSimulator`，`adb.simulator: inprocess` 时不再为每条命令启动 Python 进程；保留 CLI 模拟器路径与一致性测试。
- 测试服务器改用短轮询，`shutdown()` 不再每次等 0.5s。
- 测试套件：334 项，耗时从 31s 降到约 17s（集成套件 19.6s → 1.0s）。

**验证**

- 自动化：22 项集成检查 + 334 项单元测试通过。
- 真机（MiTV-ASTP0）：签名链接播放 SUCCESS 3.8s、暂停/继续各约 2.3s、连续 6 条命令状态正确。
- 真实语音：安装新版监听器后即时捕获到真实语音并正确分类（一条让行、一条接管），据此修掉填充词问题。
- Docker 镜像构建、容器运行、systemd 启停仍未验证（daemon 权限受限）。

## 3.4.0（2026-11-11）

**电视语音操控**

- 新增 `adbtv/voice.py`：旁听小米电视 ASR 结果（`logcat` 中的 `processAsrResult`），提取 `is_final` 文本。
- 白名单路由：播放/暂停/音量/打开应用等由 Agent 接管；天气/时间/智能家居等让行小爱，避免双重执行。
- 按 `requestId` 去重；失败帧（超时、无音频、识别错误）丢弃不触发。
- `ai-tv voice listen|test`、`scripts/ai-tv-voice.service`，`config.voice.listener` 配置段。
- 真机验收：真人语音「几点啦」「今天天气」正确让行，「打开飞牛」被接管并 SUCCESS。

**文档整理**

- 统一测试数量表述、版本号、语音实现状态；README 补齐 V3.2–V3.4 能力与全部文档链接。

## 3.3.0（2026-11-11）

**飞牛TV 内播放**

- `activate()` 由「每次点击后反复读取界面」改为「页面状态确认，必要时才读」，界面读取 19.9 秒 → 6.5 秒。
- `find_title` 改为整屏滑动查找；已在飞牛TV 内播放短剧 24.6 秒 → 6.8 秒。
- 内容身份改由导航过程实际点击的标签提供（播放器渲染时无法取界面快照）。
- 修复严重回归：放宽简称匹配曾把「山海归途」匹配到「山海归途续集」，现加续作后缀保护。

## 3.2.0（2026-11-11）

**本机媒体库直连播放**

- `adbtv/media_index.py`：扫描 `/vol1/1000/影视`，45 部剧、8483 集，解析剧名/季/集到真实文件，支持别名与口语近似。
- `adbtv/media_server.py`：只读 HTTP 媒体服务，支持 Range（206/416）、路径逃逸防护、可选令牌。
- `device.launch_video()`：以 VIEW intent 交给电视播放器。
- 指定剧集播放 81.2 秒 → 2.3 秒；不存在的剧 0.1 秒明确失败，不猜替代内容。

**其他**

- 修复真实剧名含逗号（「我，史前霸王龙」）被误当复合指令。

## 3.0.0（2026-11-10）

**V3：控制台与部署**

- 认证控制台（`make_server`）、异步任务 + 有界队列 + SSE、截图、历史、Skills/调度管理、模型配置。
- 匿名首页可加载登录表单；API 强制认证；移除 URL 中的长期令牌。
- 无特权 Docker、Compose 拆分 console/scheduler、权限受限的部署初始化与缓存清理脚本。
- 部署测试 10 项、Compose 配置校验、无网络 wheel 构建与只读安装树资源检查通过。
- Docker 镜像实际构建与容器运行未验证（当时 daemon socket 权限不足）。

## 2.2.0（2026-11-10）

**记忆与 Skills**

- `adbtv/memory.py`：SQLite 长期记忆（任务、路径可靠性、恢复统计、明确偏好、Skills），WAL、原子迁移、备份、清理、递归脱敏。
- `adbtv/skill_engine.py`：声明式白名单 Skills，隔离模拟验证 + 人工审批，修改定义自动撤销审批。

## 2.1.0（2026-11-10）

**感知、执行管理与结果核验**

- `adbtv/perception.py`：统一 `ScreenState`，XML/窗口/媒体/音频，可选视觉回退，短 TTL 缓存，点击前重定位。
- `adbtv/verifier.py`：应用/内容/播放/进度分级核验，输出 SUCCESS/PARTIAL/FAILED/UNKNOWN。
- `adbtv/execution.py`：任务生命周期、跨进程设备互斥、暂停/继续/取消、超时与动作上限。
- `adbtv/recovery.py`：失败分类与有界恢复，人工接管。
- 安全加固：远端 shell 参数引用、工具参数校验、MCP 严格 JSON-RPC、截图路径限制。
- 调度：跨进程领取、租约、有限重试、cron 时区与日周语义修正。

## 已知未验证 / 待验收

- Docker 镜像实际构建与容器运行。
- systemd 单元实际安装启停。
- 真实浏览器（非脚本）交互与移动端。
- 语音 TTS 回执（`voice.listener.reply_tts` 为预留开关，未接入）。
- 飞牛TV 自定义搜索键盘的完整适配（指定剧集仍走列表查找或直连）。
- 任务书要求的 30 项任务 × 3 次真机成功率统计。