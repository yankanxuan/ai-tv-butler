# V4.1 Final5：会话生命周期与持久化幂等

日期：2026-11-12。基于 Final4 增量修复，不重写语音网关、TitleSessionStore、ADB、Agent 或媒体服务。生产版本仍为 4.0.0b8；小爱仍是默认 ASR，独立 ASR 服务仍可选、默认关闭。不增加常开麦克风、唤醒词或新大模型。

## 1. 根因

Final4 的登录会话在 `_sweep()` 中只要存在 task_id 就刷新 `expires_at`，导致已完成任务也能让旧 Cookie 无限续期；任务关联与登录授权混在同一个 session 对象里。幂等键只存在 Portal 内存，控制台没有持久化登记，进程重启后无法恢复。

## 2. 登录会话生命周期

- 空闲 TTL：**30 分钟**（每次受认证请求刷新 last_seen）。
- 绝对上限：**8 小时**（从 login 创建时间起，任何请求都不能突破）。
- 过期判定在读取会话时和清理线程中执行；过期 Cookie 返回 401，不再续期。
- **任务不再延长登录**：已完成/运行中的 task_id 都不会改变 `expires_at`。
- 任务追踪记录 `task_links` 与 auth session 分离；登录过期/注销只移除授权会话，不调用控制台取消，不终止电视任务。
- 重新登录后通过 `GET /api/history` 查询控制台记录的、属于同一控制台令牌哈希的历史任务；不能看到其他令牌的任务。
- 登录过期清理标题候选快照，但不触碰控制台任务。

## 2.5 Cookie 跟随服务端滑动 TTL

登录 Cookie 初始 `Max-Age=1800`；此后每个受认证响应都会用服务端**剩余有效期**重发 `Set-Cookie`，正常活跃时回到 1800 秒，接近 8 小时绝对上限时缩短为剩余秒数。服务端仍是唯一判定者；浏览器不再在 30 分钟固定到期时提前丢失 Cookie，也不会突破绝对上限。`HttpOnly/Secure/SameSite=Strict` 保持不变。

## 2.6 启动恢复

- 每个控制台启动时持有 `runtime/instances/<instance_id>.lock` 的 `flock` 整个生命周期；旧实例是否存活由该锁是否可取得判断，**不再依赖 PID**。因此 Docker 新实例复用 PID 1 或相同 PID 不会把死实例误判为存活。
- `runtime/recovery-leader.lock` 以非阻塞方式领取；多个控制台同时启动时只有一个实例执行待恢复任务领取，其他实例跳过。
- 启动扫描只对**已死控制台实例**拥有的任务做处置；任务记录写 `recovery_owner_instance_id`，另一个存活实例已认领的 QUEUED 不再入队。
- 状态 `QUEUED`：原子重新入队原 task_id，不生成新任务；任务会在 worker 收到任务前设置 RUNNING，因此重放 QUEUED 不会重复执行设备动作。
- 状态 `RUNNING/PAUSED`：一律改为 `INTERRUPTED`，结果 `status=UNKNOWN`，绝不重放。
- 状态已终态：不改动。
- 幂等记录 committed 但任务 JSON 缺失：历史查询明确返回 `task_state=UNKNOWN`；不会新建或伪造。
- 恢复时写 recovery owner PID；另一存活的 console 已认领的 QUEUED 不会被第二个进程再次入队。
- 幂等 pending + 已存在 task JSON：标记 committed；不重复创建。
- 新增 13 项启动恢复/迁移故障注入测试（committed+QUEUED 重放、RUNNING→INTERRUPTED、终态不重放、缺失任务 UNKNOWN、Docker PID 1 复用、双实例并发恢复、存活租约异常退出后接管、旧 recovery_owner_pid 无实例迁移、重复启动稳定、迁移保留幂等记录），以及 Cookie 滑动 TTL 测试。

## 3. 持久化幂等

新增 `adbtv/idempotency.py`，使用已有 SQLite 能力（Python 标准库 `sqlite3`），数据库位于：

`runtime/idempotency.sqlite3`

表 `idempotency(owner, key, task_id, command_hash, state, created, updated)`，主键 `(owner,key)`：

- `owner = SHA-256(console token)`，不存明文令牌；其他令牌不能读同 key 记录。
- 同一 owner/key 重复提交，若 command_hash 相同，返回原 task_id；command_hash 不同返回 409，绝不误关联。
- 任务创建与幂等记录在同一个 SQLite `BEGIN IMMEDIATE` 事务中完成；任务 JSON 先创建、入队前不通知 worker，提交成功后才通知。
- 失败回滚同时清理半创建 task JSON/队列项，不会执行无持久记录的任务。
- 记录先写 `pending`，任务文件存在后更新 `committed`；启动/重放时对 pending：
  - task JSON 已存在 → 标记 committed 并返回原 id；
  - 没有 task JSON → 复用原 id 完成创建，不生成第二个任务。
