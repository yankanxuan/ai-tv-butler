# V4.0 Voice Beta：第一至第五阶段

版本：4.0.0b2。交付范围：智能路由、多轮会话、语音回执、任务打断、可插拔输入。没有重写 Agent、ADB、媒体、MCP 或 Docker 架构。

## 第一阶段变更

新增 `adbtv/voice_intelligence.py`。

1. 有限规则可确定的暂停/继续、绝对音量、打开应用、查询等走本地路径，不请求 LLM。
2. 明确的家居和信息域让行原生助手。媒体与家居混合的复杂请求进入语义判断。
3. 模糊需求、组合动作和上下文表达交给已配置的文本模型；模型关闭、忙碌、超时或输出不合规时追问。

模型输出必须恰好包含 `route/intent/target/parameters/confidence/reply/risk` 七个字段。公开 Schema 为 `docs/VOICE_V40_DECISION_SCHEMA.json`，代码还校验每个 intent 的参数白名单、范围、序列步数和必要参数。拒绝重复 JSON 键、额外字段、布尔置信度、NaN、未知动作和任意 shell 参数。

`route` 只能是 agent/native/clarify/ignore。模型只产生有限语义操作，不产生 shell。执行包装层将已校验、已绑定实际候选的操作转换为固定中文命令，再由现有解析器和 Agent 执行。未知应用和未证实的影视内容会追问。

模型置信度不构成授权：低置信度转追问，模型提出的有副作用动作须确认；“确认”必须由明确规则识别，不能由模型代替用户授权。

### 与小爱的冲突边界

logcat 是旁听，**无法拦截或抑制小爱**。

- Xiaomi 来源的播放、相对音量、返回、组合动作要求明确确认，避免直接重复执行。
- 打开应用、暂停、继续和设定绝对音量会短暂等待，然后读取真实状态，已经满足时不重复发送动作。
- 每轮记录 `conflict_risk` 和 `native_suppressed=false`。等待与观测只能降低风险，不代表独占控制，也不能消除用户/其他程序同时操作造成的所有竞态。
- 保留现有 prefix_only/allow/deny 操作者限制；启用智能模式不会跳过这些限制。设置前缀后，追问回答也需带同样前缀。

## 第二阶段变更

新增 `adbtv/conversation.py` 与 `adbtv/voice_runtime.py`。

每台电视有独立 session_id，保存当前应用、最近真实候选、已选内容、播放标题/季集、待回答问题、上一操作状态和有限意图摘要。默认空闲 180 秒清除整个会话，问题另有最多 60 秒的独立期限。读出的是副本，状态更新由锁保护。

会话在内存中，不写入 SQLite 或磁盘；重启后丢失。只保留必要结构化元数据，不保存完整原始语音。实际 Agent 的任务管理仍按原有机制保存必要的规范化命令/核验数据。

### 真实结果约束

- 使用既有 MediaIndex 搜索和解析文件；分类、地区取自实际 NFO 元数据，不从片名或模型推断。
- 缺少类型或地区时不当作符合筛选条件，也不声称媒体库里存在某部模型推荐作品。
- 只允许来自 library/observed_ui 的候选；模型不能传入 results 数组或插入候选。当前默认接入的是本机媒体库，其他应用的真实搜索结果尚无通用采集适配器。
- 序号必须指向当前候选；没有候选时“第二部”会追问。
- “不要这个/换一个”若所指不明会追问；即使指代明确，也不会自动播放下一部。
- 指定季集不存在时不替换为其他季集。

测试覆盖三组多轮：悬疑剧→国产→第二部→第一集；打开飞牛→三体→暂停→继续；科幻电影→不要这个→换一个。影片和类型须在真实资料中存在，不能把测试库中的内容当成 NAS 实际已有内容。Xiaomi 来源还会增加必要的确认轮。

## 非阻塞与隐私

ASR 接入线程只解析、去重和入队；不会调用模型或访问会话。LLM 与对话逻辑运行在既有有界工作队列中。

