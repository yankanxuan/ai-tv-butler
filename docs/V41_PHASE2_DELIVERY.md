# V4.1 Phase 2：按键语音与真实片名纠错闭环

日期：2026-11-12。版本仍为 **4.0.0b8**；本轮是 V4.1 Phase 2 实验增量，不是生产版本升级。现有 Agent、ADB、媒体服务器、Web 控制台、MCP、小爱语音和四容器均保留；小爱仍是生产默认 ASR。

## 1. 结论与证据边界

已完成可运行闭环：**浏览器按键录音 → 浏览器内转 16 kHz WAV → 独立 HTTPS 网关认证上传 → 独立 ASR 实验服务识别 → 真实 MediaIndex 作品级候选 → 季/集二级选择 → 用户明确确认 → 现有控制台 `/api/runs` → 电视实际播放并验证**。

实际证据：

- 用一条既有授权真人 WAV（文件回放，**不是手机麦克风**）走完上述闭环，真实电视播放《穿成野神我靠香火斩诸神》第 1 季第 1 集，任务 `SUCCEEDED`，播放器 `com.mitv.mivideoplayer` 状态 `PLAYING`，验证的媒体文件为 `穿成野神我靠香火斩诸神 S01E001.mp4`。见 [脱敏验收证据](evidence/v41_phase2_acceptance.json) 与 [页面截图](evidence/v41_phase2_portal_ui.png)。
- 用 Chromium **模拟麦克风**（`--use-fake-device-for-media-stream` + 授权 WAV）走网页录音、转换、上传、作品候选、11 季、80 集渲染、确认预览；**没有点击确认**，未执行电视操作。这证明前端路径，不是手机真人收音。
- **手机真人麦克风未验收**：本轮执行环境没有可操作的手机或真实采集设备（NAS 无 capture PCM，`arecord` 未安装）。不得把上述两条标成“手机麦克风通过”。
- 真实电视播放、暂停和取消延迟任务均为实机操作；取消测试中 `等20秒后打开飞牛` 被取消，25 秒后任务仍为 `CANCELLED`，电视包未切到飞牛。

## 2. 架构与安全边界

```text
手机/Windows 浏览器
  │ HTTPS + HttpOnly/SameSite Cookie + CSRF header
  ▼
独立 voice-portal（Phase 2 网关，标准库 HTTPS）
  │ 独立 ASR_LAB_TOKEN；WAV 大小/时长/格式校验
  ▼
独立 ai-tv-asr-lab 容器（默认关闭，2 CPU / 768 MiB）
  │ 固定 sherpa-onnx CTC 模型与哈希
  ▼
作品级 TitleSessionStore（真实 MediaIndex + RapidFuzz）
  │ 仅选择/确认，不执行
  ▼
现有控制台 <NAS_LAN_IP>:8771 /api/runs（用户控制台令牌）
  │
  ▼
现有 Agent / ADB / 媒体服务 → 小米电视
```

- 网关只监听 HTTPS（TLS 1.2+），登录使用现有控制台令牌并立即用 `/api/config` 验证；令牌只保存在网关内存会话，不写磁盘。会话 30 分钟过期，最多 64 个；Cookie 为 `HttpOnly; SameSite=Strict; Secure`。
- 所有状态变更还要求 `X-Portal-CSRF`，与 Cookie 分离，避免跨站请求。
- 网页必须先勾选“录音/导入已获授权”复选框；未勾选不会打开麦克风、读取文件或上传。上传必须 `Content-Type: audio/wav`、`X-ASR-Consent: true`；最多 2,000,000 B、60 秒、16 kHz 单声道 PCM16。网关和实验服务各验证一次；临时文件请求结束即删，不保存音频或识别全文日志。
- 每会话识别速率 30 次/60 秒；全局 ASR 一次只处理一条，忙时返回 409，不排队堆积。
- 候选快照 10 分钟过期；作品、季、集均属于该快照；确认后只能用一次。确认是唯一执行入口；取消会调用控制台取消并清空旧快照。**没有常开麦克风、自动唤醒或后台录音。**

## 3. 已实现源码

