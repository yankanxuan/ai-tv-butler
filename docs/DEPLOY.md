# V3 部署、迁移与回滚

## 环境要求

- 飞牛 NAS / Linux，Python 3.9+（推荐 3.11）与 PyYAML 5.1+。
- 小米电视开启 ADB over TCP，NAS 已获得电视调试授权。
- 主程序仅依赖标准库与 PyYAML；视觉模型是可选外部服务。
- 控制台使用标准库 HTTP + 单页 HTML，不需要 Node.js、前端构建或数据库服务。

## 保留现有安装的升级

```bash
cd ~/ai-tv-butler
# 先保存版本和运行数据。release 不会自动删除任何任务。
python3 scripts/release.py pre-v3
python3 scripts/migrate.py --config config.yaml
make test
```

- 当前没有 Git 仓库；`scripts/release.py` 生成私有 tar.gz 源码快照，不回溯覆盖用户配置。
- 已验证的 V2 回滚基线：`~/ai-tv-butler-backups/pre-v21-20261110-171130.tar.gz`，SHA256 `2a7e7bbea1b69ef04fd194df738783c5dfbcfc3bbbdd4eca0eb7724394f8f709`。
- `scripts/migrate.py` 在写配置前创建 `.v2-backup-*` 文件。匿名 `0.0.0.0` 配置会收敛到本机回环地址；若需要局域网 UI，请显式设置强 token 和防火墙。
- V2.2 SQLite 启用后，自动在 `state/memory.sqlite3` 建库；`user_version` 较新时会拒绝写入，不会清空数据。

## 启动

```bash
# 原 CLI 仍可用
./bin/ai-tv doctor
./bin/ai-tv cmd "打开飞牛"
./bin/ai-tv task list

# 认证控制台
./bin/ai-tv serve
# 查看访问令牌（不要通过聊天或公开日志发送）
cat state/runtime/console.token
```

默认只监听 `127.0.0.1:8770`。若从手机访问：

1. 仅在可信局域网中把 `server.host` 改为 NAS 的局域网 IP。
2. 设置长随机 `server.token`（空值仍自动生成文件），用防火墙限制来源。
3. 使用 `X-Auth-Token: <token>` 调用 API，浏览器登录页填入同一 token。
4. 不应直接暴露到公网；需要远程使用时在 NAS 防火墙/VPN/反向代理中启用 HTTPS。

## 使用记忆与 Skills

```yaml
memory:
  enabled: true
  path: ""  # 默认 state/memory.sqlite3
```

```bash
./bin/ai-tv memory stats
./bin/ai-tv memory history
./bin/ai-tv memory backup --path state/memory-backup.sqlite3
./bin/ai-tv memory cleanup --days 30 --max-tasks 10000
./bin/ai-tv skill list
```

Skills 是白名单工具的 JSON 数据，不是 Python 代码。生产可执行 Skill 的审批顺序：

1. 候选任务成功后生成 draft，或自行提供经审核的 JSON 定义。
2. 控制台“隔离验证”，或 `ai-tv skill validate --id <id>`；验证只使用独立模拟电视。
3. 检查定义后点击“审批启用”，或 `ai-tv skill approve --id <id> --confirm`。
4. 只有 trigger/preconditions 匹配当前目标与页面时才执行。修改定义自动回到 draft。

模拟验证不是生产兼容性保证；生产首次使用仍需真机验收。模型不会自动启用新 Skill，也不会修改生产源码。已有 YAML Flow 保留兼容；飞牛内置导航默认由 `smart_navigation` 选择。

## Docker 部署

项目提供无特权 Dockerfile 和 Compose：

```bash
mkdir -p deploy/data
cp config.example.yaml deploy/data/config.yaml
chmod 700 deploy/data
chmod 600 deploy/data/config.yaml
# 修改 deploy/data/config.yaml 中 adb.host / llm / memory.enabled
# 不要写 API Key 到 Compose 文件。
docker compose build
docker compose up -d          # console + scheduler + media + voice 四个服务
docker compose ps             # 四个都应为 healthy
docker compose logs -f voice  # 语音监听日志（默认只显示长度与哈希）
```

