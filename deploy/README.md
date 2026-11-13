# P0/P1 部署补充

数据目录优先级：`AI_TV_DATA_DIR` > `paths.data_dir` > 顶层 `data_dir`。普通可写源码安装默认保留项目 `state` 和 `logs`；wheel 或只读源码默认使用 `$XDG_DATA_HOME/ai-tv-butler`（未设置则 `~/.local/share/ai-tv-butler`）。`execution.state_dir` 显式覆盖运行时目录，其他特性路径也可以显式覆盖。配置只读时使用独立可写配置文件；显式 `--config` 或 `AI_TV_CONFIG` 文件不存在会报错。

外部数据目录的默认结构：`runtime/`（任务和跨进程锁）、`runtime/console.token`、`tasks.json`、`memory.sqlite3`、`logs/`、`.android/`。显式相对特性路径在外部数据模式相对数据根解析；本机兼容模式仍相对项目根。部署模板省略这些覆盖以使用统一默认。`execution.state_dir` 改动会同时改变默认 token 位置，默认 memory 位于 runtime 父目录。

## Docker Compose

在项目根目录运行：

```sh
python3 scripts/setup_deploy.py --data-dir deploy/data
# 将输出的 BUTLER_UID / BUTLER_GID / BUTLER_DATA_DIR 写入本机 .env
# 修改 deploy/data/config.yaml 的设备、模型和服务地址后：
docker compose build
docker compose up -d console scheduler
docker compose ps
docker compose logs --tail=100 console scheduler
docker compose stop console scheduler
```

setup 只创建当前用户拥有且有写权限的目录，不调用 sudo/chown，不更改已有配置。默认镜像用户是 10001:10001；宿主 bind mount 不会被 Dockerfile 的 chown 修复，因此必须将 `.env` 中 UID/GID 设为目录拥有者，或由管理员提前准备对应目录。非 root UID 才满足无特权部署目标，root 用户应选择普通账户执行 setup。两个容器都需要 `/data` 可写，根文件系统保持只读；`HOME=/data` 使 ADB 的 `.android` 密钥持久。不要给两个实例设置不同的 execution.state_dir，否则它们不会共享设备锁。

console 健康探针读取实际 `server.host` 与 `server.port`；通配监听地址映射为本机回环探测。host 网络模式不发布端口，端口占用由服务报错；不要终止其他进程。scheduler 独立运行，由容器重启策略监控进程退出。实际到期执行和心跳需在部署环境验收。

## systemd user

```sh
python3 scripts/setup_deploy.py --data-dir "$HOME/.local/share/ai-tv-butler"
mkdir -p "$HOME/.config/systemd/user"
cp scripts/ai-tv-butler.service scripts/ai-tv-butler-scheduler.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ai-tv-butler.service ai-tv-butler-scheduler.service
systemctl --user status ai-tv-butler.service ai-tv-butler-scheduler.service
# 停止两者：
systemctl --user stop ai-tv-butler.service ai-tv-butler-scheduler.service
```

模板假定源码位于 `%h/ai-tv-butler`，使用 `/usr/bin/python3`。虚拟环境安装者应在两个 unit 的 ExecStart 一并替换为虚拟环境 Python。两个 unit 使用同一数据目录与配置；HOME 被设为数据目录以持久化 ADB 密钥。

## 迁移、备份、恢复

先停止 console/scheduler 并备份原配置、完整 state、logs 和 `~/.android`（包括隐藏文件），再在新数据目录复制 `state` 的内容，将旧 logs 复制到新目录的 logs，按需复制 ADB 密钥至 `.android`。保留原目录和私有权限；不要在未确认新实例正常前删除旧数据。原配置中的显式 `state/tasks.json`、`logs` 等路径应审核后改为绝对目标路径或移除以采用统一默认。此工具不自动修改用户 config.yaml。

重启后确认任务、令牌、记忆和调度存在；再验证一个模拟到期任务。恢复时停止两个服务，恢复整份数据目录及配置，再用匹配版本启动。SQLite 在线备份使用现有 memory backup 命令；文件级复制 SQLite 应先停止写入。`make clean` 只清 Python 字节码，永不清任务、数据库、锁、令牌或日志历史。

内置模拟器同时支持 `python -m adbtv.simulator`、`ai-tv-simulator`，以及 `python /安装目录/adbtv/simulator.py`。通过 `FAKE_ADB_STATE=/可写目录/state.json` 指定隔离状态，wheel 不依赖 tests 目录。不要将模拟结果记为真机验收。

Docker 镜像未实际构建和启动时只能标记“待容器验收”；systemd 服务未实际安装启动时也应保留该验收项。


## V3.4.2：media 服务与单实例约束

```bash
docker compose up -d      # console + scheduler + media + voice
docker compose ps         # 四个服务都应为 healthy
```

- `media` 是唯一持有媒体端口的进程（`command: [media, serve]`）。console/scheduler/voice 只签发播放链接，**不要再启动第二个媒体服务**：第二个实例会以 EADDRINUSE 退出（退出码 2）。
- 四个服务都只读挂载媒体库到 `/media`（`MEDIA_ROOT`）：console 建索引、scheduler/voice 跑任务、media 读字节。**只给 media 挂载会导致 Agent 看到空索引并退化成 UI 搜索**。
- 四个服务共享 `/data`：配置、ADB 授权（`.android`）、设备锁（`runtime/locks`）、媒体签名密钥（`media_key`）与会话（`media_sessions.json`）。
- 健康检查：`healthcheck.py console|scheduler|media|voice`；`voice` 会分别报告监听进程、ADB 连接与日志新鲜度。
- Docker 不会因 unhealthy 自动重启，需配合 autoheal 或人工 `docker compose restart <svc>`。

## V3.4.1：voice 服务

Compose 现在有三个服务，共享同一 `/data`（配置、ADB 授权 `.android`、设备锁、去重状态）：

```bash
docker compose up -d          # console + scheduler + voice
docker compose ps             # 三个都要 healthy
```

- `voice` 以 `voice listen --force` 启动：**启动该容器即表示启用语音监听**，不依赖配置文件开关。
- 健康检查读 `runtime/voice_worker.json` 心跳（新鲜度判定），不依赖容器间 PID 可见性。
- `restart: unless-stopped` 负责崩溃重启；心跳持续为 `stale` 时容器显示 unhealthy，需要人工 `docker compose restart voice`——Docker 不会因 unhealthy 自动重启，除非另配 autoheal。
- 语音监听需要持续 ADB 连接；电视休眠或网络抖动时进程内部按指数退避重连。
- 三个服务对同一台电视的操作用同一把 flock 串行，不会互相抢设备。


## 语音回执（TTS）与 Piper

`piper-tts` 是 **GPL-3.0-or-later**，默认**不**装进镜像（避免把 copyleft 组件随镜像分发）。
需要语音回执时显式构建：`INSTALL_PIPER=1 docker compose build`，并自行确认 GPL 义务。
模型从共享数据目录读取：

```yaml
voice:
  tts:
    enabled: true
    model: /data/piper/zh_CN-huayan-medium.onnx
    audio_base_url: "http://<NAS内网IP>:8770"
```

把 `zh_CN-huayan-medium.onnx` 与同名 `.json` 放到 `deploy/data/piper/`（容器内即 `/data/piper`）。
模型缺失或 piper 不可执行时，TTS 任务明确进入 `FAILED`，不会假装播报；电视端扬声器仍不可用，
音频只经控制台 `/api/voice/audio/<name>` 下发到浏览器/手机。