| 文件 | 作用 |
|---|---|
| `poc/voice_portal.py` | 独立 HTTPS 网关、登录/CSRF、WAV 上传、ASR 转发、作品/季/集 API、确认/取消/任务查询 |
| `poc/web/voice_portal/index.html` | 手机/Windows 响应式页面 |
| `poc/web/voice_portal/app.js` | `getUserMedia` + `MediaRecorder` + Web Audio 重采样/编码 WAV、状态/超时/重试、手动修正、季/集选择 |
| `poc/web/voice_portal/style.css` | 页面样式 |
| `poc/title_session.py` | 作品级分组、季/集、审核别名、上下文加权、过期、一次性确认 |
| `poc/asr_lab_server.py` | 新增认证 `/transcribe-upload`，支持浏览器 WAV；保留路径模式 |
| `poc/asr_extended_eval.py` | CER、意图、Top-1/Top-3、误建议、拒识/追问、P50/P95 扩展指标 |
| `deploy/voice-portal/Dockerfile` | 独立门户镜像；仅 RapidFuzz 依赖，哈希锁 |
| `docker-compose.voice-portal.yml` | 可选 profile、host 网络、只读挂载、资源限制 |
| `deploy/asr-lab/Dockerfile` / `docker-compose.asr-lab.yml` | Phase 2 实验镜像与默认关闭 profile |

新增测试：`test_title_session_v41.py`（23）、`test_voice_portal_v41.py`（15）、`test_asr_extended_eval_v41.py`（6）；实验服务测试增加上传边界。

## 4. 作品级片名纠错实际结果

真实宿主机媒体库 `/vol1/1000/影视` + `deploy/data/media_index.json`：

- 候选按 `(规范化标题, 年份)` 分作品，季不再占作品名额。
- 识别 `搜索穿成一神我靠香火斩诸神` → 真实作品 `穿成野神我靠香火斩诸神`，相似度 90.9091。
- 该作品实际为 **11 季、1305 集**（季 1–11，集数 86/88/105/103/118/103/104/102/107/109/280），而不是此前把多个季误合并为 1 个季。修复点是使用 `MediaEntry.season_hint`，并加回归 `test_season_hint_prevents_folder_season_collisions`。
- 作品级只显示 1 个候选；选择季 1 后显示 105 集，选择第 1 集后确认命令为 `播放《穿成野神我靠香火斩诸神》第1季第1集`。
- 原始 ASR hypothesis 保留；手动修正另存为 `edited_hypothesis`，不覆盖原识别。别名只能来自显式审核映射，目标必须是唯一真实作品；模型不能写永久别名。
- 当前播放内容作为上下文：若正在播放的作品与查询前缀匹配，会提升该作品排序；仍不自动选择或执行。

真实容器联调：生产索引单文件只读挂到 `/index/media-index.json`，媒体根只读挂到 `/media`，网关容器启动后返回上述 11 季候选，全部 `requires_confirmation=true`。

## 5. 扩大 ASR 评测：实际数据与缺口

现有 12 条授权真人录音（**单一说话人**，7 近场 / 3 远场 / 2 噪声，50.261 秒）补充了意图/片名期望元数据并重算：

| 模型 | CER | 意图准确率（可评估 10 条） | 片名 Top-1/Top-3（2 条） | 误建议率（10 控制句） | 拒识率 | P50/P95 识别 |
|---|---:|---:|---:|---:|---:|---:|
| CTC | **28.00%** | 0.60 | **1.00 / 1.00** | **0.00** | 0.833 | 0.94 / 1.74 s |
| Transducer | 58.67% | 0.30 | 0.50 / 0.50 | 0.00 | 0.833 | 4.68 / 7.02 s |
| Paraformer | **28.00%** | **0.70** | **1.00 / 1.00** | **0.00** | 0.833 | 6.84 / 9.56 s |

口径：

- 意图准确率只统计不依赖上下文的明确指令（10 条）；`选择第一个结果`、`确认播放第一集` 需要会话上下文，单独记为 `context_required=2`，**不计入成功或失败**。
- 片名 Top-1/Top-3 只有 2 条期望片名的样本，样本量不足以宣称泛化。
- 拒识率 = 标题解析器拒绝的比例；控制句被拒绝是正确行为，但该指标混合了标题和控制样本，不能当“安全率”。
- 误建议率 = 控制句中产生作品建议的比例；本轮 0，但只有 10 条。
- 延迟是文件回放，不含浏览器录音、上传、网关或电视执行。