容器以 UID 10001 运行，root filesystem 只读，移除 Linux capabilities，挂载 `/data` 保存 SQLite/配置/任务。`network_mode: host` 用于局域网 ADB；控制台默认仍只绑定回环地址，Compose 没有发布匿名端口。

首次容器启动前请确保：

- `deploy/data/config.yaml` 的 `server.host` 是 `127.0.0.1` 或受保护的局域网地址。
- 已保存的 token 对容器 UID 10001 可读；没有 token 文件时自动生成。
- 电视允许来自 NAS 的 ADB 连接。
- 模型 base_url 如指向宿主机，Linux 主机网络模式下 `127.0.0.1` 可访问；改用 bridge 时须另行配置地址。

当前环境 Docker daemon 不可访问时，无法声称镜像已实际构建；请将此步骤标为待部署环境验证。

## 语音

**媒体服务必须只有一份**：`media` 服务独占 `serve_port`；其余服务只签发链接。多进程各自绑定端口会在 V3.4.2 被明确拒绝。

健康检查可直接运行：

```bash
python3 scripts/healthcheck.py console | scheduler | media | voice
```

容器内实测（2026-11-11）：四个服务 healthy；`media` 的 `/health` 返回 `roots=["/media"]`；控制台 API 下发"播放/暂停/继续"均 SUCCESS。

`voice` 会区分三件事：监听进程是否在线、ADB 是否连上、最近是否真的收到日志（`log_age_seconds`）。

两条语音路径，用途不同：

1. **电视语音监听（推荐，V3.4.1）**：`voice.listener.enabled: true` 后运行 `ai-tv voice listen`（或启动 Compose 的 `voice` 服务）。
   音频与唤醒由电视完成，NAS 只旁听 logcat 中的 ASR 文本，**无需 root、无需安装**。
   - 只接管白名单内的媒体指令；天气/闲聊/智能家居让行小爱。
   - **无法阻止小爱同时执行**同一句话：可能双重执行的指令会带 `conflict_risk` 标记，执行后一律用状态核验（`SUCCESS/PARTIAL/FAILED/UNKNOWN`）。
   - 默认不在日志中保存语音原文，只记录长度与哈希。

2. **浏览器 Web Speech（可选）**：`voice.enabled: true`，音频来自手机或电脑浏览器麦克风，需要 HTTPS/localhost 与浏览器授权。

不包含：本地 ASR、唤醒词、TTS 回执（`reply_tts` 为预留开关）。

## 任务与 API 兼容

- 保留 `/api/cmd`、`/api/tool`、`/api/tasks`；现在所有认证控制接口必须 token。
- 新控制台用 `POST /api/runs` 返回 `task_id`，通过 `GET /api/runs/<id>/events` 接收 SSE。
- 暂停/继续/取消：`POST /api/runs/<id>/{pause|resume|cancel}`。
- 原 `ai-tv task pause/resume/cancel` 也可操作。
- 记忆读写失败不会伪造成功，会以 memory_warning 记录告警。
- `GET /health` 是无权限的健康探针，不返回设备、模型或配置。

## 回滚

```bash
# 当前程序仍在运行，先停掉相关 systemd/docker/手动服务。
# 在新目录恢复，不会直接删除当前目录。
python3 scripts/release.py --restore \
  ~/ai-tv-butler-backups/pre-v21-20261110-171130.tar.gz \
  --destination ~/ai-tv-butler-rollback
cd ~/ai-tv-butler-rollback/ai-tv-butler
make test
# 确认回滚版本后，停止V3，再切换部署目录/软链。
```

运行数据分别备份：SQLite 用 `ai-tv memory backup`，调度文件为 `state/tasks.json`。不要在旧版程序上直接使用它无法识别的新数据库 schema。源码回滚包不包含当前实时截图、密钥库、任务数据库；用户配置包含在源码回滚包中，请按敏感文件保管。


## V4.3 增量部署（自然语言助理可靠性）

版本：`4.3.0`。变更范围：`adbtv/assistant_loop.py`、`adbtv/library_tools.py`、`adbtv/llm.py`、
`adbtv/voice_runtime.py`、`adbtv/voice_intelligence.py`、`adbtv/conversation.py`、`adbtv/config.py`、
`adbtv/media_index.py`。无数据库迁移。

