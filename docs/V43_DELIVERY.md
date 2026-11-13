# V4.3 交付报告：AI 管家可靠性升级

版本：**4.3.0**（代码 / pyproject / compose 一致）。基线：`ai-tv-butler-v4.2-assistant-20261112-public.tar.gz`。

## 1. 已确认的根因

| # | 现象 | 根因 | 证据 |
|---|---|---|---|
| 1 | 模型说"本地有12部电影"却返回 SUCCESS | `AssistantLoop.grounded()` 有 `if value <= max(listed, 20): continue`，零观测时 `listed=0` → 20，任何 ≤20 的数字都放行 | 新增用例 `test_invented_count_without_any_tool_call_is_rejected`（修复前失败） |
| 2 | 模型虚构片名不被拦 | 同上：无观测时 `titles` 为空，但 `《...》` 检查在无观测时也会因文本无观测而被"数量规则"放过 | 同上 |
| 3 | 14s 助理预算形同虚设 | `LLMClient` 只用自己的 `timeout=90`，`chat()` 不接受调用方预算 | `test_model_call_receives_remaining_budget_not_configured_timeout` |
| 4 | "继续播放刚才那部"在已选作品时仍追问 | 指代规则只认 `played_history`，不认 `selected_content` | 生产复验前后对比 |
| 5 | 资源类问句要求用户给片名 | 旧路由把"资源库"归到泛化澄清 | 生产复验 |

## 2. 修复文件与摘要

| 文件 | 修改 |
|---|---|
| `adbtv/assistant_loop.py` | 严格事实校验（删除 ≤20 逃生口）；`answer_source` 溯源；`catalog_*` 工具与 `library_*` 别名；按剩余预算调用模型；错误分类与只读重试；`failed_stage`/`budget_seconds` 诊断；`catalog_get_playback_progress` 从设备状态取 |
| `adbtv/library_tools.py` | 新增 `status()`（ok/empty/unavailable）、`episodes()`；`catalog_*` 统一命名（旧名为别名）；结构不明确文件夹只计数 |
| `adbtv/llm.py` | `chat(timeout=)`；超时取剩余预算；`LLMTimeout/LLMHTTPError(retryable)/LLMConnectionError` |
| `adbtv/voice_runtime.py` | 回合绝对截止；`answer_source` 写入响应与 metrics；选集/选集数纠正规则；指代回退到当前选择；播放选择逻辑重写 |
| `adbtv/voice_intelligence.py` | 播放状态事实查询规则（避免误判 native/控制） |
| `adbtv/conversation.py` | 结构化助理状态（白名单、有界） |
| `adbtv/config.py`、`config.example.yaml` | `voice.assistant.*` 配置 |
| `adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` | 版本 4.3.0 |
| `tests/test_assistant_v43.py` | 新增 30 项 |
| `tests/test_deployment.py` | 版本断言改为 4.3.0 + 镜像 tag 精确相等（更严格） |
| `docs/{TEST_REPORT,SECURITY_AUDIT,ROLLBACK}.md`、`docs/DEPLOY.md` | 新增/更新 |

## 3. 修改前后行为差异

| 场景 | v4.2 | v4.3 |
|---|---|---|
| 模型零工具调用说"12 部电影" | SUCCESS，原样返回 | 拦截 → 用真实索引回答（0 部电影 / 35 部剧集），`fallback_reason=ungrounded_answer` |
| 虚构片名 | 可能通过 | 拒绝，只保留观测条目 |
| 模型慢/超时 | 最长 90s 占用 | 受剩余预算约束，超时即回退确定性答案 |
| 429/500 | 直接失败 | 只读调用在预算内重试一次，再失败则降级 |
| 空库 vs 不可用 | 混为一谈 | `empty` / `unavailable` 分开说明 |
| 来源可审计 | 无 | `answer_source` ∈ {local_catalog, device_status, unverified} |
| 选集 vs 选集数 | 部分场景混淆 | 明确区分，支持"不是第二集"纠正 |

## 4. 新增与保留功能

新增：`catalog_status/summary/list/search/recommend/get_episodes/get_playback_progress`、`answer_source` 溯源、预算诊断、错误分类、选集纠正。

保留（未改动逻辑）：ADB 连接、应用开关、音量/按键、播放/暂停/继续、指定影片与集数、媒体 HTTP Range、进度记录、Web 控制台、MCP、语音监听、定时任务、SQLite、Compose、健康检查、媒体密钥、确认/幂等/实例租约/取消恢复。

## 5. 自动化测试

- **955 项单元测试通过，0 失败、0 跳过**（v4.2 的 925 + 新增 30）。
- **22 项模拟电视集成通过**。
- `docker compose config --quiet` 通过。
- 发布包审计通过（234 文件）。

详见 [TEST_REPORT.md](TEST_REPORT.md)。

## 6. 真实环境验证

| 项目 | 结果 |
|---|---|
| 生产四容器 | console/scheduler/media `healthy`；voice 因电视离线 `unhealthy`（健康检查如实报告 `adb_connected=false`） |
| 版本 | 四容器均 `ai-tv 4.3.0` |
| 媒体密钥 | 部署前后 `6300f53d…` 未变 |
| 真实 LLM + 真实库 | 通过：资源/计数/推荐均 `local_catalog`，回答与工具结果一致 |
| 10 条验收语句 | 9 条完全符合；"我上次看到了哪里"在无历史时如实说明（符合预期） |
| 真实播放验证 | **NOT_TESTED**：电视离线（`adb connect` → No route to host），未把 PARTIAL/未执行记成成功 |
| 手机真人麦克风 | **NOT_TESTED** |
| 在线片源 | **NOT_TESTED**（未接入，明确拒绝编造） |

## 7. 未解决问题与风险

1. 媒体库 0 个 NFO：类型/地区/时长缺失，题材筛选只能回答"无法判断"。
2. 本机模型 1.7–9s/次；多步循环接近 14s 预算，超时即降级。
3. 电视 ADB 不稳定（本次离线），影响播放核验与 voice 健康。
4. 控制台单令牌，非多租户。
5. `catalog_get_playback_progress` 目前读会话/设备状态，未持久化跨重启的播放位置（P2 项）。

## 8. 备份、部署与回滚位置

- 备份目录：`~/ai-tv-butler-backups/pre-v43-20261112T233443/`
  - `source-private.tar.gz`（sha256 `093942d9…`）
  - `config.yaml.private`、`media_key.sha256`、`SHA256SUMS`
- 回滚 tag：`ai-tv-butler:pre-v43-20261112T233443`（对应旧镜像 `sha256:a24b4c22…`）
- 当前镜像：`ai-tv-butler:4.3.0`（`sha256:95e96100…`，172,770,228 B），同时打回 `ai-tv-butler:4.0.0b8` 供现有 compose 引用
- 回滚步骤：[ROLLBACK.md](ROLLBACK.md)

## 9. 最重要的验收标准

| 用户说 | v4.3 实际行为 |
|---|---|
| "现在资源库里有什么？" | 直接查库（35 部/0 电影/8483 集）并列出真实条目，不要求用户提供片名 |
| "播放第二部的第一集。" | 按当前会话真实候选锁定资源 ID，生成确认；确认后才执行，执行后按设备信号判定 SUCCESS/PARTIAL/FAILED/UNKNOWN |
| 模型或工具失败 | 说明失败阶段与原因（`failed_stage`、`fallback_reason`），降级为确定性答案，绝不编造成功 |