**未完成的多说话人测试集：** 本机没有可操作真人/手机录音条件，不能伪造“多说话人”。已提供 `poc/corpus-v41-phase2.example.json` 元数据模板和评测器；阶段门槛仍是至少 3 位说话人、每条件 ≥50 句，当前数据不满足，不得据此替换小爱。Silero VAD 仍默认关闭；Phase 1 的短句数据没有证明改善，本轮不启用。

## 6. 性能、并发、视频同播与停机

环境：Intel Celeron 847，2 核/2 线程；实验容器限制 2 CPU / 768 MiB；CTC 模型、2 线程；文件回放；真实电视同时播放 4K《师兄太稳健》。

| 项目 | 实际结果 |
|---|---:|
| 连续识别 | **100/100 成功** |
| 总墙钟 | 132.69 s（第二次 100 次） |
| 单次 P50 / P95 / 最大墙钟 | **1.084 / 2.186 / 3.588 s**（第一次；含 HTTP） |
| ASR 本身 P50 / P95 | 1.021 / 2.104 s |
| 首个非空部分 P50 / P95 | 0.399 / 0.940 s（文件回放，非麦克风端到端） |
| 进程 CPU（第二次 100 次） | **206.71 CPU 秒 / 132.69 s 墙钟 = 两核 77.9%** |
| 单次推理平均 CPU | 1.93 s |
| cgroup 峰值内存 | **139,436,032 B（132.98 MiB）/ 768 MiB 限制** |
| 视频播放 | 100 次前 `PLAYING`，100 次后仍 `PLAYING`，播放器 `com.mitv.mivideoplayer` |
| 8 并发请求 | **1 成功 + 7 个 409 busy**，无并发共享识别器 |
| ASR 容器停止 | 网关仍 `ready`；上传返回 **504**；控制台 `/api/voice/status` 返回 200、`voice_beta=true`；电视仍播放 |
| ASR 重启 | 健康恢复；同一上传返回 200，识别“打开飞牛” |

说明：第一次 100 次与第二次 100 次分别用于延迟和 CPU/内存统计；两次均在视频播放中。CPU 秒包含解码器和进程开销，不是单次模型纯推理；峰值 RSS 指标与 cgroup 指标口径不同，不混用。ASR 服务停止期间没有向电视注入新的小爱语音，因此“小爱仍正常”只证明生产语音服务/控制台健康、电视未被实验影响，不是一次新的真实遥控器语音验收。

## 7. 实际设备验收结果

| 验收项 | 结果 | 证据边界 |
|---|---|---|
| 手机麦克风说“找穿成野神” | **未执行** | 无手机/真实麦克风；不得声称通过 |
| 已有授权 WAV 文件回放“搜索穿成…” | 通过 | 文件回放，非手机 |
| 少量错字检索真实候选 | 通过：`穿成一神…` → `穿成野神…`，90.9091 | 真实媒体库 |
| 网页展示候选作品 | 通过（Chromium 模拟麦克风） | 真实页面、真实 ASR、真实候选；麦克风为模拟 |
| 选择季/集 | 通过：11 季、80 集首屏渲染、季 1 第 1 集 | 页面未点击确认时 `executed=false` |
| 用户确认后才执行 | 通过：`/api/confirm` 是唯一执行入口 | 单元测试 + 实际文件回放确认 |
| 通过现有 Agent 播放 | 通过：任务 `SUCCEEDED`，播放目标 S01E001 | 真实小米电视 |
| 验证实际剧名/集数 | 通过：结果含 requested media path、S01E001、播放器 PLAYING | 真实设备状态 |
| 取消后不执行旧任务 | 通过：`等20秒后打开飞牛` → `CANCELLED`，25 s 后未打开飞牛 | 真实电视；取消入口实际调用控制台 |
| 电视最终状态 | `PAUSED`，未留下播放中的测试内容 | 实机暂停 |

## 8. Docker 启动与停止

### 8.1 ASR 实验服务（Phase 2 镜像）