### 1. 备份

```bash
cd "$HOME/ai-tv-butler"
stamp=$(date +%Y%m%dT%H%M%S); echo "$stamp"
mkdir -p "$HOME/ai-tv-butler-backups/pre-v43-$stamp"; chmod 700 "$HOME/ai-tv-butler-backups/pre-v43-$stamp"
tar -czf "$HOME/ai-tv-butler-backups/pre-v43-$stamp/source-private.tar.gz" adbtv config.yaml config.example.yaml
cp deploy/data/config.yaml "$HOME/ai-tv-butler-backups/pre-v43-$stamp/config.yaml.private"
chmod 600 "$HOME/ai-tv-butler-backups/pre-v43-$stamp"/*
docker image tag ai-tv-butler:4.0.0b8 "ai-tv-butler:pre-v43-$stamp"
sha256sum deploy/data/media_key | tee "$HOME/ai-tv-butler-backups/pre-v43-$stamp/media_key.sha256"
```

### 2. 配置

确认 `deploy/data/config.yaml`：

```yaml
voice:
  intelligence:
    enabled: true          # 创建 VoiceDialogue
  assistant:
    enabled: true          # 自然语言助理循环
    max_steps: 4
    budget_seconds: 14     # 单回合绝对预算
    remember_preferences: false
```

### 3. 构建与更新

```bash
docker compose -p ai-tv-butler build --pull
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
docker compose -p ai-tv-butler exec -T console ai-tv --version    # 应为 ai-tv 4.3.0
```

### 4. 验收

```bash
docker compose -p ai-tv-butler exec -T console python3 -c   "from adbtv.assistant_loop import TOOL_SCHEMAS;print(sorted(TOOL_SCHEMAS))"
curl -fsS http://<NAS_LAN_IP>:<CONSOLE_PORT>/health
```

控制台语音区依次说：

1. 现在资源库里有什么？
2. 播放第二个。
3. 播放第二集。
4. 确认播放。

回滚见 [ROLLBACK.md](ROLLBACK.md)。

## V4.3.1 增量部署（事实可靠性修复）

版本：`4.3.1`。仅改 `adbtv/assistant_loop.py`（事实台账、字段绑定、播放进度 Schema）；`library_tools` 与配置格式未变，无数据库迁移，无新增依赖、无语音模型变更。

### 1. 备份（沿用 4.3 方式）

```bash
cd "$HOME/ai-tv-butler"
stamp=$(date +%Y%m%dT%H%M%S); echo "$stamp"
bk="$HOME/ai-tv-butler-backups/pre-v431-$stamp"
mkdir -p "$bk"; chmod 700 "$bk"
tar -czf "$bk/source-private.tar.gz" adbtv config.yaml config.example.yaml
cp deploy/data/config.yaml "$bk/config.yaml.private"
docker image tag ai-tv-butler:4.3.0 "ai-tv-butler:pre-v431-$stamp"
sha256sum deploy/data/media_key | tee "$bk/media_key.sha256"
```

### 2. 构建与更新

```bash
docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
docker compose -p ai-tv-butler exec -T console ai-tv --version   # 应为 ai-tv 4.3.1
```

### 3. 验收（只看真实观测，不看模型 SUCCESS）

```bash
docker compose -p ai-tv-butler exec -T console python3 - <<'PY'
from adbtv.assistant_loop import TOOL_SCHEMAS
print(sorted(TOOL_SCHEMAS))
print(TOOL_SCHEMAS['catalog_get_playback_progress']['returns'])
PY
```

在语音区依次问，并核对回答数字与 `catalog_summary` 的 movies/series/total/episodes 一致：

1. 现在资源库里有什么？
2. 一共有多少部电影？
3. 有多少部电视剧？
4. 给我推荐三部。
5. 我上次看到哪一集？

回滚步骤见 [V431_DELIVERY.md](V431_DELIVERY.md) 第 7 节与 [ROLLBACK.md](ROLLBACK.md)。
