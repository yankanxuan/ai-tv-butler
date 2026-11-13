# V4.1 第一阶段：离线 ASR 比较与真实片名候选报告

日期：2026-11-12（Asia/Shanghai）。本报告只记录**实际执行**的离线文件回放与隔离容器测试，**不是**真人麦克风实时流式验收，也不改变生产默认 ASR。小爱仍为默认；现有 console / scheduler / media / voice 四容器未修改、未重启、未替换。

## 1. 结论

1. 同一批 12 条既有授权真人普通话录音上，**当前 CTC 仍是综合最稳的 CPU 基线**：总 CER 28.0%、指令召回 7/12、RTF 0.265、单模型进程峰值 RSS 124.7 MiB。
2. **流式 Transducer**（中英双语 int8，198.2 MB）在本机双核 Celeron 847 上明显退化：总 CER 58.7%、召回 4/12、RTF 1.28、RSS 281.7 MiB。其 greedy 输出有大量插入；本轮**没有**启用官方仅支持 transducer + `modified_beam_search` 的热词，因此不能据此评价热词效果。
3. **Paraformer**（FunASR 在线 Paraformer 的 sherpa-onnx int8 导出）在近场/远场更准（CER 6.5% / 4.8%，召回 10/12），但噪声插入更严重（CER 212.5%）、RTF 1.66、RSS 314.0 MiB。实时性不合格，且噪声输出不可直接进入执行层。
4. 加入 **Silero VAD v5.1.2** 做切句后再解码：CTC 总体 CER 反而升到 32.0%（7 个删除），Paraformer 为 26.7% 但噪声仍 150%，Transducer 为 76.0%。本批录音都是短句，VAD 的 0.5 s 静音切分与 0.25 s 最短语音门槛并未带稳定收益；**不据此启用 VAD**，需要真实长句/端点数据再调。
5. Docker 实验镜像**实际构建并启动**，通过授权真人录音的 HTTP 离线识别与真实片库候选建议；未授权 401、路径穿越 400、始终 `requires_confirmation=true`，未调用任何生产播放/Agent。测试后实验容器已移除。
6. 真实**按键麦克风**未验收：本机 `/dev/snd` 只有回放/控制/sequencer，没有采集设备，`arecord` 也未安装。PTT 工具只实测了「已有授权 WAV → 16 kHz PCM16 → 授权样本清单」的离线导入路径，未把它标成麦克风通过。

## 2. 模型、权重与许可（固定）

完整机器可读登记在 `poc/models-v41.lock.json`，证据原文在 `poc/licenses/v41/`。代码许可与权重许可是分开的。

| Profile | 实际来源与 revision | 工件（SHA-256 / 字节） | 权重许可证据 |
|---|---|---|---|
| `ctc` | `csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01` @ `a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da` | model.int8.onnx `68c9c943…` / 26,342,340；tokens `6fed8c6c…` / 13,366 | ONNX 卡未独立声明；其源 checkpoint 卡明确 Apache-2.0。保留派生证据链 |
| `transducer` | `csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` @ `98590b7ed6443e77b714204da2757d75e1a642f4` | encoder 181,895,032；decoder 13,091,040；joiner 3,228,404；tokens 56,317 | 固定模型卡 `license: apache-2.0`；SHA 与既有审计一致 |
| `paraformer` | `csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en` @ `8e40c43232a1c5c66c82111efc5820d3accca11b` | encoder.int8 165,462,184 `81a70226…`；decoder.int8 71,664,561 `f3cca9f7…`；tokens 75,756 `59aba887…` | 固定 ONNX 卡 `license: apache-2.0`，转换自 `damo/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8404-online` |
| Silero VAD | `snakers4/silero-vad` @ `6478567951ae5c9979ad7b234185b5515f4be7a1`（v5.1.2 tag） | `silero_vad.onnx` 2,327,524 `2623a295…` | 仓库 LICENSE 为 MIT；Git blob 亦核对 |

运行时固定：CPython 3.11、sherpa-onnx **2.0.1** + sherpa-onnx-core 2.0.1、JiWER 4.0.0、RapidFuzz 3.14.3、Click 8.1.8；`poc/requirements-v41.lock` 对 5 个 wheel 做 `--require-hashes`。新环境用本地 wheel 离线安装成功、`pip check` 无冲突。

**边界：** FunASR 本轮使用其官方在线 Paraformer 权重的 sherpa-onnx CPU 导出推理，**不是** FunASR Python/PyTorch 工具包本身；两者的内存和延迟不可互换引用。Paraformer 来源权重卡的 Apache-2.0 是上游声明，不代替训练数据权利审计。

## 3. 方法与硬件

