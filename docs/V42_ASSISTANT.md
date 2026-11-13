# V4.2：从固定路由器到自然语言助理（增量交付）

日期：2026-11-12。基于 V4.1 Final5 Prod 增量升级，未重写 Agent、ADB、媒体服务、语音识别或 Docker 架构；版本号仍为 `4.0.0b8`。

## 1. 结论

本轮把「理解需求 → 主动查询 → 调用工具 → 综合结果 → 自然回答」做成了真实循环，而不是只改提示词或加关键词：

- 新增 6 个只读媒体工具，全部读真实 `MediaIndex`，不虚构片名/数量/类型。
- 新增工具调用循环：模型只能从固定工具清单选择，参数经 JSON Schema 子集校验；一次请求可多次调用工具。
- 回答必须与工具观测对齐（片名与计数），否则回退到只读观测的确定性渲染。
- 模型不可用/超时/输出非法 JSON 时，确定性规划器仍能回答真实库问题。
- 播放只产生"提议"，必须走既有确认链路；不执行未确认动作。

## 2. 第一阶段：媒体库认知（真实数据）

生产库实测（容器内 `/media`）：

| 项 | 实测 |
|---|---|
| 作品数 | 35 |
| 电影 / 剧集 | 0 / 35 |
| 集数 | 8483 |
| 有 NFO 元数据的作品 | **0** |
| 索引根 | `/media`，可读 |

关键修复：

1. **类型识别不再依赖目录名含"电影"**。现在按 NFO `<movie>`/`<tvshow>` → 显式集号/多季结构 → 单文件无集号（视为电影）→ 否则 `unknown`。多个无集号视频的文件夹只计数为"结构不明确"，不分配虚构集号。
2. `media_index` 现在把"单个无集号视频"收录为第 1 集，使电影可被检索与播放（此前这类文件完全不可见）。
3. 缺类型/时长元数据时如实说明，不做标题猜类型。
4. 新增只读工具：`library_summary`、`library_browse`、`library_search`、`library_recommend`、`library_recent`、`library_health`。全部有界（≤20 条）、无文件路径外泄、只读。

## 3. 第二阶段：真实工具调用循环

`adbtv/assistant_loop.py`：

```
utterance + 有界上下文
  → 模型动作（tool / answer / clarify / propose_play）
  → 白名单 + JSON Schema 校验
  → 执行只读工具 → observation
  → 可继续调用（≤4 步，总预算 14s）
  → 事实对齐校验 → 自然回答
  → 需播放则 proposal → 既有确认链路
```

- 模型不能输出 ADB/shell/SQL/URL/路径：参数与文本都过 `_FORBIDDEN` 检查，工具结果只返回投影字段。
- 未引入 Pydantic AI 等大框架：现有 `LLMClient` 已支持 Anthropic/OpenAI 兼容协议，工具校验用标准库实现（约 200 行），与 Docker/依赖锁零冲突。
- 一次请求多工具：`library_summary` + `library_browse` 等已实测（`steps=3`）。

## 4. 第三阶段：自然语言理解

- 主动浏览："现在有哪些资源""资源库里边有什么"。
- 推荐："随便推荐一部""好看的"（基于真实入库，含排除项）。
- 条件继承："不要太长的" 继承上一轮类型/集数偏好。
- 候选引用："第二个是什么"（描述真实条目），"换一部类似的"（排除上一部）。
- 库信息问答与闲聊；对工具不可用/资源不存在/元数据缺失给出**具体**说明。
- 只有真正缺关键条件才追问。

## 5. 第四阶段：会话与记忆

- `ConversationManager` 增加结构化 `assistant` 状态（白名单字段）：`library_scope`、`last_query`、`last_tools`、`last_status`、`last_item`、`preferences`、`last_intent`。全部有界、可校验，未知字段丢弃。
- 短期上下文随会话 TTL 过期；会话按 `tv_id` 隔离，两个用户不共享候选。
- 长期偏好默认**不落盘**；仅当 `voice.assistant.remember_preferences: true` 时写入 `runtime/voice_preferences.json`（0600），只存类型/集数偏好，不含对话原文。

## 6. 第五阶段：模型接入与诊断

- 容器内实测模型可达（`cn:deepseek-v4.1-flash`，Anthropic 协议），单次 1.7–9s。
- 发现并修复一个真实缺陷：该模型会先输出推理文本再输出 JSON，原 `json.loads` 直接失败 → 现在用平衡括号提取**最后一个** JSON 对象。
- 响应 `metrics` 增加诊断：`model_name`、`decision_source`、`tool_name`、`tool_status`、`llm_latency_ms`、`tool_latency_ms`、`total_latency_ms`、`fallback_reason`。不记录密钥、对话原文或令牌。
- 模型不可用时媒体查询与基本控制仍走确定性路径。

## 7. 第六阶段：安全

- 只读工具自动执行；播放/切换沿用既有确认、幂等、取消、实例租约、ADB 设备锁与真实播放状态验证。
- 模型回答若引用未观测到的片名或编造计数 → 拒绝，改用观测渲染（`fallback_reason=ungrounded_answer`）。
- 模型提议播放未入库片名 → 拒绝（`proposed_unknown_title`）。

## 8. 验收结果（真实环境）

### 8.1 自动化

