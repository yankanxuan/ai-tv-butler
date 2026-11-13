# 电视语音操控接入可行性（实测记录）

> 状态：**已落地实现并端到端验收**（2026-11-11）；V3.4.1 完成稳定性与路由修复。
> 实现：`adbtv/voice.py`（logcat 解析 + 白名单路由 + 去重）、`ai-tv voice listen|test`、`scripts/ai-tv-voice.service`。
> 实测：真人说「几点啦」「今天天气」→ 正确让行小爱；说「打开飞牛」→ Agent 接管并返回 SUCCESS。
> 探测时间：2026-11-10 17:18–17:39，设备：小米电视 MiTV-ASTP0（mulan，Android 9，1920×1080），连接方式：网络 ADB `192.0.2.20:5555`。
> 本文第 1–4 节为可行性探测记录（2026-11-10）；落地实现与验收见「落地状态」一节。

## 结论

电视侧语音操控**可以接入 Agent，且电视端零安装、零改动**。

小米电视自带的远场/近场语音链路已经完成了拾音、唤醒、云端 ASR 与 NLP：用户对电视说的话，会在数秒内以**结构化 JSON** 形式出现在 `logcat` 中，其中 `is_final: true` 的记录包含完整识别文本。NAS 侧只需一个常驻 `adb logcat` 旁听进程提取文本，再交给 `ai-tv` Agent 执行即可。

因此 README 早先「NAS 无法直接读取小米遥控器麦克风」的表述需要修正：NAS 确实**拿不到原始 PCM**（无 root），但**能拿到识别后的文本**，这已足够驱动 Agent。

## 实测证据

### 1. 真人语音 → 识别文本

用户在电视前实际说了两句话，均为电视自身语音链路识别，NAS 侧从 logcat 实时读取：

| 时间 | 触发方式 | 最终识别文本 | 中间结果演进 |
|---|---|---|---|
| 17:38:39 | 遥控器语音键（`STARTSPEAKING` / `RecordStartEvent`） | 今天天气怎么样 | 今天天 → 今天天气怎么 → 今天天气怎么样 |
| 17:39:02 | 免按键唤醒「小爱同学」（`mWakeupWord='小爱同学'`，ONBOARD_MIC） | 打开飞牛 | — |

关键日志（已脱敏，仅保留 ASR 相关行）：

```
I =vc=en=aifw==xaf=pia=: FarFieldEngineprocessAsrResult, instruction =
{"header":{"name":"RecognizeResult","namespace":"SpeechRecognizer",...},
 "payload":{"is_final":true,"is_vad_begin":true,
  "results":[{"asr_binary_offset":2280,"begin_offset":1300,"confidence":0.0,
   "is_key_word_free_request":false,"is_nlp_request":false,
   "origin_text":"今天天气怎么样","text":"今天天气怎么样"}]}}

I =vc=en=en=proxy=: FAR_FIELD_callback | onEngineEvent AsrResultEvent(
  isFinal=false, text=今天天气怎么样, requestId='54bd9451...', engineType='FAR_FIELD')

I =vc=en=af=qc=: Thread-25 | onAsrResult, rid: 54bd9451..., result: 今天天气怎么样
```

要点：

- **流式可用**：中间结果逐字刷新，可用于做「说完即刻响应」或提前路由。
- **延迟**：说话结束到 `is_final:true` 约 **0.5 秒**（`asr.finish.talking.offset` 后 0.5s 内落盘）。
- **字段稳定**：`origin_text` 为原始识别文本，`text` 为归一化文本，可选 `text` 做指令匹配。
- **两种唤醒源**：`wakeupOrigin=KEY`（遥控器语音键）与 `wakeupOrigin=VOICE`（唤醒词），可由 `mWakeupWord`、`mWakeupOriginType`、`audioSource` 区分。

### 2. NLP 与播报同样可见

识别之后，小爱的语义结果与执行指令也在同一通道：

```
I =vc=en=aifw==xaf=pia=: FarFieldEngineprocessAsrResult, instruction = {"header":{"name":"RecognizeResult",...
I =vc=en=aifw==xaf=aew=: recv inst tra: nopt-...-Nlp.StartStream[54bd9451...]
I =vc=en=af=qc=: Thread-25 | onReceivePassThroughInst Nlp.FinishPreStream, isStream false
I =vc=cp=nlp=: FAR_FIELD_callback | receive nlp instruction Nlp.FinishPreStream, multiIntention false
I =vc=OperationCapabilityImp=: single_operation_executor | onOperationCompleted:
  operation=...FinishPreStreamOperation@..., state=STATE_SUCCESS
I =vc=en=en=proxy=: onEngineEvent NlpEvent(rid='...', ..., System.TruncationNotification / System.Exception / Dialog.Finish)
```