- 已 committed 但任务文件缺失时失败关闭，不创建新任务也不伪造成功。
- 保留 **7 天**；清理时先 reconcile 旧 pending，再删除过期记录。
- 旧客户端不带幂等键时行为完全不变。

控制台新增受认证保护接口：

- `GET /api/idempotency`：当前令牌主体最近 50 条记录（task_id/key/时间/task 状态），不返回命令文本。
- `GET /api/idempotency/<key>`：仅当前主体记录；不存在 404。
- `POST /api/runs` 接受 `X-Idempotency-Key` 头或 `idempotency_key` 体字段；两者不一致 400。
- 202 响应新增 `idempotent` 布尔值；旧字段 `task_id` 保持。

## 4. 验证结果

- **869 项单元测试通过，0 失败、0 跳过**，104.391 秒。
- **22 项模拟电视集成检查通过**。
- 新增 SQLite 幂等存储测试 7 项：重放、命令冲突、pending 恢复、创建失败回滚、owner 隔离、保留清理、数据库故障。
- 新增控制台 HTTP 测试 7 项：同键顺序/并发只生成一个任务、命令冲突 409、旧客户端兼容、历史查询认证、服务重启保持 task_id、数据库故障阻止创建。
- 保留 100 轮双线程确认压力测试：每轮只生成一个 task；单项 1.012 秒通过。
- 登录测试覆盖：已完成任务过期、运行中任务过期但不取消、绝对上限、注销、并发清理、重新登录历史。
- 媒体密钥 16 进程×10 轮测试仍保留；Final4 已改为共享 45 秒截止和 finally kill/reap，Final5 全量回归约 22 秒通过。未观察到文件锁死锁；原问题主要是冷启动/CPU 争用和失败时未回收子进程。

脱敏证据见 [Final5 证据](evidence/v41_final5_reliability.json)。

## 4.5 Final5 实验镜像

| 镜像 | ID | 大小 |
|---|---|---:|
| `ai-tv-asr-lab:v4.1-phase2` | `sha256:7da9627c…` | 183,018,768 B |
| `ai-tv-voice-portal:v4.1-phase2` | `sha256:0b95f11e…` | 137,654,349 B |

门户默认 8443 健康检查 `healthy`；ASR `/health` ready。实验服务已停止。最终 Final5（含 Cookie/恢复）门户镜像 ID 为 `sha256:0b95f11e…`，大小 137,654,349 B。生产 console 已按第 6 节完成 Final5 镜像部署、四服务健康检查、重启幂等、媒体密钥/任务历史核对和 QUEUED/RUNNING 故障注入。

## 5. 真实手机状态

本轮仍没有 Android 手机或 iPhone 可操作；ADB 只看到小米电视，主机无 capture PCM/`arecord`。**手机真人麦克风、HTTPS 信任、手机端确认/取消均未执行，不标记通过。**

## 5.5 真实 Docker 验收

已用 Final5 源码构建并部署主镜像 `ai-tv-butler:4.0.0b8`（ID `sha256:eb0c8c1a…`），旧镜像保留回滚 tag；先完成独立临时 `/data` 注入，再完成生产部署与重启注入：

- 两个并发请求使用同一 `X-Idempotency-Key`：都返回 202 且 task_id 相同，只有一条任务记录。
- 停止并重启容器后同一 key 再次请求：返回原 task_id，`idempotent=true`，未新建任务。
- 注入 `QUEUED` 且 `owner_pid=1`、实例锁已死的任务文件：启动后以原 task_id 执行一次，结果由无 ADB 环境判定为 `UNKNOWN`，没有重复任务。这直接覆盖 Docker PID 1 复用。
- 注入 `RUNNING` 且 `owner_pid=1`、实例锁已死的任务文件：启动后标为 `INTERRUPTED`，结果 `status=UNKNOWN`，未重放。
- 旧格式 `recovery_owner_pid=1` 且无 `recovery_owner_instance_id` 的 QUEUED/RUNNING 任务：均迁移为 `INTERRUPTED/UNKNOWN`，保留原 task_id、指令、事件和 `migration.original_recovery_owner_pid=1`，未执行任何播放。
- 测试容器使用临时数据、非 root、只读根和 tmpfs；验收后已停止。生产四容器未重建。

生产四服务均 healthy、版本 4.0.0b8；备份与回滚 tag 已记录在 `pre-final5-20261112T192004`。

### 5.6 生产部署与真实故障注入结果

