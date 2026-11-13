# V4.1 Final4：并发确认、幂等提交与媒体密钥测试稳定性

日期：2026-11-12。基于 Final3 增量修复，不新增常开麦克风、唤醒词或大模型；小爱仍是生产默认 ASR。生产版本仍为 4.0.0b8，实验服务仍默认关闭。

## 1. 根因与修复

并发复现：两个线程同时调用 `Portal.confirm()`，两者都在未加锁的 `task_id` 检查处看到空值；`TitleSessionStore.confirm()` 也没有会话锁，两个线程都能通过媒体复核并标记 confirmed，随后各发一次 `/api/runs`。结果两个播放任务，会话只保留最后一个 task_id。

修复分为三层：

1. **TitleSessionStore 全量会话锁**：`suggest/seasons/episodes/select/confirm/cancel/cleanup` 使用同一 `RLock` 串行化；`confirm` 的“复核 → 标记 confirmed → 返回命令”不可分割。
2. **Portal 每会话锁与提交状态机**：会话新增 `lock`、`submission`、`idempotency_key`、`submission_command`、`submission_error`、`confirmed_selection`。确认、取消、退出、清理都遵守同一把会话锁。
3. **幂等提交**：每次确认生成唯一 `X-Idempotency-Key`，先写入会话状态再调用控制台；重复确认返回原 task_id 并查询任务状态，不再创建新任务。

## 2. 提交状态机

| submission | 含义 | 重复确认行为 |
|---|---|---|
| `idle` | 无任务 | 允许一次原子提交 |
| `submitting` | 提交进行中（会话锁持有者） | 其他线程等待；不并发提交 |
| `submitted` | 控制台返回 task_id | 返回同一 task_id，并查询原任务状态；不新建 |
| `unknown` | 超时/连接中断/5xx/202 无 task_id | 返回 `submit_unknown`，保留幂等键；**不自动重试** |
| `cancelled` | 已确认 CANCELLED 或无任务取消 | 不重新提交 |

规则：

- 只有 HTTP 202 且响应含 task_id 才记为 `submitted`。
- HTTP 400/401/403/409/422 是“已拒绝、未创建任务”，清除已消费的标题快照，允许用户重新选择/确认。
- 超时、连接重置、5xx、202 缺少 task_id 属于“结果未知”，保留 key，绝不创建第二个任务；识别新请求也会被 409 拒绝，避免越过未知状态创建重复任务。
- 已知 task_id 时重复确认会先 `GET /api/runs/<id>` 恢复状态，而不是重新提交。
- `X-Idempotency-Key` 同时放入请求头；控制台当前忽略未知头也不影响门户侧的单次提交，未来控制台可按该键扩展查询。

## 3. 确认与取消/退出/清理并发

- **确认 + 取消**：取消获取同一会话锁；若确认已提交，则可对同一 task_id 取消；若取消先发生，确认看到 `cancelled` 或被注销，不再提交。两者不会各创建一个任务。
- **确认 + 退出**：logout 先移除会话，等待正在进行的确认完成，然后返回 `active_task_id`/`submission` 供客户端明确处理；旧 Cookie/CSRF 随即失效。不会静默丢弃正在提交的任务。
- **确认 + 过期清理**：清理线程先获取会话锁；正在提交的会话跳过；已提交且有 task_id 的会话延长 TTL，避免任务期间被清理。确认与清理同时发生时，只有一个结果。
- 识别与确认也互斥：`transcribing` 状态阻止确认；确认中的状态阻止新识别。识别异常在 `finally` 中释放标志，避免死锁。
- Final3 的确认前媒体复核保留：路径、存在性、大小、mtime、候选快照变化仍会被拒绝并要求重选。

## 4. 新增/调整测试

- 两个 HTTP 线程同时 `/api/confirm`：**只调用一次 `/api/runs`，两个响应均返回同一 task_id**，并恢复任务状态。
- 确认 + 取消并发：只提交一次；取消结果必须为 `cancelled` 或明确的 `cancel_not_effective`。
- 确认 + 退出并发：确认成功；logout 返回 `active_task_id`；旧 Cookie 随后 401。
- 确认 + 过期清理并发：只提交一次；有 task_id 的会话不被清理。
- 网络响应丢失后重复确认：第一次 `submit_unknown`，第二次仍 `submit_unknown`；控制台提交计数保持 1；幂等键相同。
- 直接 `TitleSessionStore.confirm` 两线程：恰好一个成功、一个 `already confirmed`。
- **100 轮双线程确认压力测试**：每轮两个线程同时确认，只产生一个 task；独立运行 1.012 秒通过。

最终回归：

- **836 项单元测试通过，0 失败、0 跳过**，95.729 秒。
- **22 项模拟电视集成检查通过**。
- 新增/调整测试覆盖 Final4 全部并发场景；无新增第三方依赖，继续使用现有 RapidFuzz（MIT）和标准库。

## 5. 真实控制台复验

用真实控制台提交延迟任务 `等20秒后打开飞牛`，再由 Final4 `Portal.cancel` 取消：

- 返回 `cancelled`，最终服务端状态 `CANCELLED`；
- task_id 仅在确认后清理；
- 电视未打开飞牛；
- 随后 logout 返回 `revoked=true`，旧会话访问失败。

