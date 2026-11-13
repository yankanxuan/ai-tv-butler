# V4.1 Final3：取消、退出、确认复核与上传边界修复

日期：2026-11-12。基于 Phase 2 Final2 增量修复，不重写 Agent、ADB、MediaIndex、ASR 或 Docker 架构。生产版本仍为 4.0.0b8，小爱仍是默认 ASR；实验服务仍默认关闭。

## 1. 修复结论

| 缺陷 | 修复 | 验证 |
|---|---|---|
| 取消失败仍报告 `cancelled` 并丢弃 `task_id` | 取消请求失败/超时返回 `cancel_failed` 或 `cancel_pending`，保留 `task_id`；只有控制台最终状态确认为 `CANCELLED` 才返回 `cancelled` 并清理 | 五种模拟回归 + 真实控制台延迟任务取消为 CANCELLED |
| 退出只清前端，不撤销服务端会话 | 新增 `POST /api/logout`，删除内存会话并返回 `Max-Age=0` 清除 Cookie；前端退出实际调用 | 旧 Cookie 再访问 `/api/session` 返回 401；旧 CSRF 不能继续 |
| 确认前不检查媒体文件 | `confirm` 复核快照、作品/季/集、路径、存在性、大小、mtime；删除/变更/重新匹配后要求重选 | 删除、大小变化、mtime 变化、重新 suggest 四类回归 |
| 候选过期只靠请求时清理 | `TitleSessionStore.start_cleanup()` 启动守护清理线程，服务关闭时停止 | 短周期线程清理回归 |
| 大于 2 MB 上传触发 BrokenPipe | 网关和 ASR 服务在超限时先有界排空请求，再返回 413；发送端捕获 BrokenPipe/Reset | 真实 HTTPS 网关 2,000,001 B → 413，随后会话仍 200；客户端早退后服务仍健康 |
| 手机实机未验收 | 未执行的项目保持“未验收”，不标记通过 | 见第 5 节 |

## 2. 取消语义

`POST /api/cancel` 返回以下状态之一：

| status | 含义 | task_id |
|---|---|---|
| `cancelled` | 取消请求被接受，且轮询确认最终 `CANCELLED` | 清理，返回本次 task_id 证据 |
| `cancel_pending` | 请求已发出，但 2 秒窗口内任务仍 RUNNING/状态查询失败 | **保留**，可安全重试 |
| `cancel_failed` | 控制台拒绝、HTTP 非 200 或网络超时 | **保留**，可安全重试 |
| `cancel_not_effective` | 任务已进入 SUCCESS/PARTIAL/FAILED/UNKNOWN/INTERRUPTED，最终不是 CANCELLED | **保留**，不再重试 |
| `cancelled`（无 task） | 仅有未提交候选，直接丢弃候选 | 无 |

实现要点：

- 不再先调用 `store.cancel` 再发取消；只有最终 CANCELLED 后才清理标题快照。
- 前端不再无条件清空 `state.taskId`；失败/待确认时保留任务号并显示“可安全重试”。
- 2 秒轮询上限避免请求长时间占用；用户可再次点击取消继续核对。
- 若任务最终成功或失败，明确显示 `cancel_not_effective`，绝不伪装取消。

## 3. 服务端退出

`POST /api/logout`：

- 从 `Portal.sessions` 删除当前 sid，并取消其标题候选快照。
- 返回 `Set-Cookie: vp_sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0`（HTTPS 时加 `Secure`）。
- 旧 Cookie 访问 `/api/session` 返回 401；旧 CSRF 随会话失效。
- 无 Cookie 调用也返回 200 并清 Cookie，避免前端退出卡死。
- 前端 `logout()` 先调用该接口，再清本地状态；请求失败也不会复用旧令牌，但会提示本地已退出、服务端响应可能未确认。

真实网关实测：登录 200 → 退出 200、`logged_out`、`Max-Age=0` → 旧 Cookie `/api/session` 401。

真实控制台取消实测：延迟任务 `等20秒后打开飞牛` 经 Final3 `Portal.cancel` 返回 `cancelled`、最终 `CANCELLED`，task_id 仅在确认后清理，电视未打开飞牛。

## 4. 确认前媒体复核与清理

`select` 现在把以下内部证据写入会话（**不返回给前端**）：

- 作品 ID、季、集；
- 媒体绝对路径、`size_bytes`、`mtime_ns`。

`confirm` 在设置 `confirmed` 前复核：

1. 候选快照未过期；
2. 作品仍属于当前快照，季/集仍存在；
3. 路径与快照中记录一致；
4. 文件存在、不是符号链接；
5. 当前大小和修改时间与选择时完全一致。

任一项失败：清空 `selected`/`selected_media`，返回 400，要求重新选择；不生成执行命令、不调用控制台。

清理：`create_server` 启动时调用 `store.start_cleanup()`，默认每 60 秒清除过期会话；`server_close` 停止线程。测试使用 0.05 秒周期确认线程确实清理并退出。

## 5. 真实手机验收：未执行

本轮执行环境只有：