`Nlp.*`、`Dialog.Finish`、`Execution.*` 等指令名可用来判断小爱是否已自行处理该请求，从而决定 Agent 是否应当介入或让行。

### 3. 服务端可远程触发语音条（辅助手段）

无需真人发声也能驱动语音 UI，用于自动化验证与提示音流程：

| Action | 实测效果 |
|---|---|
| `com.xiaomi.mitv.voice.KEYDOWN` | 弹出语音条，`mCurrentFocus=com.xiaomi.voicecontrol`，UI 出现「唤醒或按遥控器语音键继续说」，并成功 `onRecordStart` |
| `com.xiaomi.mitv.voice.KEYUP` | 结束拾音进入识别 |
| `com.xiaomi.mitv.voice.ONBOARD_MIC_START` | 切换板载麦克风拾音路径（`onRecordingStart ... recording false ONBOARD_MIC`） |
| `com.xiaomi.mitv.voice.SHORTPRESS` | 关闭语音条/主题（实测从播放器退回桌面） |

`am start-foreground-service -a <action>` 即可触发，均无需额外权限。这 22 个 action 全部由导出服务 `com.xiaomi.voicecontrol/.VoiceControlService` 提供，`permission=null`。

### 4. 电视端能力盘点

| 项 | 实测值 |
|---|---|
| 语音应用 | `com.xiaomi.voicecontrol` 5.12.0.7（系统应用，非 debuggable） |
| 远场引擎 | `com.xiaomi.wakeupservice`（DUBHE 唤醒引擎，KWS 常驻，RTF≈0.29） |
| 无障碍服务 | `com.xiaomi.voicecontrol/.VoiceAccessibilityService` 已启用 |
| 麦克风 | 音频策略含 `AUDIO_DEVICE_IN_BUILTIN_MIC` / `BACK_MIC`，`enable_usr_voice=1`、`mico_support_prewakeup=1` |
| 账号 | `com.xiaomi` / `com.mitv` 均已登录（ASR 走云端，依赖登录态） |
| 网络 | 电视可 ping 通 NAS（`192.0.2.10`，延迟 2–4 ms） |
| Root | 无（`uid=2000(shell)`，`adbd cannot run as root in production builds`） |
| 蓝牙遥控 | 当前 `STATE_DISCONNECTED`（协议栈 ON，未连接配对设备）；语音键仍可用，说明走的是蓝牙 HID 之外的路径或盒载麦克风 |

## 方案对比

| 方案 | 机制 | 可行性 | 代价 | 结论 |
|---|---|---|---|---|
| **A. logcat 旁听** | 常驻 `adb logcat` 过滤 `processAsrResult`，提取 `is_final` 文本 | ✅ 已实测通过 | 极低：单进程 + 正则 | **推荐** |
| B. `AgentDebugService` | `GET_AGENT_ACTIONS` / `EXECUTE_AGENT_ACTION` 走小米 Agent 协议 | ❌ 生产版拒绝：`Only debug version support agent debug!` | — | 排除 |
| C. `aigency` AIDL 三方接入 | 实现 `com.xiaomi.voicecontrol.aigency.ClientInterface` 与 `IAgencyListener`，由小爱反向调用 | ⚠️ 接口存在（`XiaoAiAgency`、`AgencyProcessor`、`ThirdPartyLifeControlService`） | 需自写 APK、系统签名/白名单、随版本失效 | 备选，成本高 |
| D. 本地 ASR 从原始音频 | `AudioRecord` 取 PCM 自行识别 | ❌ 无 root，且会与系统拾音抢占 | — | 排除 |
| E. 浏览器 Web Speech | V3 现有 `voice.enabled`，手机/电脑浏览器麦克风 | ⚠️ 可用但非电视麦克风 | 需 HTTPS/localhost 授权，待真实浏览器验收 | 保留（非电视语音） |

方案 A 的最大优势是**复用小米云端 ASR**：无需本地 STT 模型，中文准确率与电视原生体验一致，且零协议逆向成本。

## 推荐架构（方案 A）

