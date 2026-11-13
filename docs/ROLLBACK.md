# V4.3 回滚手册

## 1. 回滚前确认

```bash
cd "$HOME/ai-tv-butler"
docker compose -p ai-tv-butler ps
python3 -c "import adbtv;print(adbtv.__version__)"
```

## 2. 代码/镜像回滚（推荐，秒级）

```bash
# 使用部署时记录的 tag（示例，按实际时间替换）
docker image tag ai-tv-butler:pre-v43-<stamp> ai-tv-butler:4.3.0
docker compose -p ai-tv-butler up -d --no-build
docker compose -p ai-tv-butler ps
```

回滚后立即验证：

```bash
docker compose -p ai-tv-butler exec -T console ai-tv --version
curl -fsS http://<NAS_LAN_IP>:<CONSOLE_PORT>/health
docker compose -p ai-tv-butler exec -T console python3 -c \
  "from adbtv.assistant_loop import TOOL_SCHEMAS;print(len(TOOL_SCHEMAS))"
```

## 3. 配置回滚

v4.3 只新增 `voice.assistant.*` 字段（`enabled/max_steps/budget_seconds/remember_preferences/preferences_path`）。回滚配置：

```bash
cp "$HOME/ai-tv-butler-backups/pre-v43-<stamp>/config.yaml.private" deploy/data/config.yaml
docker compose -p ai-tv-butler restart console
```

不删 `voice.assistant` 也不会导致 v4.2 代码报错（未知键被忽略）。

## 4. 源码回滚

```bash
tar -xzf "$HOME/ai-tv-butler-backups/pre-v43-<stamp>/source-private.tar.gz" -C .
```

## 5. 数据说明

- **不需要**回滚 `/data`：v4.3 未做任何数据库迁移，`runtime/idempotency.sqlite3`、任务 JSON、`media_key`、会话文件格式均未变。
- 新增的 `runtime/voice_preferences.json` 仅在 `remember_preferences: true` 时出现，回滚后无影响，可保留或删除。
- **禁止** `docker compose down -v`：会删除命名卷，可能丢失持久化数据。

## 6. 回滚后的验收

1. 四容器 `healthy`，版本回到旧值。
2. `/api/voice` 正常返回。
3. 电视可打开应用、播放/暂停。
4. 旧镜像的幂等记录仍可读（SQLite 表结构未变）。
