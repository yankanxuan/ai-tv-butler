# V4.0.0b4 语音交互闭环修复

基线 V4.0.0b3。本轮不重写跨容器确认、TTS 异常恢复与跨容器打断，只修闭环缺口。

## 一、跨容器会话状态（P1）

`adbtv/conversation.py`：

- `hydrate()` 现在**完整替换**共享字段：`current_app`、`recent_results`、`selected_content`、`current_playback`、`played_history`、`waiting_question`、`pending_action`。快照里没有选择时，旧选择被显式清空（此前会残留）。
- 快照新增 `expires_at`（空闲到期时间），`export()` 写入、`hydrate()` 校验：
  - 已过期 → `clear()` 并返回 `False`，不会复活旧上下文；
  - 截止时间不可解析 → 视为不可信，同样失效；
  - 未过期 → 恢复 `session_id` 与 `expires_at`，继续按原 TTL 计时。
- 待确认操作保留**独立 60 秒**有效期（`take_pending` 自行判断），与会话 TTL 解耦。
- 损坏快照（非字典、非法 id、含 shell 的 pending）一律安全丢弃，不抛异常、不导入危险值。

`adbtv/voice_input.py` 的 `SharedSessionStore.transaction()` 仍以 `flock` 覆盖整轮读改写，两个容器同时清除/恢复不会互相覆盖。

## 二、Web 控制台接入真正的 VoiceDialogue（P1）

`adbtv/web/index.html` 新增独立「语音对话」区：

- 网页麦克风（Web Speech）识别后**直接发 `/api/voice`**，不再只是填充普通任务框；
- 显示 Agent 自然语言回复、真实候选列表（可点击选择第 N 部）、确认/取消按钮；
- 显示会话 ID、状态、路由与执行/验证 JSON；
- 支持连续多轮文字输入与语音输入；
- 普通任务输入保持 `/api/runs` 不变。

## 三、异步 TTS 交付（P2）

`adbtv/voice_response.py`、`adbtv/server.py`、`adbtv/index.html`：

- 每次 TTS 请求生成唯一 `job_id`，状态机 **QUEUED → SYNTHESIZING → READY / FAILED**；
- 认证接口 `GET /api/voice/tts/<job_id>` 查询状态；`READY` 时返回 `audio_url`；
- 网页用**带 `X-Auth-Token` 的 fetch** 取 WAV（blob → Audio 播放），不使用未认证直链；
- 合成成功不等于已播放：`played` 字段恒为 0，`status()` 明确 `tv_audio=false`；
- Piper 或模型缺失时任务进入 `FAILED` 并给出原因，工作线程继续服务后续请求；
- 队列满返回 `tts_queue_full` 且任务标记失败，不谎报成功。
- 文档统一控制台默认端口 **8770**（此前示例误写 8771）。

## 配置

```yaml
server: { port: 8770 }          # 控制台默认端口，audio_base_url 与之一致
voice:
  intelligence: { enabled: true }
  conversation: { shared_dir: "/data/runtime/voice_sessions" }
  tts:
    enabled: true
    audio_base_url: "http://<NAS内网IP>:8770"
    async: true
```

## NAS 更新

```bash
cd "$HOME/ai-tv-butler"
docker image tag ai-tv-butler:4.0.0b3 ai-tv-butler:pre-v404
docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
curl -s http://<NAS内网IP>:8899/readyz          # version 应为 4.0.0b4
```

浏览器打开 `http://<NAS内网IP>:8770`，用 `runtime/console.token` 登录，页面出现「语音对话」区即可验证。

## 回滚

```bash
docker image tag ai-tv-butler:pre-v404 ai-tv-butler:4.0.0b3
docker compose -p ai-tv-butler up -d --no-build
```

或仅关闭 Beta（`voice.intelligence.enabled=false`）恢复旧语音路径。无数据迁移；`/data/runtime/voice_sessions` 可随时删除。

## 明确未做

- 电视扬声器播报（实测不可用，`played` 恒 0）；
- 真人麦克风经容器 ASR 的端到端验收；
- 4.0.0b4 镜像构建与四容器健康（本会话无 Docker socket 权限）。