```
你对电视说话（遥控器语音键 / "小爱同学" 唤醒）
        │
        ▼  电视本地：拾音 → KWS → 云端 ASR / NLP（小米既有链路）
        │
        ▼  logcat: SpeechRecognizer.RecognizeResult (is_final=true)
   NAS 常驻 voice_listener
        │  ① 提取 origin_text
        │  ② 关键词/白名单路由
        ├── 命中 Agent 域 → ai-tv cmd "<文本>"（LLM 规划 → ADB 操控 → 证据核验）
        ├── 命中播报域 → 本地 TTS 合成 → 电视播放（piper zh_CN-huayan）
        └── 未命中 → 让行，交由小爱原生处理（不重复执行）
```

### 路由原则

- **白名单优先**：只接管明确属于 Agent 能力的指令（播放/暂停/找剧/切换应用/音量等），避免与小爱抢执行。
- **幂等与去重**：同一次会话的中间结果会多次刷新，只处理 `is_final=true`，并按 `requestId` 去重。
- **超时静默**：`STATE_RESULT_TYPE=fail`、`CONVERSATION_TIMEOUT`、`no audio bytes` 等异常帧直接丢弃，不触发 Agent。
- **失败可见**：Agent 未成功时按 V3 证据原则返回 `PARTIAL/FAILED`，不退化成「播放热门」。

### 播报回执

本机已具备中文 TTS：`~/.piper/models/zh_CN-huayan-medium.onnx` + `piper` 二进制（位于 `OpenMontage/.venv`）。可合成后经 `adb push` + 播放器或 `media` 通道在电视播出，作为 Agent 执行结果的语音反馈。注意当前 `ai-tv-butler` 未声明 TTS 依赖，落地时需明确是复用外部 venv 还是新增依赖。

## 风险与边界

| 风险 | 说明 | 应对 |
|---|---|---|
| logcat 环形缓冲有限 | 事后翻查会丢帧，必须实时读取 | 常驻 `adb logcat` 流式管道，不落全量盘 |
| 识别文本含用户隐私 | 语音原文会进入日志与记忆库 | 脱敏落库，默认不持久化原文，仅存指令意图 |
| 双重执行 | 小爱与 Agent 同时响应同一句话 | 白名单路由 + 接管提示音 |
| 依赖小米云与登录态 | 掉登录/断网则无 ASR | 监听 `state_result_type=fail`，降级为文字输入 |
| 版本漂移 | `VoiceControlService` action 集与日志格式随 OTA 变化 | 正则容错 + 启动自检（抓一次自测语句校验格式） |
| 无 root 限制 | 无法注入音频、无法改系统配置 | 接受边界，只用文本通道 |
| ADB 断连 | 电视休眠/网络抖动 | 监听重连，指数退避；不重试风暴 |

## 落地状态

| 项 | 状态 |
|---|---|
| 1. `adbtv/voice.py` 解析器（`is_final` 提取、requestId 去重、异常帧过滤） | ✅ 已实现，14 项单元测试 |
| 2. 白名单路由（allow / deny / prefix_only） | ✅ 已实现，deny 优先于 allow |
| 3. `config.yaml` 的 `voice.listener` 段 | ✅ 已实现（默认关闭，需显式开启） |
| 4. systemd 常驻单元与失败自愈 | ✅ `scripts/ai-tv-voice.service`；进程内指数退避重连 |
| 5. 端到端真人语音验收 | ✅ 已验收（见下）；TTS 回执仍为预留项 |
| 6. README / 本文档状态更新 | ✅ 已更新 |

### 实测记录（2026-11-11 12:58）

同一段真人语音连续三句，监听器实时判定：

```
[voice] listener_connected
[route] 几点啦   -> denied:native-assistant-domain (handled=False)
[route] 今天天气 -> denied:native-assistant-domain (handled=False)
[route] 打开飞牛 -> allowlist (handled=True)
[语音] 打开飞牛
[结果] SUCCESS 全部步骤已完成
```

即：属于小爱域的问题被正确放行，属于电视管家的指令被接管并执行成功。

### 使用

```yaml
voice:
  listener:
    enabled: true
```

```bash
ai-tv voice test      # 自检：读取 logcat 并统计可解析的最终识别
ai-tv voice listen    # 常驻旁听（Ctrl+C 退出）
```

systemd：

```bash
cp scripts/ai-tv-voice.service ~/.config/systemd/user/
systemctl --user enable --now ai-tv-voice
```

## V3.4.1 稳定性与路由修复

### 1. 不再重放历史语音

问题：`adb logcat` 默认会先输出整个环形缓冲（实测 **46907 行**），重启监听器时会重放旧 ASR 结果并执行历史指令。

修复：