- ADB 可见设备：`<TV_ADB_ADDRESS>`，型号 `MiTV_ASTP0`（电视）；
- 无声卡采集设备：`/dev/snd` 仅有 `controlC1`、`pcmC1D0p`、`seq`，无 capture PCM；`arecord` 未安装；
- 无 Android 手机或 iPhone 可操作。

因此以下项目**没有执行，不标记通过**：

- Android Chrome 真人麦克风；
- iPhone Safari 真人麦克风；
- 手机 HTTPS 证书信任/安全上下文；
- 手机端真实网络切换、锁屏、切后台；
- 手机真人说“找穿成野神”后的完整链路。

Phase 2 已验收的是：Chromium **模拟麦克风**的浏览器路径、授权 WAV 文件回放至真实电视的闭环，以及真实电视取消延迟任务。Final3 只新增/回归修复，不把模拟音频升级成手机实机证据。

## 6. 自动化测试

- **828 项单元测试通过，0 失败、0 跳过**（Final2 的 815 项 + 13 项新增回归）。
- **22 项模拟电视集成检查通过**。
- 新增覆盖：
  - 取消失败保留 task_id 并重试；
  - 取消超时保留 task_id；
  - 取消 pending 与最终 SUCCESS 不报告 cancelled；
  - 退出后旧 Cookie/CSRF 失效；
  - 确认前删除、改大小、改 mtime、重新匹配；
  - 过期会话线程清理；
  - 超限上传 413 后服务仍可用；
  - 客户端提前关闭连接后服务仍健康；
  - 前端实际调用 `/api/logout`，取消只在 verified 后清理。

完整结果见 [Final3 证据](evidence/v41_final3_fixes.json)。

## 7. 启动与回滚

沿用 Phase 2 的独立服务，默认关闭：

```bash
cd "$HOME/ai-tv-butler"

# ASR 实验服务
export ASR_LAB_MODEL_DIR="$HOME/.cache/ai-tv-v41"
export ASR_LAB_AUDIO_DIR="/path/to/authorized/audio"
export ASR_LAB_UID="$(id -u)"
export ASR_LAB_GID="$(id -g)"
export ASR_LAB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  --profile asr-lab up -d --build

# HTTPS 网关（需要证书和真实控制台地址/端口）
export ASR_LAB_URL="http://127.0.0.1:8765"
export CONSOLE_URL="http://<NAS_LAN_IP>:<CONSOLE_PORT>"
export VOICE_PORTAL_INDEX="$HOME/ai-tv-butler/deploy/data/media_index.json"
export VOICE_PORTAL_MEDIA_ROOT="/vol1/1000/影视"
export VOICE_PORTAL_CERT_DIR="$HOME/.cache/ai-tv-v41/tls"
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml \
  --profile voice-portal up -d --build
```

手机访问 `https://<NAS_LAN_IP>:8443`，必须信任证书或使用受信任证书/反向代理；首次需允许麦克风并勾选授权。

回滚：

```bash
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal down
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down
docker image rm ai-tv-voice-portal:v4.1-phase2 ai-tv-asr-lab:v4.1-phase2
```

生产 `ai-tv-butler` 四容器、Agent、ADB、MediaIndex、小爱和主 Docker 配置均未修改；本轮结束时实验端口 8443/8765 无监听，四容器 healthy。

## 7.5 Final3 实验镜像

| 镜像 | ID | 大小 |
|---|---|---:|
| `ai-tv-asr-lab:v4.1-phase2` | `sha256:7f8acf7b…` | 182,998,254 B |
| `ai-tv-voice-portal:v4.1-phase2` | `sha256:cf54912d…` | 137,627,077 B |

两镜像均用 Final3 源码重建；门户在默认 8443 健康检查 `healthy`。Phase 2 文档中的旧镜像 ID 已被本次重建取代。验收后实验服务均停止。

## 8. 许可证

Final3 没有新增第三方库：取消/退出/复核/清理/上传边界使用 Python 标准库与现有 RapidFuzz 3.14.3（MIT）。模型和运行时沿用 Phase 1/2 固定版本与哈希：

- sherpa-onnx 2.0.1 / core（Apache-2.0）；
- CTC / Transducer / Paraformer 权重（Apache-2.0 上游声明，固定 revision/hash）；
- Silero VAD v5.1.2（MIT，默认关闭）；
- RapidFuzz 3.14.3（MIT，`requirements-portal.lock` / `requirements-v41.lock`）。

完整证据仍见 `poc/models-v41.lock.json`、`poc/licenses/v41/`、`THIRD_PARTY_NOTICES.md`。

## 9. 未完成

- 真实手机麦克风、真实 HTTPS 证书信任和多移动端并发未验收。
- 多说话人语料（≥3 人、每条件 ≥50 句）仍未完成；现有 12 条单说话人不能替代。
- 24 小时长稳、ARM64、真实断网、常开 VAD/唤醒未验证；VAD 仍默认关闭。
- 取消后的最终 CANCELLED 依赖控制台真实任务状态；若控制台自身不可达，只报告 `cancel_pending/failed`，不猜测成功。