真实 HTTPS 网关此前已验证 logout 清 Cookie、2,000,001 B 上传返回 413；Final4 未改变这些路径。

## 6. 媒体密钥子进程 30 秒问题

定位：`tests/test_media_v343.py::test_16_process_initialization_for_10_rounds` 使用 16 个冷启动子进程 × 10 轮，每个 `communicate(timeout=30)`。在 2 核 NAS 上：

- 连续 5 次原测试：**21.4–22.3 秒全部通过**，未观察到死锁；`MediaAuth` 的文件锁能正常串行化。
- 剩余风险不是锁死，而是 16 个 Python 冷启动/导入在系统负载下接近每子进程 30 秒；原测试失败时也没有 `finally` 杀死/回收其余 15 个子进程，会留下占资源的子进程并放大下一轮失败。

修复测试本身：

- 改为**共享 45 秒总截止时间**，不是每个子进程重新给 30 秒；
- 超时时报告仍存活 PID，明确提示“CPU 饥饿或锁死锁”；
- `finally` 中 kill 并 `communicate` 回收所有子进程，绝不留下孤儿；
- 保留 16 进程 × 10 轮的真实并发和文件锁验证。
- 修复后连续 3 次：**22.75 / 21.16 / 22.19 秒全部通过**。

这不是通过无限增加超时掩盖问题：总时限有限、失败会清理、诊断信息可区分“未退出”和“已退出但慢”。若未来仍超时，应先查系统负载/子进程导入，再查文件锁。

## 7. 手机实机状态

本轮仍没有 Android 手机或 iPhone 可操作；ADB 只看到小米电视，主机无 capture PCM。**Android Chrome / iPhone Safari 真人麦克风、HTTPS 信任、手机端确认与取消均未执行，不标记通过。**

## 7.5 Final4 实验镜像

| 镜像 | ID | 大小 |
|---|---|---:|
| `ai-tv-asr-lab:v4.1-phase2` | `sha256:7da9627c…` | 183,018,768 B |
| `ai-tv-voice-portal:v4.1-phase2` | `sha256:f0bf0585…` | 137,648,711 B |

两镜像均已用 Final4 源码重建；门户默认 8443 健康检查 `healthy`，ASR `/health` ready。验收后两实验服务均停止。

## 8. NAS 更新步骤

Final4 只改网关/片名会话与测试，ASR 容器模型和依赖未变。更新时重建两个实验镜像并重启：

```bash
cd "$HOME/ai-tv-butler"

# 1. 先停实验服务，避免旧进程占用端口
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal down
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down

# 2. 重建（依赖仍由哈希锁/固定基础镜像保证）
docker build --network=host -f deploy/asr-lab/Dockerfile -t ai-tv-asr-lab:v4.1-phase2 .
docker build --network=host -f deploy/voice-portal/Dockerfile -t ai-tv-voice-portal:v4.1-phase2 .

# 3. 启动 ASR 实验
export ASR_LAB_MODEL_DIR="$HOME/.cache/ai-tv-v41"
export ASR_LAB_AUDIO_DIR="/path/to/authorized/audio"
export ASR_LAB_UID="$(id -u)"
export ASR_LAB_GID="$(id -g)"
export ASR_LAB_TOKEN="$(cat "$HOME/.cache/ai-tv-v41/lab.token")"
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab up -d --no-build

# 4. 启动 HTTPS 网关；CONSOLE_URL 必须是实际控制台地址/端口
export ASR_LAB_URL="http://127.0.0.1:8765"
export CONSOLE_URL="http://<NAS_LAN_IP>:<CONSOLE_PORT>"
export VOICE_PORTAL_INDEX="$HOME/ai-tv-butler/deploy/data/media_index.json"
export VOICE_PORTAL_MEDIA_ROOT="/vol1/1000/影视"
export VOICE_PORTAL_CERT_DIR="$HOME/.cache/ai-tv-v41/tls"
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal up -d --no-build

curl -fsS http://127.0.0.1:8765/health
curl -kfsS https://127.0.0.1:8443/api/health
```

生产 `ai-tv-butler` 四容器不需要重建，也不应在实验更新时重启。

## 9. 回滚

```bash
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal down
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down
docker image rm ai-tv-voice-portal:v4.1-phase2 ai-tv-asr-lab:v4.1-phase2
```

回滚不影响生产四容器、Agent、ADB、MediaIndex、小爱或主 Docker 配置。实验模型/音频/证书由操作者自行保留或删除。

## 9.5 证据

[Final4 脱敏证据](evidence/v41_final4_reliability.json) 记录 836 项测试、100 轮并发、真实取消失败/成功路径、媒体密钥耗时和实验镜像 ID。

## 10. 未完成

- 真实手机麦克风与移动端并发未验收。
- 控制台尚未实现按 `X-Idempotency-Key` 查询的持久化索引；门户通过本地状态避免重试，进程重启后未知提交仍只能人工核对。若需跨进程恢复，应另立控制台任务索引改造。
- 24 小时长稳、ARM64、真实断网、常开 VAD/唤醒未验证。