- 备份：`pre-final5-20261112T192004/data-private.tar.gz`，包含 `deploy/data`、`config.yaml`、`docker-compose.yml`、`.env`，SHA-256 `43ba6da4…`；旧镜像 tag `ai-tv-butler:pre-final5-20261112T192004`（`sha256:0fcb7301…`）。
- 新镜像：`ai-tv-butler:4.0.0b8`（`sha256:eb0c8c1a…`）。
- 四服务 console/scheduler/media/voice 全部 `healthy`，版本 `ai-tv 4.0.0b8`。
- 媒体密钥前后 SHA-256 均为 `6300f53d…`，未重建。
- 原有 225 个任务文件保留；新增 1 个幂等验证任务，清理 3 个故障注入任务后共 226 个。
- 生产 console 注入两个同键并发请求：只生成 1 个 task_id；重启 console 后同键返回原 task_id。
- 实际重启 console 注入：新格式 QUEUED 被执行一次；旧格式 `recovery_owner_pid=1` 的 QUEUED/RUNNING 均迁移为 `INTERRUPTED/UNKNOWN`，未执行播放。
- 故障注入任务已清理；幂等验证记录保留。
- **Android Chrome 真人麦克风未执行**：没有可用手机，不能把模拟麦克风或 WAV 回放冒充真人语音。

## 6. NAS 更新步骤

Final5 修改了生产控制台代码（`adbtv/server.py`、新增 `adbtv/idempotency.py`），因此持久化幂等需要重建并重启**生产 console 镜像**；独立 ASR/门户仍可选。**已于 2026-11-12 19:20 实际部署**；当前四容器镜像为 `sha256:eb0c8c1a…`，旧镜像 tag 为 `ai-tv-butler:pre-final5-20261112T192004`。

```bash
cd "$HOME/ai-tv-butler"
stamp=$(date +%Y%m%dT%H%M%S)

# 1. 记录当前生产镜像，并备份 /data（使用原有备份流程/目录，0600/0700）
docker image tag ai-tv-butler:4.0.0b8 "ai-tv-butler:pre-final5-${stamp}"
# 按现有 deploy_b8/migrate 备份流程冷备 /data；至少保留 config.yaml、media_key、
# runtime/console.token、runtime/idempotency.sqlite3（如已存在）。

# 2. 先停独立实验，避免端口/资源干扰
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal down || true
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down || true

# 3. 重建生产镜像并滚动更新四容器（保持原 compose/数据/端口）
docker compose -p ai-tv-butler build --pull
docker compose -p ai-tv-butler up -d

# 4. 验证
curl -fsS http://<NAS_LAN_IP>:<CONSOLE_PORT>/health
docker compose -p ai-tv-butler exec -T console ai-tv --version
docker compose -p ai-tv-butler exec -T console \
  python3 -c "import sqlite3;print(sqlite3.connect('/data/runtime/idempotency.sqlite3').execute('select count(*) from idempotency').fetchone())"
```

四容器仍共享同一镜像；console 承载 `/api/runs` 幂等，scheduler/voice/media 不需额外配置。生产小爱入口、ADB 授权、媒体只读挂载和现有 compose 不变。

## 7. 回滚

```bash
cd "$HOME/ai-tv-butler"
docker image tag ai-tv-butler:pre-final5-${stamp} ai-tv-butler:4.0.0b8
docker compose -p ai-tv-butler up -d
```

回滚后旧 console 会忽略 `idempotency.sqlite3`，数据库可保留；不要删除当前 `/data`。若需要同时回滚实验服务：

```bash
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal down
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down
```

生产四容器以外的数据、模型、证书由操作者自行保留。

## 7.5 旧格式恢复记录迁移

旧版本可能只写 `recovery_owner_pid`，没有 `recovery_owner_instance_id`。PID 可被 Docker 复用（尤其 PID 1），因此无法通过 PID 证明恢复进程是否存活，也无法证明任务是否尚未执行。

新版处理：

- 扫描到“有 `recovery_owner_pid`、无实例 ID”的旧记录时，不再调用 `os.kill`，直接保守迁移。
- 状态改为 `INTERRUPTED`，结果 `status=UNKNOWN`，禁止重放。
- 保留原 task_id、instruction、events、幂等记录；新增 `migration.from=recovery_owner_pid`、`original_recovery_owner_pid`、`migrated_at`、`reason` 审计信息。
- 新格式任务只使用实例 flock 租约判断所有权，不退回 PID 判断。
- 真实容器注入 `recovery_owner_pid=1` 的 QUEUED/RUNNING 旧任务，均迁移为 INTERRUPTED/UNKNOWN，未执行播放。

## 8. 边界

- 控制台仍只有一个部署级 token；`owner` 区分的是令牌哈希，不是多租户账号。共享同一 token 的客户端会看到同一历史集合，这是现有单用户模型，不宣称为多租户隔离。
- 幂等记录保留 7 天；过期后同 key 可重新创建任务。若业务要求永久幂等，需要独立审计策略。
- 启动恢复现在会重放已死实例的 QUEUED 任务；RUNNING/PAUSED 与旧格式恢复记录保守标记 INTERRUPTED/UNKNOWN，不重放。若 pending 记录存在但 task JSON 从未创建，则没有可证明安全的任务可执行，保留 UNKNOWN/不创建。
- 未增加常开麦克风、唤醒词或新模型；小爱仍是生产默认。手机实机仍未验收。
