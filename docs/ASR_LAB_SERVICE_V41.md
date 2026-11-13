# V4.1 第一阶段：独立 Docker ASR 实验服务与按键录音

本功能默认关闭，仅用于显式授权音频的离线识别和片名候选建议。Phase 2 增加认证 WAV 上传接口与独立 HTTPS 语音网关；实验镜像标签更新为 `ai-tv-asr-lab:v4.1-phase2`。小爱保持生产默认。实验服务没有 Agent、ADB、播放接口或生产状态探测，不读取生产配置、console token 或媒体密钥。未修改主 Dockerfile、主 Compose 或 `adbtv` 代码。

## 服务契约

- 标准库 `ThreadingHTTPServer`；宿主 CLI 默认只监听 `127.0.0.1`。容器内监听自己的桥接网络地址，宿主仅发布 `127.0.0.1:8765`。
- 固定启动模型：启动时同步构造 `poc.asr_lab.Engine(manifest_path, num_threads=2, vad=False)`，由 Engine 校验固定注册表、模型 hash 和许可证据。只有模型真实加载成功才绑定服务并返回 ready；加载失败退出 2，没有可误报成功的 health。
- `GET /health`：仅返回 `service=asr-lab`、`ready=true` 及离线处理标记；不报告生产是否健康，不泄露 manifest、token 或个人路径。
- `POST /transcribe`：必须有 `Authorization: Bearer <独立实验token>`，请求只允许 `{"path":"relative.wav","consent":true}`。不能按请求替换 manifest、模型、线程数或生产配置。
- `POST /transcribe-upload`：同样的独立 Bearer 鉴权；`Content-Type: audio/wav`、`X-ASR-Consent: true`，请求体为最多 2,000,000 B、60 秒、16 kHz 单声道 PCM16 WAV；`X-Capture-Kind` 只能为 `uploaded_offline_wav`、`push_to_talk_microphone_then_offline` 或 `offline_wav_input`。原始音频写入请求临时目录并在响应前删除。Phase 2 网关使用此接口，不需要共享可写音频目录。
- 路径必须严格位于只读 `audio_root` 下，拒绝绝对路径、`..`、空/点路径段、反斜杠、非 WAV。使用 `dir_fd`/`O_NOFOLLOW` 逐级打开，拒绝文件及父目录符号链接、设备和 FIFO；复制到私有临时快照后再识别，避免校验后路径被替换。
- 输入默认最多 **2,000,000 字节、960,000 帧（60秒）**，只接受完整 16 kHz / 单声道 / PCM16 WAV。请求 JSON 最多 8192 字节，拒绝 chunked 请求。模型引擎自身的 60 秒限制也保留。
- 一次只识别一条；并发请求返回 HTTP 409 `busy`，不排队、不并发共享识别器。鉴权失败 401、无明确 consent 403、音频边界/格式错误 400、请求体过大 413、识别异常 500。异常响应不回显模型错误或私有路径。
- 返回 `asr`（Engine 的真实 hypothesis/计时等）、`suggestions` 和 `capture_kind=offline_file_transcription`。未显式提供片名缓存时 suggestions 为 disabled/空候选；原始识别不被候选建议改写，更不执行播放。

## 独立 Docker 启动

只使用独立文件和项目名，**不要把实验文件与主 Compose 合并**。无 `--profile asr-lab` 不启用服务；平时无需启动。当前锁仅覆盖 CPython 3.11 / Linux x86_64，ARM 未验收。

基镜像固定为 `python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`。可显式设置 `ASR_LAB_BASE_IMAGE` 覆盖，但仍须使用经过核验的 digest 并记录。依赖引用 `poc/requirements-v41.lock`，使用 `--only-binary=:all: --require-hashes`；不将生产运行时锁或整个宿主目录装入实验镜像。构建需要网络，运行时服务本身不下载模型。

> 实测修正（2026-11-12）：最初使用 `internal: true` 网络时，宿主无法通过已发布端口访问（连接被拒）；Docker 限制已发布端口只在非 internal 网络工作。现改为独立 bridge、仅发布 `127.0.0.1`，**不声称容器无外网出口**。服务没有联网代码路径，模型和音频全部只读挂载。

先明确选择独立实验目录，准备固定模型及实验 manifest；模型准备命令会显式下载注册表中已核验工件，服务启动不会隐式下载：

```bash
cd "$HOME/ai-tv-butler"
export ASR_LAB_MODEL_DIR="$HOME/.cache/ai-tv-v41/models"
export ASR_LAB_AUDIO_DIR="$HOME/.cache/ai-tv-v41/authorized-audio"
# 使用安装了 poc/requirements-v41.lock 的独立评测解释器：
python -m poc.asr_lab prepare --profile ctc --cache "$ASR_LAB_MODEL_DIR"
# 将 prepare 生成的 ctc.json 复制为服务固定读取的 model.json；保留其中相对 model_dir。
cp -n "$ASR_LAB_MODEL_DIR/ctc.json" "$ASR_LAB_MODEL_DIR/model.json"
mkdir -p "$ASR_LAB_AUDIO_DIR"
```

