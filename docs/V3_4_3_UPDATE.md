# V3.4.3 NAS 更新、验证与回滚

本版本只修复可靠性问题，保留现有 CLI、MCP、Web、语音和播放功能。

## 修改内容

- 媒体密钥初始化由持久的锁文件和 `flock` 串行化；密钥必须恰好 32 字节，直接读取二进制内容。独立临时文件完整写入、flush/fsync、原子替换并同步父目录。读取、生成、加锁或持久化失败都会报错。
- 会话文件读取、保存或锁失败不再被吞掉，不会向 Agent 返回未持久化的成功链接。
- `/livez` 只检查 HTTP 进程；`/readyz` 和兼容入口 `/health` 检查媒体根目录实际访问能力、密钥长度及与进程密钥的一致性。不可用返回 503，健康响应不包含绝对路径。
- 绑定、Agent 探活及电视 URL 使用统一地址解析。配置为空时优先选择通向电视 ADB 地址的本机 IPv4 路由；失败明确要求设置 `media.advertise_host`，不再回退到 loopback。手动设置 `advertise_host` 优先。显式 loopback 仅供本机测试/内部健康探测，Agent 默认拒绝用于电视播放。
- Docker 媒体健康检查实际请求 `/readyz`。四个服务继续共享数据目录和只读媒体库。
- public 包排除 `.env` 及其私有变体，只允许 `.env.example`；源码预审后再审计压缩包成员及实际内容，审计失败不替换已发布文件。检测通用凭据赋值和用户绝对主目录路径。

## 更新前备份

先等正在执行的电视任务完成。以下命令在 NAS 上有 Docker 权限的终端执行；不要在另一个目录直接运行 Compose。

```bash
cd "$HOME/ai-tv-butler"
umask 077
stamp=$(date +%Y%m%d-%H%M%S)
backup="$HOME/ai-tv-butler-backups/pre-v343-$stamp"
mkdir -p "$backup"
# 保留旧镜像，避免回滚时需要重新构建。
docker image tag ai-tv-butler:3.4.2 ai-tv-butler:pre-v343
# 停止后备份，避免 SQLite/会话文件写入过程中复制。
docker compose -p ai-tv-butler stop
tar -czf "$backup/source-private.tar.gz" --exclude='./deploy/data' --exclude='./state' --exclude='./logs' --exclude='./.git' .
```

数据目录以 `.env` 中 `BUTLER_DATA_DIR` 的实际值为准。默认目录可这样备份：

```bash
tar -czf "$backup/data-private.tar.gz" -C deploy data
```

这些是私人备份，含本地配置及密钥，不可公开分享。**不要删除或重新生成现有 `media_key`**，有效会话依赖原密钥；发现旧密钥不是 32 字节时，本版本会明确失败，需停止服务并从备份恢复，不能静默替换。

## 更新与启动

公开包不包含本地配置与数据。将已下载的压缩包解到原源码目录，保留现有 `.env`、本地配置和数据：

```bash
cd "$HOME/ai-tv-butler"
tar -xzf /path/to/ai-tv-butler-3.4.3-public.tar.gz --strip-components=1 -C .
docker compose -p ai-tv-butler config --quiet
docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
```

`/path/to` 是下载文件的实际位置。新部署先复制 `.env.example`，按实际路径配置 `BUTLER_DATA_DIR`、`MEDIA_ROOT`；现有部署不要覆盖原 `.env`。媒体库在所有容器内保持只读路径 `/media`。

推荐在私人配置中显式设置 NAS 内网地址：

```yaml
media:
  serve_host: "192.0.2.10"
  advertise_host: "192.0.2.10"
```

`192.0.2.10` 是文档示例地址，必须替换成真实 NAS 地址。留空时需要正确配置 `adb.host` 或网络 `adb.serial`，并存在通向电视的 IPv4 路由。若自动发现失败，服务明确报配置错误。

## Docker 验证

```bash
docker compose -p ai-tv-butler ps
for service in console scheduler media voice; do
  docker compose -p ai-tv-butler exec -T "$service" ai-tv --version
done
docker compose -p ai-tv-butler exec -T media python3 /app/healthcheck.py media
docker compose -p ai-tv-butler exec -T voice python3 /app/healthcheck.py voice
```

四个服务应显示 healthy，版本均为 3.4.3。媒体 `/readyz` 的 JSON 应包含 `version: 3.4.3`，不得含 roots/绝对路径；不要以旧镜像的 `/health` 200 代替新版验收。`/livez` 200 也不能替代 readiness。

之后在控制台运行：指定剧集播放 → 暂停 → 继续，分别检查真实电视的 PLAYING/PAUSED。必须区分源码直连测试和容器内测试，不能将旧容器健康冒充新版已上线。

## 回滚

如更新失败，先停止新容器，从刚才的私人源码备份恢复原代码和 Compose，并使用保留的旧镜像启动：

```bash
cd "$HOME/ai-tv-butler"
docker compose -p ai-tv-butler stop
# 将 backup 改成上面实际生成的备份目录。
tar -xzf "$backup/source-private.tar.gz" -C .
docker image tag ai-tv-butler:pre-v343 ai-tv-butler:3.4.2
docker compose -p ai-tv-butler up -d --no-build
```

本次没有运行数据格式迁移，通常保留现有数据即可；需要恢复数据时必须先停止全部容器，再将私人数据备份恢复到原 `BUTLER_DATA_DIR`。密钥与会话文件要成对恢复，不能混用。

## 验收记录

完整结果见同目录 `V3_4_3_TEST_RESULTS.md`。源码、独立解包测试和真实电视均通过；用户完成镜像重建后，又核验了新版 readiness 与容器 API 播放。报告明确区分接口/心跳验证与无法读取的 Docker 内部 Health 字段，不把旧版本运行状态算作 V3.4.3 验收。
