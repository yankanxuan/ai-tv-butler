# V4.0.0b3 智能语音可靠性补丁

基于 V4.0.0b2 的增量修复，不重写 Agent、ADB、媒体服务与多轮语音架构。

## P0-1 跨容器多轮确认

问题：待确认操作原本存在 `VoiceDialogue._extras`（进程内存），voice 容器发出"播放三体"后，console 容器收到的"确认"永远找不到待办，播放不会执行。

修复：

- 待确认操作写入**共享会话快照**（`pending_action`），随 `SharedSessionStore` 落到 `/data`，voice/console 均可读。
- 会话 ID 在跨进程 hydrate 时保持一致，两个入口看到同一 `session_id`。
- 结构化待执行意图只保存**规范化命令**（如 `播放《三体》第1季第一集`）、intent、候选 id、创建/到期时间；不保存 shell，写入前校验并拒绝 `adb/shell/;|&$` 等字符。
- **一次性**：确认时 `take_pending()` 先取出并清空；重复确认或过期（默认 60s，且不超过会话 TTL）不再执行。
- **新请求作废旧提议**：任何非确认/否认的新指令都会清除 pending。
- **并发安全**：`SharedSessionStore.transaction()` 用 `flock` 覆盖"读取→处理→写回"整轮，两个容器同时提交不会互相覆盖。
- **确认前重新校验**：重新解析命令、检查应用仍已配置、候选仍在当前结果中、媒体库仍能解析该片、设备状态仍需要该操作（已暂停就不重复暂停）。

## P0-2 TTS 后台线程

问题：`audio_base_url` 为空但合成成功时，`_loop()` 访问 `result["audio_url"]` 抛 `KeyError`，工作线程直接退出；同时 `spoken` 在 `deliver` 与 `_loop` 各加一次，重复计数。

修复：

- 交付状态显式区分：`synthesized`（WAV 生成成功）→ `deliverable`（能构造 URL）→ `delivered`（已登记可供下载）→ `played`（**恒为 false**，电视端音频实测不可用）。
- 缺 URL 时标记 `audio_base_url_missing`，工作线程继续运行。
- 计数单点化：`delivered` 唯一累加，`spoken == delivered`；`synthesized` 另计。
- 队列满返回 `speak=false, reason=tts_queue_full`，不再谎报成功；队列容量/溢出单独计数 `overflow`，与合成失败 `failures` 区分。
- `_loop()` 整体 try/except，异常记入 `last_error` 并继续处理后续请求。
- 新增 `status()`：明确 `tv_audio=false`（附实测原因）、交付通道、认证方式（控制台令牌）。

## P1-1 跨容器任务打断

- 任务记录新增 `device` 与 `role`；打断按**设备身份**选择目标，不再只看 PID：voice 可取消同电视上由 console/scheduler 发起的任务。
- 设备不匹配 → 一律不动；本机设备身份未知时只允许操作本进程任务（保守）。
- 角色白名单 `voice.interrupt.roles`（默认 console/scheduler/voice/cli）。
- 前缀授权沿用 `voice.listener.prefix_only`：未带前缀的打断返回 `UNAUTHORIZED` 且不执行。
- 慢速媒体 ADB 操作移到独立高优先级线程；`handle()` 默认**不等待**，返回 `PENDING`，避免阻塞 ASR 读取；需要同步结果时可显式传 `wait=`。
- 仍走既有任务控制文件与 checkpoint，取消结果由任务自身状态核验。

## P1-2 复杂语义

- 新增/完善确定性规则（不依赖模型）：相似推荐（基于真实类型/地区元数据）、"不是这个换第二个"、"继续刚才看到的那一集"、"打开X，<音量/静音/暂停/搜索>"组合拆分、"先找国产悬疑剧再从第一集开始"先列真实候选再追问。
- 指代解析只依赖会话里的真实播放历史与候选；没有历史时一律追问。
- 家居+电视混合句改为追问（原先直接让行会丢掉电视那一半）。
- 观察缓存 `observe_ttl_seconds`（默认 1.5s）：连续多轮不再重复 dump 电视。
- Schema 扩展 `swap`、`resume_episode` 意图与 `like` 参数，仍受白名单/范围校验。

## 配置示例

```yaml
voice:
  intelligence:
    enabled: true
    observe_ttl_seconds: 1.5
  conversation:
    shared_dir: /data/runtime/voice_sessions   # 跨容器共享会话
  interrupt:
    allow_cross_container: true
    roles: [console, scheduler, voice, cli]
    media_wait_seconds: 3
  tts:
    enabled: true
    audio_base_url: "http://<NAS内网IP>:8770"   # 手机/网页取音频的地址
    async: true
```

`shared_dir` 留空则退回单进程内存会话（跨容器确认会失效，属预期降级）。

## NAS 更新

```bash
cd "$HOME/ai-tv-butler"
docker image tag ai-tv-butler:4.0.0b2 ai-tv-butler:pre-v403   # 保留回滚镜像
docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
curl -s http://<NAS内网IP>:8899/readyz      # 应含 version=4.0.0b3
```

`shared_dir` 必须是四个容器共享的 `/data` 子目录，否则跨容器确认无法生效。

## 回滚

```bash
cd "$HOME/ai-tv-butler"
docker image tag ai-tv-butler:pre-v403 ai-tv-butler:4.0.0b2
docker compose -p ai-tv-butler up -d --no-build
```

或直接关闭 Beta：`voice.intelligence.enabled=false` 并重启 voice 容器，回到 V3.4.x 语音路径。本版无数据格式迁移；`/data/runtime/voice_sessions` 可直接删除（仅会话快照）。