- 硬件：Intel Celeron 847 @ 1.10 GHz，2 核/2 线程，x86_64；现有 NAS 服务同时运行。
- 同一 `human/samples.json`：7 条近场、3 条约 3 米远场、2 条噪声；总计 50.261 s；12 条音频 SHA-256 与样本清单哈希在报告中逐条保留。
- 每个模型**独立子进程**、2 CPU 线程、greedy 解码、200 ms 音频块 + 660 ms 右上下文补齐；先跑无 VAD，再用同一流式接口加 Silero VAD。
- CER：JiWER `process_characters` 最短 Levenshtein；NFKC、去标点和空白；整数 Σ(S+D+I)/Σ参考字符，可 >100%。
- 计时：文件**无实时节拍**，`first_partial_seconds` 是计算时间，不等于麦克风/端点延迟；CPU 是推理区间进程时间（不含加载/读文件）；RSS 是单模型进程生命周期峰值。
- 候选建议：真实 `deploy/data/media_index.json` + 只读 `/media` 映射进实验容器；只建议、不改写原识别、不自动选择/播放。

## 4. 无 VAD 实测

| 模型 | 近场 CER（S/D/I） | 远场 CER（S/D/I） | 噪声 CER（S/D/I） | 总 CER | 指令召回 | 专名 | RTF | 推理 CPU | RSS | 初始化 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CTC | 6.52%（3/0/0） | 19.05%（3/1/0） | 175.0%（1/0/13） | **28.0%** | **7/12** | 3/5 | **0.265** | 21.89 s | 124.7 MiB | 4.30 s |
| Transducer | 45.65%（4/0/17） | 85.71%（2/0/16） | 62.5%（0/0/5） | 58.67% | 4/12 | 3/5 | 1.275 | 96.68 s | 281.7 MiB | 7.32 s |
| Paraformer | **6.52%（3/0/0）** | **4.76%（0/1/0）** | 212.5%（0/0/17） | 28.0% | **10/12** | 3/5 | 1.658 | 111.89 s | 314.0 MiB | 6.93 s |

首个非空部分结果的中位/最大（文件回放）：CTC 0.381/1.176 s；Transducer 1.997/2.570 s；Paraformer 2.389/5.725 s。这些不是实时延迟。

观察：CTC 与 Paraformer 总 CER 同为 28.0%，但组成不同（CTC 7S/1D/13I，Paraformer 3S/1D/17I）；Paraformer 的插入更集中，噪声两条超过 200%。专名命中三者都是 3/5，没有明显改善“穿成野神”类专名。

## 5. 加 Silero VAD 实测

同一模型接口、同一音频，在流式识别前用 Silero VAD v5.1.2（threshold 0.5、最短语音 0.25 s、静音 0.5 s、最长 20 s、512 样本窗口）切句：

| 模型 | 近场 CER | 远场 CER | 噪声 CER | 总 CER | 召回 | RTF | RSS |
|---|---:|---:|---:|---:|---:|---:|---:|
| CTC + VAD | 26.09% | 23.81% | 87.5% | 32.0% | 4/12 | 0.227 | 136.7 MiB |
| Transducer + VAD | 67.39% | 104.76% | 50.0% | 76.0% | 2/12 | 0.730 | 291.0 MiB |
| Paraformer + VAD | 10.87% | 14.29% | 150.0% | 26.67% | 6/12 | 0.846 | 330.7 MiB |

VAD 让 Transducer/Paraformer 的 RTF 下降（切掉静音与插入），但 CTC 出现 7 个删除、近场召回掉到 2/7。短句数据不利于判断 VAD 的真实端点收益；本阶段**默认关闭 VAD**，后续用长句、真实按键流和电视对白另验收。

## 6. 真实片库候选纠错

- 真实只读扫描宿主 `/vol1/1000/影视` 得到 **42 个有现存 episode 的季候选**，fingerprint `043198…`；不写生产索引。
- 误识别 `搜索穿成一神我靠香火斩诸神` → 真实标题 `穿成野神我靠香火斩诸神`，相似度 **90.9091**；同剧 5 个季全部列出，状态 `needs_clarification / ambiguous_seasons`，全部 `requires_confirmation=true`。
- 精确片名同样要求季确认；`暂停` 被拒绝；库外片名无候选。没有自动选择、没有直接播放、没有改写原始 hypothesis。
- **容器内实测**（Docker 实验服务，CTC 模型，真实索引与只读媒体根）：同一段授权录音识别后返回上述 5 个真实候选，HTTP 200；未授权 401；`../outside.wav` 400。响应 SHA-256 与脱敏候选见 `docs/evidence/v41_phase1_docker.json`。
- 这是**功能演练**，不是 Top-1/Top-3 纠错率、误建议率或播放成功率验收；样本只有少量真实剧名，尚不能给统计门槛结论。

## 7. 独立 Docker 实验服务