```bash
cd "$HOME/ai-tv-butler"
export ASR_LAB_MODEL_DIR="$HOME/.cache/ai-tv-v41"
export ASR_LAB_AUDIO_DIR="/path/to/authorized/audio"
export ASR_LAB_UID="$(id -u)"
export ASR_LAB_GID="$(id -g)"
export ASR_LAB_PORT=8765
export ASR_LAB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  --profile asr-lab up -d --build
curl -fsS http://127.0.0.1:8765/health
```

固定镜像标签 `ai-tv-asr-lab:v4.1-phase2`；基础镜像 `python:3.11-slim@sha256:07d482…`；运行时依赖仍由 `poc/requirements-v41.lock` 哈希锁定。默认关闭；无 `--profile asr-lab` 不启动。

### 8.2 可选片名索引 overlay

```bash
export ASR_LAB_TITLE_INDEX="$HOME/ai-tv-butler/deploy/data/media_index.json"
export ASR_LAB_MEDIA_ROOT="/vol1/1000/影视"
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  -f deploy/asr-lab/title-index.override.example.yml \
  --profile asr-lab up -d --build
```

只读挂载索引单文件与媒体根；不挂生产 `/data`、不读取生产 token。

### 8.3 HTTPS 语音网关（Phase 2，默认关闭）

生成自签名证书（或换成受信任证书；手机需信任该证书，否则浏览器只允许在用户接受风险后使用麦克风）：

```bash
mkdir -p "$HOME/.cache/ai-tv-v41/tls"
openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
  -subj '/CN=AI-TV-Butler Voice Portal' \
  -addext 'subjectAltName=IP:<NAS_LAN_IP>,IP:127.0.0.1,DNS:localhost' \
  -keyout "$HOME/.cache/ai-tv-v41/tls/key.pem" \
  -out "$HOME/.cache/ai-tv-v41/tls/cert.pem"
chmod 600 "$HOME/.cache/ai-tv-v41/tls/key.pem"
```

使用 Docker（独立 project，host 网络仅用于访问只监听本机/LAN 的控制台和回环 ASR）：

```bash
export ASR_LAB_TOKEN="$(cat "$HOME/.cache/ai-tv-v41/lab.token")"
export ASR_LAB_URL="http://127.0.0.1:8765"
export CONSOLE_URL="http://<NAS_LAN_IP>:<CONSOLE_PORT>"   # 本安装实际为 8771
export VOICE_PORTAL_INDEX="$HOME/ai-tv-butler/deploy/data/media_index.json"
export VOICE_PORTAL_MEDIA_ROOT="/vol1/1000/影视"
export VOICE_PORTAL_CERT_DIR="$HOME/.cache/ai-tv-v41/tls"
export VOICE_PORTAL_UID="$(id -u)"
export VOICE_PORTAL_GID="$(id -g)"

docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml \
  --profile voice-portal up -d --build
```

访问 `https://<NAS_LAN_IP>:8443`。控制台端口必须从实际部署配置读取（`deploy/data/config.yaml` 的 `server.port`；本机容器实际为 8771，而不是示例配置中的 8770）。

也可直接运行网关（本机已实测）：

```bash
ASR_LAB_TOKEN_FILE="$HOME/.cache/ai-tv-v41/lab.token" \
python3 -m poc.voice_portal \
  --index deploy/data/media_index.json \
  --path-map '{"\/media":"\/vol1\/1000\/影视"}' \
  --lab-url http://127.0.0.1:8765 \
  --console-url http://<NAS_LAN_IP>:<CONSOLE_PORT> \
  --host 0.0.0.0 --port 8443 \
  --cert "$HOME/.cache/ai-tv-v41/tls/cert.pem" \
  --key "$HOME/.cache/ai-tv-v41/tls/key.pem"
```

停止：

```bash
docker compose -p ai-tv-voice-portal -f docker-compose.voice-portal.yml --profile voice-portal down
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down
```

这不会停止或修改 `ai-tv-butler` 生产四容器。

## 9. 许可证清单（本轮增量）