| 措施 | 说明 |
|---|---|
| `logcat -v epoch` | 每行带设备侧时间戳 |
| 过期过滤 | 早于 `stale_seconds`（默认 10s）或早于本次启动的结果一律丢弃 |
| 持久化去重 | `requestId`（无则文本哈希）写入 `runtime/voice_state.json`，重启后同一条不再执行 |
| 不存原文 | 去重文件只存哈希与时间；终端/日志默认输出 `len=` 与 `hash=` 前缀 |

### 2. 有界队列，长任务不阻塞读取

`on_utterance` 现在跑在固定工作线程上，读取线程只负责解析与入队：

- 队列容量 `queue_size`（默认 4），满时**丢弃最旧**（语音场景里最新的指令更有价值）；
- 丢弃数、过期数、执行数通过 `stats()` 与心跳文件暴露；
- 队列内容在出队时会再次做过期检查，避免排队太久执行陈旧命令。

### 3. 心跳与健康检查

监听器每 5 秒写 `runtime/voice_worker.json`（pid、updated、stats），`ai-tv voice listen` 停止时写 `stopped`。
`scripts/healthcheck.py voice` 与容器 healthcheck 依据**新鲜度**判断，不依赖跨容器 PID 可见性。

### 4. 路由改为意图组合

原先纯子串匹配导致两类误判，现已修复：

| 输入 | 旧行为 | 现行为 |
|---|---|---|
| 关闭客厅的灯 | 被 Agent 接管（含"关闭"） | 让行小爱（智能家居域） |
| 打开B站播放新闻 | 被拒绝（含"新闻"） | Agent 接管（应用+动作） |
| 播放穿成野神第一部第一集 | 正确 | 正确（强媒体动词，无需"视频"字眼） |
| 打开设置 | 可能被接管 | 让行（打开类需应用或介质） |
| 放在桌上的手机 | 误判为播放 | 让行（排除"放在/放到"） |

判定顺序：智能家居设备 → 点名媒体应用 → 强媒体动词 → 动作+介质 → 播放控制 → 信息/闲聊 → 默认让行。
`classify()` 返回 `app/action/obj/domain/handle/reason`，便于诊断为什么接管或让行。

### 5. 关于"拦截小爱"的能力边界

**我们无法阻止小爱同时执行同一句话。** 监听 (logcat) → 路由 (白名单) → 执行 (Agent) 三步都不改变电视助手的行为。

因此 V3.4.1 的做法是：

- 只接管明确的媒体类指令，降低重叠面；
- 对 `打开/关闭/暂停/继续/音量/播放` 这类**可能双重执行**的指令标记 `conflict_risk`；
- 每次执行后由 `Verifier` 依据实际状态核验（`SUCCESS/PARTIAL/FAILED/UNKNOWN`），冲突时至少能如实报告；
- 不宣称"独占接管"，文档与 CLI 提示都写明这一点。

真正的独占需要电视侧配合（关闭小爱自执行或接入官方 Agent 协议），当前生产版不可用。

### V3.4.2 修复（队列与重放边界）

- `_enqueue()` 丢弃最旧任务后现在会 `task_done()`，不再让 `unfinished_tasks` 只增不减；
  `_drain()` 的过期分支同样结束计数，并有"所有分支都归零"的测试。
- `run_forever()` 统一使用 `_logcat_args()`：此前实际执行路径**没有** `-T`，与设计不一致。
- 时间戳异常兜底：明显未来时间（> 启动时间 + 300s）不当作新鲜；缺失时间戳不误判为过期。
- 新增队列满载、处理器异常、断连恢复、时间戳异常测试。

### 仍待完善

- **TTS 回执**：`voice.listener.reply_tts` 目前为预留开关，未接入 piper 合成与电视播放。
- **origin 字段**：唤醒来源（遥控器键 / 唤醒词）标记不在 ASR 结果行内，当前多为空；不影响路由。
- **断网/掉登录降级**：解析器会丢弃失败帧不触发 Agent，但未做「降级为文字输入」的主动提示。

## 附：本次探测使用的最小验证命令

```bash
# 实时旁听识别文本
adb -s 192.0.2.20:5555 logcat -v threadtime | grep -a "processAsrResult"

# 触发语音条并开始拾音（辅助验证）
adb -s 192.0.2.20:5555 shell am start-foreground-service -a com.xiaomi.mitv.voice.KEYDOWN
adb -s 192.0.2.20:5555 shell am start-foreground-service -a com.xiaomi.mitv.voice.KEYUP

# 本地 TTS 试音
echo '小爱同学，打开飞牛TV' | piper -m zh_CN-huayan-medium.onnx -f test.wav
```