- 镜像 `ai-tv-asr-lab:v4.1-experiment`，基于固定 `python:3.11-slim@sha256:07d482…`，实际构建成功；镜像大小 182,869,388 B，amd64。
- 实际运行：非 root、read-only 根、2 CPU / 768 MiB / 64 PID、cap_drop ALL、no-new-privileges、无 host network / Docker socket / 生产 `/data`；模型、音频、索引和媒体根均只读挂载；仅发布 `127.0.0.1` 端口。
- 容器健康检查真实通过；实际 HTTP 识别与候选返回 200；401/400 边界通过；测试结束 `down`，无遗留实验容器。
- **网络边界修正：** 原配置 `internal: true` 会使宿主无法访问已发布端口，实测连接被拒。已改为普通 bridge + 仅回环发布；**不再声称“无外网 egress”**，运行时不下载模型，只有宿主回环可调用。此修正来自真实运行，不是静态设计。
- 运行期缺陷修正：Docker `local` 日志驱动在默认压缩下要求 `max-file > 1`（实测 `max-file: "1"` 导致容器无法创建任务），已改为 2；工作区源码 0600 导致容器内非 root 读取失败，镜像内仅对 `/app` 执行 `chmod -R a+rX`，不改宿主机权限。

## 8. 向后兼容与回滚

- 生产 `adbtv/`、主 `Dockerfile`、主 `docker-compose.yml`、runtime lock 与镜像均未修改；容器仍为 `sha256:0fcb7301…`、版本 b8、四服务 healthy（实验前后均核验）。
- 实验依赖只在 `poc/requirements-v41.lock` 与实验镜像；主镜像不含 sherpa-onnx/JiWER/RapidFuzz/Torch。
- 回滚：`docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml --profile asr-lab down`，删除实验镜像即可；不影响生产。模型缓存与音频留在操作者指定目录，不自动删除。
- 小爱默认入口保持；没有任何生产 ASR 切换。生产四容器仅在实验前后读取健康/镜像 ID，未借实验流量改写其状态。
- “并行”仅指生产链路持续运行、实验链路独立：无法把同一 WAV 灌入电视小爱，故**没有**同音频小爱 A/B；小爱结果仍只能来自真实遥控器 logcat。

## 8.5 Phase 2 扩展指标（同一批真人录音）

Phase 2 在不修改既有 hypothesis 的前提下增加了意图、片名 Top-1/Top-3、误建议/拒识和 P50/P95，详见 [Phase 2 交付报告](V41_PHASE2_DELIVERY.md)。当前 12 条仍是**单说话人**：CTC 意图 0.60、片名 Top-1 1.00（仅 2 条）、误建议 0；Paraformer 意图 0.70、Top-1 1.00；Transducer 意图 0.30、Top-1 0.50。样本量不足以证明泛化，也不能替代多说话人测试集。

## 9. 未完成与下一阶段

- 真实**按键麦克风实时**测试未执行：无采集设备（`/dev/snd` 无 capture PCM，`arecord` 未安装）。工具代码与离线导入已实测；麦克风验收必须换有采集设备的机器并由真人按键录制。
- 未测真实断网、长时间运行、端到端延迟、电视对白回声、ARM64。
- 未启用 transducer 热词：需另测 `modified_beam_search` 与固定 BPE，并做同音负例/无热词对照；不能把本轮 greedy 结果当热词结论。
- 仅 12 条短录音、单一说话人；需要 ≥3 人、每条件 ≥50 句的独立验收集后再决定是否替换小爱。
- 下一步：P2 真实按键流式 + VAD 长句端点；P3 中文 TTS/多轮；之后才考虑唤醒。详见 `docs/VOICE_V41_OPEN_SOURCE_PLAN.md`。

## 10. 复现

```bash
# 独立评测环境（固定哈希，不进入生产）
python3 -m venv /tmp/v41-eval
/tmp/v41-eval/bin/python -m pip install --only-binary=:all: --require-hashes -r poc/requirements-v41.lock

# 准备已审核模型（显式下载；离线推理不再联网）
/tmp/v41-eval/bin/python -m poc.asr_lab prepare --profile ctc --cache "$HOME/.cache/ai-tv-v41"
/tmp/v41-eval/bin/python -m poc.asr_lab prepare --profile transducer --cache "$HOME/.cache/ai-tv-v41"
/tmp/v41-eval/bin/python -m poc.asr_lab prepare --profile paraformer --cache "$HOME/.cache/ai-tv-v41" --with-vad

# 三模型顺序配对（每模型独立进程；无 VAD）
/tmp/v41-eval/bin/python -m poc.asr_compare_v41 \
  --manifest "$HOME/.cache/ai-tv-v41/ctc.json" \
  --manifest "$HOME/.cache/ai-tv-v41/transducer.json" \
  --manifest "$HOME/.cache/ai-tv-v41/paraformer.json" \
  --samples /path/to/authorized/samples.json --consent \
  --output-dir /path/to/new-private-report-dir

# 加 Silero VAD（需 --with-vad 准备的 manifest）
#   同上并追加 --vad

# 独立实验服务
export ASR_LAB_MODEL_DIR="$HOME/.cache/ai-tv-v41"
export ASR_LAB_AUDIO_DIR=/path/to/authorized/audio
export ASR_LAB_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
docker compose -p ai-tv-asr-lab -f docker-compose.asr-lab.yml \
  --profile asr-lab up -d --build
```

完整私有逐句报告在操作者目录，公开脱敏计数见 `docs/evidence/v41_phase1_asr.json` 与 `docs/evidence/v41_phase1_docker.json`。