model.json 与模型目录必须可由容器指定的非 root UID/GID 读取；不要通过运行 root、privileged、放开整个生产数据卷解决权限问题。默认 UID/GID 10001，可将 `ASR_LAB_UID` / `ASR_LAB_GID` 设为已有文件拥有者的非 root ID，不要求更改生产文件权限。

创建一个**独立** token，仅从实验环境变量或明确的实验文件读取。环境变量方式示例（不写入版本库或生产 `.env`）：

```bash
export ASR_LAB_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  --profile asr-lab config --quiet
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  --profile asr-lab up -d --build
curl --fail http://127.0.0.1:8765/health
```

实验 Compose 是非 root、read-only 根文件系统、丢弃 capabilities、no-new-privileges、独立 bridge（仅回环发布）、**2 CPU / 768 MiB / 64 PID** 限制；仅模型目录与授权音频目录只读挂载。临时目录 32 MiB tmpfs，用于每请求临时快照；请求结束即清理。没有 host network、privileged、Docker socket、生产 `/data` 或麦克风设备映射。容器不自动重启，避免隐藏持续失败。

调用服务时路径相对于 `ASR_LAB_AUDIO_DIR`；录音必须已经由操作者明确授权并放在该目录：

```bash
curl --fail http://127.0.0.1:8765/transcribe \
  -H "Authorization: Bearer $ASR_LAB_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"path":"authorized-take.wav","consent":true}'
```

以上路径为操作示例，不表示文件已存在或录音已验收。HTTP 不提供上传接口，不能浏览其他目录。响应中的文本可能属于个人语音内容，调用方自行决定是否保存，服务默认不记逐请求/原文日志。Docker 本地日志仅保留有限的启动信息，token 不写日志。

停止和移除实验容器：

```bash
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down
unset ASR_LAB_TOKEN
```

不会操作主项目容器、生产卷或小爱入口。音频与模型仍在操作者指定目录；本工具不自动删除用户文件。

## 本机服务与可选候选缓存

```bash
ASR_LAB_TOKEN_FILE=/path/to/independent-experiment-token \
python -m poc.asr_lab_server \
  --manifest /path/to/experiment/model.json \
  --audio-root /path/to/authorized-audio \
  --host 127.0.0.1 --port 8765
```

token 文件只按显式配置读取，要求普通文件、非链接；token 为至少32字符的独立 ASCII 值。不能把 `console.token` / `media_key` 传给实验。环境变量 `ASR_LAB_TOKEN` 与实验 token 文件互斥。

需要候选建议时显式添加 `--title-cache /path/to/experimental-title-cache.json`，调用 `TitleSuggester.from_cache(...).suggest(hypothesis)`。不默认读取生产索引或扫描媒体目录。候选器可能要求索引所指实际媒体文件存在且可读；容器需要另行显式只读挂载相同实验媒体路径并覆盖 command 增加该参数，不能把“索引加载”当成候选文件已经核验。默认 Compose 不挂载生产媒体，不自动启用候选纠错。

可选的 [title-index overlay](../deploy/asr-lab/title-index.override.example.yml) 只在明确选择时启用：

```bash
export ASR_LAB_TITLE_INDEX=/path/to/reviewed-existing-media-index.json
export ASR_LAB_MEDIA_ROOT=/path/to/actual-media-root
# 索引内媒体路径须与容器的 /media 一致；只读索引不会被服务重写。
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  -f deploy/asr-lab/title-index.override.example.yml \
  --profile asr-lab config --quiet
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  -f deploy/asr-lab/title-index.override.example.yml \
  --profile asr-lab up -d --build
```

此 overlay 仅追加现有索引单文件 `/index/media-index.json` 与媒体根 `/media` 两个只读挂载，`create_host_path=false`，保留基础模型/音频挂载及资源限制；不挂整个生产 data、不读取生产 token。缓存中的文件路径不存在、超出已挂载根或不可读时，不能冒充已核验候选。不用 overlay 时没有这两个挂载，也不启用缓存。

若要试 VAD，必须准备注册表固定且许可核验过的 VAD 工件并在实验 manifest 配置 `vad_dir`，再显式传 `--vad`。默认关闭；VAD 不代表唤醒、授权或远场识别通过。

## 按键录音工具

`poc/push_to_talk.py` 复用成熟已有 `arecord` / `ffmpeg`，不引入 sounddevice、PyAudio 或 Torch。Linux ALSA 是当前麦克风入口；不保证 NAS 已有可用麦克风。需要先自行确认声卡及输入设备，工具找不到录音器、设备打开失败、无有效 WAV 时退出 2，明确不声称麦克风验收通过。