| 组件 | 许可 | 使用方式 |
|---|---|---|
| sherpa-onnx 2.0.1 / core | Apache-2.0 | Phase 1 已固定；本轮实验镜像复用 |
| CTC / Transducer / Paraformer 权重 | Apache-2.0 上游声明（CTC 保留源 checkpoint 证据链） | 固定 revision/hash；不随生产主镜像 |
| Silero VAD v5.1.2 | MIT | 默认关闭；本轮未启用 |
| RapidFuzz 3.14.3 | MIT | 网关与实验环境；`requirements-portal.lock` 哈希锁定 |
| Python 3.11-slim 基础镜像 | PSF/其发行条款 | 固定 digest |
| 浏览器 Web Audio / MediaRecorder / getUserMedia | 浏览器标准 API | 不引入第三方 JS 库 |
| ffmpeg（可选已有工具） | 按发行版许可 | 本轮网页转换使用浏览器；未新增依赖 |

完整许可证文本/哈希证据在 `poc/licenses/v41/`、`poc/models-v41.lock.json`、`poc/requirements-v41.lock`、`poc/requirements-portal.lock` 和 `THIRD_PARTY_NOTICES.md`。

## 10. 回滚

1. 停止网关和实验服务：上面两条 `down`。
2. 删除实验镜像：`docker image rm ai-tv-voice-portal:v4.1-phase2 ai-tv-asr-lab:v4.1-phase2`。
3. 删除自签名证书、实验 token、缓存模型/音频由操作者自行决定；不会删除用户媒体库。
4. 生产四个容器、`adbtv/`、主 Dockerfile、主 Compose、runtime lock 和小爱默认均未改；无需回滚生产。
5. 若曾通过网关播放，电视保持最后状态；本轮已暂停测试视频。需要时用现有控制台“暂停播放”。

## 11. 未完成 / 下一步

- **真实手机麦克风**、真实多说话人语料（≥3 人、每条件 ≥50 句）、长句/多步骤覆盖未完成；当前只有单说话人 12 条和 1 条长句模板。
- 手机访问自签名 HTTPS 会有证书警告；正式环境应换受信任证书或使用受管理的反向代理。
- 控制台端口/绑定地址随部署配置变化；网关需显式配置，不能硬编码 8770。
- 真实断网恢复、长时间运行（24h）、ARM64、电视对白回声、常开 VAD/唤醒未验收；VAD 仍默认关闭。
- 多用户并发已测 8 请求和双会话隔离；更大规模、跨设备并发仍待测。
- 未做同音频小爱 A/B；小爱结果仍只能来自真实遥控器 logcat。

## 12. 复现命令

```bash
# 单元与集成回归（使用包含可选依赖的隔离测试环境）
/tmp/osfv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
/tmp/osfv/bin/python tests/test_suite.py

# Phase 2 扩展指标
/tmp/osfv/bin/python -m poc.asr_extended_eval \
  --report /path/to/model-1.json --report /path/to/model-2.json --report /path/to/model-3.json \
  --samples /path/to/authorized/samples-phase2.json \
  --index deploy/data/media_index.json --path-map '{"\/media":"\/vol1\/1000\/影视"}' \
  --output /path/to/new/phase2-metrics.json

# 作品级候选（真实库只读）
/tmp/osfv/bin/python -m poc.title_session --index deploy/data/media_index.json \
  --path-map '{"\/media":"\/vol1\/1000\/影视"}' --text '搜索穿成一神我靠香火斩诸神'
```

完整测试数量见 [Phase 2 验证摘要](evidence/v41_phase2_validation.json)：**815 项单元测试通过、0 跳过；22 项模拟电视集成通过；公开包审计通过（210 个文件）**。

最终实验镜像（Phase 2 时点；Final3 已用修复源码重建，见 [Final3 报告](V41_FINAL3_DELIVERY.md)）：

| 镜像 | ID | 大小 |
|---|---|---:|
| `ai-tv-asr-lab:v4.1-phase2` | `sha256:2d75aa00…` | 182,962,042 B |
| `ai-tv-voice-portal:v4.1-phase2` | `sha256:7ef9fe73…` | 137,608,947 B |

本轮验收结束后两个实验服务均已停止，8443/8765 无监听；生产四容器仍 healthy、镜像仍为 `sha256:0fcb7301…`。