每个路由器最多一个模型请求在途，即使请求已经超时，也不会为后续请求无限创建线程。默认调用方最多等待 4 秒，忙碌和超时走安全回退。模型、设备和 Agent 在 listener 启动时初始化一次。

脱敏审计记录最终路由、固定 intent、风险、结果状态、哈希/长度和阶段耗时，不保存 target、参数、reply 或完整语音。`executed` 继续表示完成回调的计数；在智能模式下追问/让行也是一次回调，不能把这个计数直接当作成功的 ADB 任务数。

语音 Beta 默认不写 SQLite 任务历史，但仍可读取既有偏好和已审批 Skills；通过 `memory.record_history=false` 控制写入，而不关闭整个记忆模块。原 CLI/Web 功能默认维持历史写入行为。

## 启用配置

现有容器不自动切换 Beta。备份私人配置与数据后，在实际数据目录的 config.yaml 中合并：

```yaml
voice:
  intelligence:
    enabled: true
    llm_timeout_seconds: 4
    confidence_threshold: 0.8
    native_settle_seconds: 0.25
  conversation:
    ttl_seconds: 180
    max_turns: 8
    print_replies: false
    persist_agent_history: false
  listener:
    enabled: true
    reply_tts: false
```

`print_replies` 可显式开启生成的文字追问输出，便于终端/私有容器日志调试；它不是电视播报。默认关闭，避免将影视偏好写进日志。模型继续使用既有 llm 配置，不需要新增依赖。

## 文字会话测试入口

```bash
ai-tv voice dialogue --force
# 每行输入一轮；/exit 退出。
```

此入口可操作真实电视，使用文字输入并输出 JSON/追问。它不自动收音、不调用 TTS。无麦克风不能声称连续监听；xiaomi_logcat 仍需用户再次唤醒小爱或按语音键。

## NAS 更新与回滚

先按 V3.4.3 的备份方法保存私人源码、数据目录、现有镜像。**保留 media_key 与会话文件**，不要重新生成密钥。

将公开包解到源码目录，保留原 `.env`、私人配置和数据；Beta 镜像标签为 `ai-tv-butler:4.0.0b1`。媒体库仍对四服务只读挂载，数据和设备锁保持共享。

```bash
cd "$HOME/ai-tv-butler"
docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
docker compose -p ai-tv-butler logs --tail=50 voice
```

需要先配置 `voice.intelligence.enabled=true` 才进入新路由；默认 false 保留旧语音路径。不要并行启动第二个实际语音 listener，避免双重执行。

回退算法时将 intelligence.enabled 改为 false 并重启 voice；完整版本回滚时恢复备份源码/Compose，使用保留的 V3.4.3 镜像 `up -d --no-build`。本阶段没有 SQLite/媒体数据格式迁移。

## 第三阶段：语音回执（TTS）

新增 `adbtv/tts.py`、`adbtv/voice_response.py`。

**先做了电视端安全性验证，结论是否定的**：

| 探测 | 结果 |
|---|---|
| 通用 `VIEW` + `audio/wav` | 只解析到蓝牙 OPP，无播放器接手 |
| `tinyplay`（系统自带） | `cannot open device '/dev/snd/pcmC0D0p'`，无法打开 PCM |
| 用 MiVideoPlayer 播 WAV | 会占用视频画面并打断正在播的影片，不允许 |

因此**不实现电视端出声**，避免假设"ADB 启动 WAV 播放器是安全的"。落地方式为任务允许的降级：

- 语音在已认证控制台的 `/api/voice/audio/<name>` 以 WAV 下发，手机/浏览器播放；
- 电视侧保持文字提示（回复文本照常返回）。

Piper 复用现有 `~/.piper/models/zh_CN-huayan-medium.onnx`，无需新依赖。安全适配器：argv 列表调用（无 shell）、200 字上限、超时、哈希文件名（文本不进文件名）、原子写入、0600、缓存上限与 LRU 淘汰。