每次必须指定 `--consent`、真实参考文本 `--reference` 和新的 `--output` WAV 位置。不给 `--seconds` 时 Enter 开始、Enter 停止；给秒数时录固定时长，默认最大60秒（arecord 的小数秒向上取整）。原始采集只存在临时目录，随后 ffmpeg 显式转换为16 kHz 单声道 PCM16；用户指定 WAV 和其旁的样本清单使用0600保存，拒绝覆盖已有目标。

```bash
# Enter 开始 / Enter 停止；reference 必须写实际说出的内容。
python -m poc.push_to_talk --consent --reference '打开飞牛' \
  --recorder arecord --device default \
  --output "$ASR_LAB_AUDIO_DIR/take-001.wav"

# 固定 5 秒：
python -m poc.push_to_talk --consent --reference '暂停播放' --seconds 5 \
  --output "$ASR_LAB_AUDIO_DIR/take-002.wav"
```

生成兼容既有评测的 `schema_version=1`、samples、origin、license、authorized、permission_evidence、reference、sha256、condition、proper_nouns 等字段。可用 `--condition far-field` / `noise` 和重复 `--proper-noun` 标注实际场景；不能靠手动标签声称某种距离/噪声已验收。

麦克风录制路径始终标记 **`capture_kind=push_to_talk_microphone_then_offline`**：先录音落盘、再整文件识别，**不是实时流式 ASR**。录音时长、离线推理首 partial 和真实麦克风到结果的延迟不能混用。

也可显式输入已有 WAV 做离线转换：

```bash
python -m poc.push_to_talk --consent --reference '实际参考文本' \
  --input-wav /path/to/authorized-existing.wav \
  --output "$ASR_LAB_AUDIO_DIR/imported-001.wav"
```

此模式标记 `capture_kind=offline_wav_input`，不打开麦克风、不算麦克风验收。已有录音的正式模型对照由独立评测 runner 完成，本工具不将回放伪装成新真人采集。

可加 `--server-url http://127.0.0.1:8765 --server-path take-001.wav` 在成功保存后调用实验服务；客户端只允许 localhost HTTP，使用同一独立实验 token 环境变量或 `--token-file`。默认不发送任何请求，不上传原音频；服务按只读挂载中的相对路径读取。CLI 只向标准输出显示 manifest 路径和可选识别响应，不额外生成原文日志。

## 实际容器验收（2026-11-12）

- 镜像 `ai-tv-asr-lab:v4.1-experiment` 实际构建成功，大小 182,869,388 B，amd64；基镜像 digest 与依赖哈希锁生效。
- 容器实际启动，非 root、read-only 根、2 CPU/768 MiB、无 privileged，模型/音频/索引/媒体根均只读；健康检查真实通过。
- 授权真人录音 `human-02.wav` 经 HTTP 离线识别返回 200，并给出真实片库 5 个同名季候选，状态 `needs_clarification / ambiguous_seasons`，全部需确认；未授权 401、路径穿越 400。
- 测试结束后容器已 down，无遗留实验容器；生产四容器未变。
- 实测修正两项：Docker `local` 日志驱动在默认压缩下要求 `max-file > 1`（`1` 会导致容器无法启动，现为 2）；工作区源文件权限 0600，镜像内只对 `/app` 执行 `a+rX`，不改宿主文件。

详见 [离线 ASR 比较与容器报告](ASR_COMPARISON_V41.md) 与 [脱敏容器证据](evidence/v41_phase1_docker.json)。

## 本子任务验证与限制

定向命令：

```bash
python3 -m unittest discover -s tests -p test_asr_lab_service_v41.py -v
```

已通过32项模拟回归（含认证上传、坏 WAV、大小/格式/采集标签、401/403/415/413）：固定模型启动/失败不ready、Bearer、consent、无请求级模型配置、路径穿越/符号链接/FIFO、格式/字节/帧限制、临时快照清理、busy拒绝/失败释放、异常脱敏、独立token来源、录音器命令/停止、无声卡失败、离线与麦克风标记、manifest兼容/0600以及独立Docker配置约束。

这些测试使用 mock Engine、mock subprocess 和临时生成的合成 WAV；不下载模型、不打开真实麦克风、不操纵电视，不作为中文准确率或麦克风端到端验收。本文件不把配置静态检查当成容器已运行；实际构建、启动、HTTP 识别和候选已在上一节记录；Phase 2 的认证上传与网关见 [Phase 2 交付报告](V41_PHASE2_DELIVERY.md)。真实麦克风仍未验收：本机无 capture PCM 设备且 `arecord` 未安装；已测的是授权 WAV 离线导入，不是麦克风通过。