- **925 项单元测试通过，0 失败、0 跳过**（V4.1 的 878 + V4.2 新增 47）。
- 22 项模拟电视集成检查通过。
- 新增 47 项覆盖：类型识别（含无 NFO）、元数据缺失、搜索/推荐/最近/健康、Schema 校验（未知工具/参数/类型/范围/注入）、JSON 提取、多工具循环、事实对齐拒绝、播放提议、未知片名拒绝、超时回退、工具失败回退、15 条验收语句、空库、过期会话、双用户隔离、偏好持久化。

### 8.2 生产真实语句（`/api/voice`，真实模型 + 真实库）

| # | 语句 | 结果 | 工具 |
|---|---|---|---|
| 1 | 现在有哪些资源？ | SUCCESS，35 部（0 电影/35 剧集），列 8 部 | 只读库 |
| 2 | 资源库里边有什么？ | SUCCESS，同上 | 只读库 |
| 3 | 一共有多少部电影？ | SUCCESS，"没有电影，有 35 部剧集（8483 集）" | 只读库 |
| 4 | 最近新加了什么？ | SUCCESS，列最近 10 部 | library_recent |
| 5 | 随便推荐一部电影。 | SUCCESS，"没有电影，可从剧集推荐" | library_browse |
| 6 | 找一部悬疑的。 | SUCCESS，说明无类型标签、无法筛选 | library_recommend |
| 7 | 不要太长的。 | SUCCESS，列出 ≤18 集的 3 部 | library_recommend |
| 8 | 第二个是什么？ | SUCCESS，"第2个是《万妖图录传》…无类型元数据" | 会话上下文 |
| 9 | 换一部类似的。 | SUCCESS，排除上一批后给出 2 部 | library_recommend |
| 10 | 我上次看到了哪里？ | NEEDS_CLARIFICATION，如实说"还没有记录" | 会话上下文 |
| 11 | 播放这部的第一集。 | NEEDS_CONFIRMATION（经"第二部"选择后） | 确认链路 |
| 12 | 确认播放。 | 已提交并执行，**验证为 PARTIAL**（见 8.3） | Agent |
| 13 | 取消刚才的操作。 | NEEDS_CLARIFICATION，"没有执行任何电视动作" | 确认链路 |
| 14 | 电视现在正在放什么？ | SUCCESS，返回事实查询结果 | query_media |
| 15 | 资源库不可用时还能做什么？ | SUCCESS，说明能力边界，不编造 | 能力说明 |

### 8.3 未通过的验收（如实记录）

- **真实播放成功未确认**：确认后 Agent 打开了播放器，但电视处于待机/ADB `dumpsys` 超时，验证返回 `PARTIAL`（"播放器已打开，但未在预算内确认到声音"）。没有把 PARTIAL 记成成功。此前 V4.1 阶段同一片名曾实测 `SUCCESS`（audio_track_started=true），说明播放路径本身可用，本次失败属设备侧状态。
- **手机真人麦克风未验收**：没有可用手机，未把模拟麦克风或 WAV 回放当作手机语音。
- **在线资源站未接入**：`fnmedia-ext` 未连接，在线片库一律如实回答"无法核实"。

## 9. Docker 更新

```bash
cd "$HOME/ai-tv-butler"
stamp=$(date +%Y%m%dT%H%M%S)
mkdir -p "$HOME/ai-tv-butler-backups/pre-v42-$stamp"
tar -czf "$HOME/ai-tv-butler-backups/pre-v42-$stamp/source-private.tar.gz" adbtv config.yaml config.example.yaml
cp deploy/data/config.yaml "$HOME/ai-tv-butler-backups/pre-v42-$stamp/config.yaml.private"
docker image tag ai-tv-butler:4.0.0b8 "ai-tv-butler:pre-v42-$stamp"

# 启用助理（voice.assistant.enabled: true），再重建
docker compose -p ai-tv-butler build --pull   # 需网络；本机需代理
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
```

验证（不要只看版本号）：

```bash
docker compose -p ai-tv-butler exec -T console python3 -c \
 'from adbtv.assistant_loop import TOOL_SCHEMAS; print(sorted(TOOL_SCHEMAS))'
```

回滚：

```bash
docker image tag "ai-tv-butler:pre-v42-$stamp" ai-tv-butler:4.0.0b8
docker compose -p ai-tv-butler up -d --no-build
```

配置回滚：用备份的 `config.yaml.private` 覆盖 `deploy/data/config.yaml` 后重启 console。

## 10. 已知限制

- 本库 0 个 NFO：类型/地区/时长全部缺失，因此"悬疑/科幻"类筛选只能如实回答"无法判断"。要按题材推荐，需要为媒体库补充 NFO 或元数据。
- 模型单次 1.7–9s，多步循环可能到 10s 以上；预算 14s，超时即回退确定性答案。
- 控制台仍是单令牌模型；会话按 `tv_id` 隔离，不是多账号。
- 电视 ADB 在验收末尾变得不稳定（`dumpsys` 超时），会影响播放状态核验与 voice 容器健康检查；这是设备侧问题，代码未掩盖。

## 11. 证据

- [V4.2 验收证据](evidence/v42_assistant_acceptance.json)
- 新增测试：`tests/test_assistant_v42.py`
- 新增模块：`adbtv/library_tools.py`、`adbtv/assistant_loop.py`
- 修改：`adbtv/media_index.py`（单片电影收录、结构不明确计数）、`adbtv/conversation.py`（结构化助理状态）、`adbtv/voice_runtime.py`（助理回合、指代/候选说明、查询答复）、`adbtv/voice_intelligence.py`（`assistant/describe/recall` 意图、播放状态规则）、`adbtv/config.py`、`config.example.yaml`