**实测性能**：首次合成约 **9.4 秒**（模型加载为主），同一文本缓存命中 0 ms。因此提供默认开启的 `tts.async`：`respond()` 1 ms 返回并后台合成，避免拖慢后续对话。

策略（`VoiceResponder.plan`）：`enabled=false` 不发声；视频播放中默认只播报失败，不播例行确认；确认类问题一律留文字；任务开始播报需显式 `ack_on_start`。`reply_tts` 旧开关保留兼容。

## 第四阶段：语音任务打断

新增 `adbtv/voice_interrupt.py`，接入现有任务控制文件（Agent 已有的 checkpoint 机制）。

- 打断在 **ASR 读取线程**直接处理，不进普通队列、不调模型；因此慢任务和 TTS 合成都不会拖延取消。
- 词表封闭且精确匹配（去标点后全等），避免"取消任务之后播放三体"这类句子被当成打断。
- 严格区分两类意图：
  - `取消任务/停止操作/别做了` → 取消 Agent 任务；
  - `停止播放/暂停/继续播放` → 控制视频本身；
  - 裸 `停下/停止` → **追问**"取消 Agent 还是停止视频"，不猜。
- 只控制本进程（pid）拥有的任务；无任务时如实返回 `NO_TASK`。
- `人工接管` = 取消任务并标记接管状态。
- 新增计数 `interrupts`、`interrupt_p95_ms`，心跳可见。

实测（真机）：任务运行中发"取消任务"，**打断延迟 54 ms**，任务以 CANCELLED 在 checkpoint 退出（总时长 12.03 s）；"停下"返回 NEEDS_CLARIFICATION；无任务时返回 NO_TASK。

## 第五阶段：可插拔语音输入

新增 `adbtv/voice_input.py`：

| 输入源 | 状态 |
|---|---|
| `xiaomi_logcat` | 已实现（现有 ADB logcat 旁听） |
| `web_microphone` | 已实现（控制台 `/api/voice` 接收浏览器识别文本） |
| `external_asr` | **预留，显式拒绝**（HTTP 501 / `UnsupportedInput`），不假装可用 |

所有通道共用同一 `VoiceDialogue` 与会话策略。`SharedSessionStore` 用 `flock`+原子写在共享 `/data` 下按电视保存**会话快照**（仍不含原始语音），使控制台与 voice 容器可以共享同一轮上下文；`shared_dir` 留空则退回单进程内存。

**不宣称连续收音**：NAS 无麦克风；小米路径每轮仍需唤醒词或遥控器语音键，状态接口 `continuous_listening=false` 明确标注。

## 第六阶段：性能

- 简单控制与明确让行不调用模型；只有模糊/上下文请求才调用。
- 每个路由器最多一个在途模型请求（含超时后），避免线程堆积；`model_busy` 立即安全回退。
- ASR 读取线程只解析/去重/入队；模型、TTS、设备操作都在工作线程或独立线程。
- 逐轮记录 `asr_receive_ms / queue_ms / observe_ms / route_ms / llm_ms / adb_ms / tts_ms / interrupt_ms`。
- Agent、设备、模型在进程启动时构建一次并复用；`/api/voice` 复用控制台长驻上下文。

## 明确未完成的后续阶段

- 真人唤醒+ASR 经新版容器进入 Beta 路由的端到端验证（容器未重建，仍未做）。
- 电视端出声：已实测不可用，未实现；若日后可安全混音再评估。
- 修改"尚未执行的任务"目前只覆盖取消排队项与任务级 pause/cancel，没有步骤级重写 UI。
- 真实模型的组合指令准确率偏低（8 条样本中 1 条 Schema 不合规）。
- 新版镜像 4.0.0b2 构建与四容器健康检查（本会话无 Docker socket 权限）。

测试数据、真实模型样本、真实电视测试与限制详见 `VOICE_V40_PHASE12_TESTS.md`。
