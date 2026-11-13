# V4.1 语音开源技术选型与分阶段计划

调研日期：**2026-11-12（Asia/Shanghai）**。本选型报告对应的 V4.1 工作仅为调研与计划，**未因本计划安装组件、下载新模型、接入生产、修改配置或开展新功能开发**。文中的性能门槛和资源预算均为**待实测的目标**，不是已达到的指标。

## 1. 决策与现状

**小爱继续作为生产默认 ASR。** V4.1 优先验证“飞牛 NAS CPU 推理 + 明确的独立收音端 + 现有语音路由”的可选离线链路。首先复用 sherpa-onnx 的在线 ASR、Silero VAD 和 TTS 运行时；热词必须另选适用的 transducer 模型；唤醒、降噪、Wyoming/Home Assistant 接入均后置、可独立关闭。任何阶段未过门槛，停留在实验模式，不自动替换小爱。

已阅读的背景资料：

- [ASR_COMPARISON_B8.md](ASR_COMPARISON_B8.md)：真人授权录音的离线 ASR 对比与限制；采用其**更新后的标准 Levenshtein 重跑报告**，不把旧百分比直接抄为 V4.1 新结果。
- [OPEN_SOURCE.md](OPEN_SOURCE.md)：开源优先、适配现有架构、语音依赖不进入当前生产环境的原则。其“仅导入、无中文模型”的早期阶段记录已被后续 PoC 推进，不能混作当前状态。
- [V408_DELIVERY.md](V408_DELIVERY.md)：现有小爱遥控器链路、语音路由、Docker 部署和限制；以具体章节和后续修订为准，不以同一交付报告中的历史待办覆盖后写的实测结果。
- [poc/README.md](../poc/README.md)：固定模型 revision/hash、授权样本、独立运行时、CPU 线程数和计时范围；合成语音 smoke test 与真人录音评估分开。

**当前证据边界：** 有授权真人录音的离线回放，不等于独立麦克风持续收音已经跑通。快速喂入分块音频测出的“首个非空结果”，不是从人开口到结果到达的实时延迟；不能与实时首字门槛直接比较。小爱结果来自电视真实遥控器/logcat，尚不能向小爱灌入同一 WAV，因此小爱与本地引擎只能做注明条件的语句级比较，不能声称同音频 A/B。真实独立麦克风、远场连续监听、回声条件、ARM64、长期运行、下面其他候选的本机质量和资源均未验收。

## 2. 许可审查方法及官方证据

代码许可、模型权重许可、ONNX/GGUF 等导出物许可、词典/分词器/音色及训练数据说明是不同条目。运行时 Apache-2.0 或 MIT，不会给所加载权重自动授予同样许可。模型卡的声明也是上游声明，不代表独立完成训练数据权利审计。

本次先使用 **dsh-web-search-pro** 的 `search.run` 证据包和 `read.fetch` 阅读官方文档/仓库；部分 Hugging Face 和 GitHub raw 页面由该读取链路超时，随后经本机已配置代理只读访问官方原始文件和 GitHub API，未下载权重或音频。下表均为 2026-11-12 获取的证据。`main/master` 链接可变；除了列出的固定 revision，进入下一阶段前必须保存实际 commit、许可全文、文件 SHA-256 和下载来源，不能把本次网页观察当作锁版本。

| 候选/材料 | 代码许可 | 权重/数据许可及实际结论 | 官方证据 |
| --- | --- | --- | --- |
| sherpa-onnx | Apache-2.0 | 运行时许可不覆盖全部 ASR/TTS/VAD/KWS 模型 | [代码 LICENSE](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE)、[官方能力与平台](https://github.com/k2-fsa/sherpa-onnx) |
| 现有中文 small CTC int8 | 由 sherpa-onnx 推理 | 固定 ONNX 卡**没有独立 license 字段**；卡指向的源 checkpoint 固定卡声明 Apache-2.0。保留“派生来源证据链”，不能写成 ONNX 卡明确授予 Apache-2.0 | [ONNX 卡 a5f60fe0…](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01/blob/a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da/README.md)、[源卡 6768aa8b…](https://huggingface.co/csukuangfj/icefall-streaming-zipformer-small-ctc-zh-2025-04-01/blob/6768aa8b65bc68e128af2ee08a7142dd0abe4be4/README.md) |
| 中文/英文流式 Zipformer transducer | 由 sherpa-onnx 推理 | 已有候选固定卡明确 Apache-2.0；不是现有 CTC，也尚未在本机测准确率/热词 | [双语卡 98590b7e…](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/blob/98590b7ed6443e77b714204da2757d75e1a642f4/README.md) |
| FunASR 工具包 | 当前官方代码 LICENSE 为 MIT，不能沿用旧版本印象 | 权重逐模型核对。`funasr/paraformer-zh-streaming` 当前卡指向 `apache-2.0-20260804` 标签并说明该快照为 Apache-2.0；历史 revision `45df2405b92dfeb21e01ea4465954677005ae370` 缺对应许可文件，不能追溯套用新声明 | [代码 LICENSE](https://github.com/modelscope/FunASR/blob/main/LICENSE)、[Paraformer 模型卡](https://huggingface.co/funasr/paraformer-zh-streaming/blob/main/README.md)、[标签下 LICENSE](https://huggingface.co/funasr/paraformer-zh-streaming/blob/apache-2.0-20260804/LICENSE) |
| SenseVoice | 当前官方代码 MIT | `FunAudioLLM/SenseVoiceSmall` 卡为 `license: other`，链接 **FunASR Model Open Source License Agreement v1.1**。该自定义协议含保留出处/名称、行为终止和修订条款；不能标为标准 Apache-2.0 或把代码 MIT 当权重许可 | [代码 LICENSE](https://github.com/QwenAudio/SenseVoice/blob/main/LICENSE)、[模型原始卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall/raw/main/README.md)、[MODEL_LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE) |
| whisper.cpp / OpenAI Whisper | whisper.cpp MIT | OpenAI 官方明确 **Whisper 代码及权重 MIT**；第三方量化/转换分发物仍须核对来源与转换链 | [whisper.cpp LICENSE](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE)、[OpenAI 许可声明](https://github.com/openai/whisper#license) |
| Silero VAD | MIT | 官方发布的 VAD 按仓库 MIT 声明；选定 ONNX 文件需固定版本和哈希，不能借此泛指全部 Silero 语音模型 | [官方 README 与模型说明](https://github.com/snakers4/silero-vad)、[LICENSE](https://github.com/snakers4/silero-vad/blob/master/LICENSE) |
| RNNoise | BSD-3-Clause | 官方构建会另下载模型，README 没有给出足以将任意外部模型视为 BSD 的独立声明；所选模型包的许可与 hash 仍须核验。训练数据另列，不能等同权重许可 | [官方镜像 README/COPYING](https://github.com/xiph/rnnoise)、[主仓库](https://gitlab.xiph.org/xiph/rnnoise)、[模型下载脚本](https://github.com/xiph/rnnoise/blob/main/download_model.sh) |
| DeepFilterNet | MIT 或 Apache-2.0 双许可，可择一 | 官方 README 明确**本仓库代码和模型权重**均双许可；外部新权重不自动纳入 | [License 章节](https://github.com/Rikorose/DeepFilterNet#license) |
| Piper 当前 OHF 分支 | GPL-3.0，已读取 COPYING；历史 rhasspy/piper 的 MIT 结论不能覆盖该分支 | 逐音色核对。`zh_CN/huayan/medium` 的模型卡写明数据集 **License: Unknown**，本轮不作为可默认分发的中文音色。容器分离不消除分发 GPL 程序的义务 | [当前项目](https://github.com/OHF-Voice/piper1-gpl)、[COPYING](https://github.com/OHF-Voice/piper1-gpl/blob/main/COPYING)、[huayan MODEL_CARD](https://huggingface.co/rhasspy/piper-voices/raw/main/zh/zh_CN/huayan/medium/MODEL_CARD) |
| MeloTTS | MIT | `myshell-ai/MeloTTS-Chinese` 模型卡 `license: mit`，并明确中文/中英混读；卡顶 `language: ko` 与正文不一致，记录为元数据瑕疵。BERT/G2P/词典及 ONNX 转换物另审，不只看 PyPI 元数据 | [代码许可及说明](https://github.com/myshell-ai/MeloTTS#license)、[中文模型原始卡](https://huggingface.co/myshell-ai/MeloTTS-Chinese/raw/main/README.md) |
| Kokoro | 官方 Python 代码 Apache-2.0 | `hexgrad/Kokoro-82M` 原始卡 `license: apache-2.0`；普通话音色表确有 4 女/4 男音色。第三方 ONNX 导出包、音素化依赖和额外音色逐项核对，不把 82M 主权重许可推及全部资产 | [代码 LICENSE](https://github.com/hexgrad/kokoro/blob/main/LICENSE)、[权重原始卡](https://huggingface.co/hexgrad/Kokoro-82M/raw/main/README.md)、[音色表](https://huggingface.co/hexgrad/Kokoro-82M/raw/main/VOICES.md) |
| sherpa-onnx KWS | 运行时代码 Apache-2.0 | 官方支持中文自定义关键词，但**本轮未选定且未核实具体 KWS 权重的独立许可**，不得默认随产品分发 | [KWS 官方文档](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)、[官方模型发布入口](https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models) |
| openWakeWord | Apache-2.0 | 附带预训练唤醒模型 **CC BY-NC-SA 4.0**；共享 embedding 有不同许可，不能用其 Apache 声明覆盖整套唤醒模型。官方现成模型以英文为主，中文不是即插即用 | [官方 README/License](https://github.com/dscripka/openWakeWord#license)、[预训练模型说明](https://github.com/dscripka/openWakeWord#pre-trained-models) |
| Wyoming / Home Assistant Core | Wyoming MIT；HA Core Apache-2.0 | 协议/Core 自身不提供可一概许可的 ASR/TTS 权重；每个服务、音色和附加组件独立核验 | [Wyoming LICENSE](https://github.com/OHF-Voice/wyoming/blob/main/LICENSE.md)、[HA Core LICENSE](https://github.com/home-assistant/core/blob/dev/LICENSE.md) |

**分发准入：** 优先使用可追溯的标准宽松许可代码和权重。自定义许可、NC 模型、模型卡 Unknown 或导出物来源不明的候选可作为隔离调研项，不能标成“已通过产品分发审查”。若未来选 GPL Piper，需保存相应源代码提供方式、构建信息和 notices；若只复用协议，也不能把某个具体 Wyoming 服务的许可推断成协议 MIT。

## 3. ASR 选择：先解决 CPU 实时性与专名，再谈替代

所有“中文质量”判断只描述任务适配性及验证方法；除更新后的本地 ASR 报告外，没有候选在本机的横向质量实测，**不排名、不引用上游 CER 或倍速作为飞牛结论**。

| 候选 | 中文与流式边界 | CPU/内存/磁盘考虑 | Docker 与接入成本 | 维护/决定 |
| --- | --- | --- | --- | --- |
| sherpa-onnx small 中文 CTC int8 | 真正在线模型；现有 PoC 可复用。中文短指令/专名需看更新报告；**官方 hotwords 不支持 CTC** | 固定模型约 26.34 MB，模型+tokens 26,355,706 B 来自既有清单；大小不是 RSS。2 线程只作为实验起点 | 自建独立 CPU sidecar，复用现有 manifest 与音频校验；流式网络协议、端点和取消仍需适配。成本低到中 | 活跃上游；保留基线/回归用途，不能把“加热词”写成调参即可完成 |
| sherpa-onnx 中文/双语 Zipformer transducer | 真流式；**热词要求 transducer + `modified_beam_search`**，默认 greedy 不支持。中文/混合 tokens、BPE 词表必须匹配。热词是偏置，不保证专名正确，更不能把热词命中当用户确认 | 已核对双语候选固定 int8 工件合计 198,270,793 B，来自既有清单，尚未下载/实测；beam search 增加搜索成本，需与 greedy 同音频比较 | 同运行时、不同模型角色 encoder/decoder/joiner/tokens，另审 BPE 资产。独立挂载，不能把 CTC manifest 改个字段就冒充已适配；成本中 | **优先热词实验候选**；需同时证明专名改善与噪声无热词“强插入” |
| FunASR Paraformer streaming | 流式 Paraformer 可在线分块；普通 Paraformer 不因挂 WebSocket 就变流式。2pass 的在线 partial 与离线最终修正分别计时。模型示例 chunk `[0,10,5]` 使用 60 ms 帧，不能承诺 300 ms 首字 | 官方有 CPU runtime；当前模型卡记 220M 参数，通常比 small CTC 的存储/部署负担大，具体 RSS/线程/RTF 待测。避免把 Python/PyTorch 基线和 C++ CPU SDK 当同一配置 | 官方提供 online CPU Docker/源码构建和 WebSocket，成本中到高；可复用协议，必须适配 final/partial、2pass 覆盖、断流。官方旧示例含 privileged，项目部署应仅给实际需要权限 | 活跃；**流式中文备选**。优先许可完整的固定 Apache 标签；辅助 VAD/标点/LM 各自建清单 |
| SenseVoiceSmall（FunASR 或 sherpa-onnx 离线运行时） | 中文/粤语候选，模型为非自回归；非自回归不等于在线状态式流式。VAD 切句、滑窗重复推理仅是分段服务，句尾等待仍在；情感/事件标签不用于操作权限判定 | 有 CPU/ONNX 路径，可用于整句复核；Python 栈和导出运行时资源不同，逐配置测试。上游“处理 10 秒只需若干 ms”不是本机延迟 | 官方有服务路径；若复用 sherpa 需选离线 API 与对应导出物，不能复用在线 CTC 工厂。成本中 | 当前代码有维护；**整句质量对照候选**，权重自定义许可需单独决策，不作为默认宽松许可方案 |
| whisper.cpp + Whisper 多语模型 | 支持中文应选多语模型，`.en` 为英文。`whisper-stream` 是定时/滑窗转写示例，不能等同 Zipformer 在线缓存语义；需防重复文本与静音幻觉 | 官方 README 参考：tiny 75 MiB/约273 MB内存、base 142 MiB/约388 MB、small 466 MiB/约852 MB；均为上游参考且随量化/后端变化，不是 NAS 实测。CPU 可运行不保证大模型低延迟 | 官方 Docker、C/C++ CPU/量化路径成熟；命令行离线接入低，可靠流式去重/取消接入中到高 | 活跃；保留跨引擎离线对照及兼容备选，不作为当前低延迟中文首选 |

官方能力依据：[sherpa 热词](https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html)、[ASR 模型目录](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/index.html)、[FunASR CPU 实时指南](https://github.com/modelscope/FunASR/blob/main/runtime/docs/SDK_advanced_guide_online_zh.md)、[SenseVoice 官方用法](https://github.com/QwenAudio/SenseVoice)、[whisper.cpp 资源与 Docker](https://github.com/ggml-org/whisper.cpp)、[whisper-stream 示例](https://github.com/ggml-org/whisper.cpp/tree/master/examples/stream)。这些链接证实功能入口，不证明选定版本在本机已经支持全部组合。

热词实验建议从“飞牛、短剧类型、当前片库前若干候选名”构成有限清单，按会话更新；保存清单版本、boost、beam、是否去掉无关片名。测试包括“带热词、同音但非热词、根本没说该热词、电视节目念出片名”四类，量化偏置带来的假命中，不能靠无限扩大 boost 达标。

### 3.1 自定义词表与影视剧名纠错：复用检索，不改写识别证据

拟复用真实 `media_index` 的片名、已审核别名及当轮检索候选构建有限词表，保留媒体唯一ID和索引版本；不得生成不存在的剧名或把模型猜测写回正式别名库。词表一份可供适用的 transducer 热词使用，另一份用于识别后的候选建议，两者分别评估，CTC只参与后一条路径。

候选重排优先复用已核验 MIT 许可的 **RapidFuzz 3.14.3**，以规范化编辑距离/字符串相似度提取有限候选，结合当前检索上下文和候选前两名分差展示建议；上游为 [RapidFuzz 官方项目](https://github.com/rapidfuzz/RapidFuzz)、[MIT LICENSE](https://github.com/rapidfuzz/RapidFuzz/blob/main/LICENSE)。相似度不是概率或语音置信度，阈值必须在调参集确定并在独立验收集冻结。若后续引入拼音相似度、多音字词典或专门纠错模型，应另核代码、词典/模型许可后再立项，本计划不假设新增依赖已经许可通过。

只对明确的“搜索/播放某片名”槽位提供“是否指的是某某剧”的建议，不静默替换原文；同音多候选、低分或分差不足、未在库中命中时追问用户，选中片名后仍走既有播放确认。保留原始 `hypothesis`、独立候选列表与最终用户选择；原始 CER/ASR 专名命中照原文计算，纠错后的候选命中与任务完成率另报，禁止用纠错结果美化 ASR CER。

验收至少覆盖真实库片名、长短名/别名、同音不同剧、用户明确说了另一个片名、未见/库外片名、电视对白及热词无关负例。分别统计候选 Top-1/Top-3 命中、建议覆盖率、错误建议率、用户拒绝率和追问率；默认选择/播放错误次数目标为0，错误建议率目标≤1%（仅为待验目标，给出分母与置信区间）。自动纠错不在本轮范围内；有限候选建议未达门槛时保持原始搜索和追问。

## 4. VAD、降噪、TTS、唤醒和多轮复用比较

下表的资源轻重为架构判断，均需本机测量；没有实测中文听感、MOS 或 CER 改善值。

| 候选 | 质量/功能限制 | 资源与 Docker | 接入成本及决定 |
| --- | --- | --- | --- |
| Silero VAD（优先经 sherpa-onnx） | 只判语音区间，不识别说话内容/唤醒词；节目对白也是语音。支持 8/16 kHz，需调开头保留、结束静音与最大句长，避免剪掉“暂停”的首音 | ONNX 可避免把 Torch 拉入生产。上游模型量级较小、单线程性能声明仅作参考；复用 ASR sidecar，所选模型需版本适配 | 低到中；**第一阶段端点首选**。需测漏检、误检、切头/切尾，而非只看总体识别率 |
| RNNoise | 面向噪声抑制；并非 AEC，不能可靠消掉电视/自身 TTS 人声。中文辅音可能受影响，不保证改善 ASR | C 库，官方示例 48 kHz raw PCM；要在前端 48 kHz 降噪后再下采样16 kHz，避免反复重采样。需自建镜像/编译固定 ISA；不依赖 Torch | 中；模型包许可未闭合前只做备选。不把 GitHub 镜像陈旧当主仓停更 |
| DeepFilterNet | 48 kHz 全带宽增强；STFT/lookahead 有算法延迟，补偿文件时间轴不等于实时零延迟。改善主观听感也可能损伤 ASR | Rust 预编译推理可免 Python；Python 训练/推理依赖更重。自建 sidecar，额外 CPU、缓冲与重采样成本待测 | 中到高；**有噪声基线后再试**，与不增强/RNNoise 同源配对。许可较清晰但不因此提前启用 |
| sherpa-onnx TTS 运行时 | 官方有中文/中英 Melo-VITS、Matcha、Kokoro 等入口。分句合成/音频回调不自动等于文本 token 连续流式；必须测首可播放音频及实际扬声器首声 | 可与 ASR 共用运行时但独立 worker，避免合成堵塞解码。中文词典、phonemizer、声码器可能各有资产 | 中；**优先复用运行时**，先选一个许可闭合的中文音色，避免同时装多套 Torch 服务 |
| MeloTTS 中文 / 对应 ONNX 导出 | 原始中文模型卡 MIT；支持中英混读，上游说 CPU 可实时，项目未实测。重点试听片名、多音字、数字、英文混读 | 原始 Python 含 Torch/BERT 等较重依赖；sherpa VITS 导出有机会减轻运行栈，但词典和导出物需审。没有已验证官方一键适用本项目 Docker 的结论 | 中；与 Kokoro 进行短回复对照。优先审计固定 ONNX 导出或自导出链，不直接凭 Python 包许可入包 |
| Kokoro-82M 中文音色 | 主权重/代码 Apache-2.0；普通话音色有官方列表，质量仍待真人盲听。不可拿英文 demo 推导中文质量 | 82M 参数不等于82 MB；量化、音色向量、G2P 增加资源。Python 与 sherpa ONNX 两条路径成本不同；Docker 自建/固定导出物 | 中；**低延迟中文 TTS 对照候选**，可用固定短句缓存改善常见响应，但冷未缓存也必须达标 |
| 当前 Piper | 本地短回复方向适合低延迟，但具体中文音色权利不能忽略；huayan Unknown 不通过默认分发门槛 | 本地 CPU、已有 Wyoming 服务生态；当前 GPL 引擎/音素化依赖入镜另审，音色独立挂载 | 已有 Piper 合成样本不构成任意音色生产授权；作为兼容备选，先换成许可明确的中文音色或保持用户自行提供 |
| sherpa-onnx KWS | 中文可自定义关键词，tokens/拼音单位必须与模型一致；KWS、ASR hotwords、VAD 是三种能力。唤醒成功也不授权播放/执行 | 常驻 CPU；不要直接用完整 ASR 常开替代专用唤醒。具体模型占用和许可待核验，复用运行时可减少依赖 | 中；按键/浏览器开始会话先行，**唤醒后置可选**，先通过误唤醒与回声门槛 |
| openWakeWord | 英文预训练为主；中文训练需要另行数据/音色授权，NC 预训练不作默认分发 | 16 kHz/80 ms 帧，ONNX/TFLite，新增常驻模型；有 Wyoming 封装但需另核其版本许可 | 中到高；中文本项目优先级低于 sherpa KWS；保留在明确用途边界内的对照 |
| Wyoming | JSONL+PCM 的语音服务协议，已有 STT/TTS/wake 生态；不是语义记忆或安全确认引擎。服务是否支持 transcript streaming 需能力协商 | 协议本身轻量，实际开销来自模型；服务有 Docker 生态，但每个镜像独立审版本/许可/架构。**协议默认无鉴权/加密**，仅受控内网或受保护隧道 | 中；有收音卫星/HA需求时优先复用协议，避免自创整套音频传输；不把裸 TCP 暴露公网 |
| Home Assistant Assist pipeline / conversation | 官方支持 wake→STT→conversation→TTS，带 `conversation_id` 延续会话；这并不自动提供本项目片库候选、确认、任务状态或权限语义 | HA Core 容器/完整系统增加内存和运维负担，不为本项目仅两轮确认强行新增 HA。用户已有 HA 时适配更合算 | 中到高；复用 pipeline 事件/会话接口或可选网关，保留本项目确认、取消、幂等和执行规则，禁止双端重复执行 |

官方入口：[Silero VAD](https://github.com/snakers4/silero-vad)、[RNNoise](https://gitlab.xiph.org/xiph/rnnoise)、[DeepFilterNet](https://github.com/Rikorose/DeepFilterNet)、[sherpa TTS 目录](https://k2-fsa.github.io/sherpa/onnx/tts/index.html)、[Wyoming 协议](https://github.com/OHF-Voice/wyoming)、[HA Assist pipeline](https://developers.home-assistant.io/docs/voice/pipelines/)、[HA Wyoming 集成](https://www.home-assistant.io/integrations/wyoming/)。

## 5. 维护状态快照与来源日期

以下日期是 **GitHub 官方仓库 API 的 `pushed_at`（UTC）**，不是 release 日期、模型训练日期，也不是质量/响应速度保证。所有所列仓库当次 API 的 `archived=false`。每行链接为本次读取的官方 API；`updated_at` 可能受非代码活动影响，本表不以 star 数或 `updated_at` 判断维护。

| 仓库 | API 最近 push（UTC） | 对选型的影响 |
| --- | --- | --- |
| [k2-fsa/sherpa-onnx](https://api.github.com/repos/k2-fsa/sherpa-onnx) | 2026-11-11 14:59:02 | 近期有推送；固定当前 PoC 2.0.1 与下一阶段版本，避免拿在线文档推断旧 wheel 全部能力 |
| [modelscope/FunASR](https://api.github.com/repos/modelscope/FunASR) | 2026-11-11 17:31:03 | 工具包近期推送；旧 CPU 镜像/模型标签有自己的时间线，不能只升级 Python 包就认为服务已更新 |
| [QwenAudio/SenseVoice](https://api.github.com/repos/QwenAudio/SenseVoice) | 2026-09-30 10:22:01 | 官方入口由 FunAudioLLM 链接重定向至 QwenAudio；记录重定向及代码/模型许可分离 |
| [ggml-org/whisper.cpp](https://api.github.com/repos/ggml-org/whisper.cpp) | 2026-11-10 12:09:38 | 近期推送；CPU 编译选项及量化模型需和测试报告配套 |
| [snakers4/silero-vad](https://api.github.com/repos/snakers4/silero-vad) | 2026-10-22 02:02:27 | 近期推送；各代 ONNX 输入/状态有差别，不能无版本替换 |
| [xiph/rnnoise（镜像）](https://api.github.com/repos/xiph/rnnoise) | 2025-02-22 23:58:31 | README 明确 GitHub 只是 convenience copy，主仓为 Xiph GitLab；本轮未获取主仓最新提交日期，不作“停止维护”判断 |
| [Rikorose/DeepFilterNet](https://api.github.com/repos/Rikorose/DeepFilterNet) | 2026-11-06 06:21:50 | 近期推送；API 单一 SPDX 为 NOASSERTION，而 README 明确代码/权重双许可，应读许可正文 |
| [OHF-Voice/piper1-gpl](https://api.github.com/repos/OHF-Voice/piper1-gpl) | 2026-11-11 17:24:06 | 近期推送，同时 README 正在招募维护者；不承诺维护 SLA |
| [myshell-ai/MeloTTS](https://api.github.com/repos/myshell-ai/MeloTTS) | 2024-12-24 19:17:13 | 非归档但较久无 push，版本兼容/供应链维护风险较高，不能只因模型可用就称活跃 |
| [hexgrad/kokoro](https://api.github.com/repos/hexgrad/kokoro) | 2025-08-06 22:28:53 | 非归档；原始模型卡 v1.0 发布日期为 2025-01-27，与代码 push 日期分开记录 |
| [dscripka/openWakeWord](https://api.github.com/repos/dscripka/openWakeWord) | 2025-12-30 16:47:27 | 非归档；README 新闻仍列 2024-02-11 v0.6.0，不能称此即最新 release，采用前复查依赖 |
| [OHF-Voice/wyoming](https://api.github.com/repos/OHF-Voice/wyoming) | 2026-11-09 21:05:27 | 近期推送；历史 rhasspy URL 重定向到 OHF，具体服务仓库不等于协议仓库 |
| [home-assistant/core](https://api.github.com/repos/home-assistant/core) | 2026-11-12 03:06:10 | 近期推送；适配必须绑定 HA 版本和 pipeline API，避免追随 dev 分支 |

维护准入还需在真正接入时核验 release/tag、目标平台构建、安全通告、关键缺陷及锁定依赖。本轮未全面审查 issue/CVE，不能声称这些项目“无漏洞”。

## 6. 飞牛 NAS CPU 推荐拓扑与预算

**先验证 x86_64 CPU，不预设 GPU，不把 ARM wheel 可下载当 ARM 已验收。** 实施前记录 NAS 的 CPU 型号/核数/ISA、内存、内核、Docker 版本及实际媒体播放负载；未确认机器规格前不写“某芯片必可实时”。

建议的可选链路（均待实施）：

1. **音频入口独立。** 按键开始的浏览器/手机/专用 USB 麦克风卫星，或已有 Wyoming satellite。明确设备、声道、采样率、时钟与音频授权；NAS 没有麦克风就不宣称常听。小爱遥控器现有 ASR 文本入口保持可用，不假设能拿到其原始 PCM。
2. **基础 CPU sidecar。** 常驻 sherpa-onnx + 单个已核验 Silero VAD + 一个 ASR 模型。ASR 先2线程、VAD1线程作为待测配置，限制队列和最大句长；small CTC 建基线，再换 transducer 验证热词。不要同机同时跑全体候选来比较单路延迟。
3. **归一化事件进入现有适配位。** `partial` 仅供显示，`final` 才可进入现有 `external_asr`/语音路由；给会话、轮次、请求、设备和模型版本标识，处理重复 final、乱序、过期与断流。音频服务不能绕过现有确认/白名单/执行层。
4. **TTS 独立 worker。** 优先在 sherpa-onnx 上对照许可明确的 Melo/Kokoro 中文音色，先短回复、分句和可取消播放；ASR 与 TTS 分离调度，测同机竞争。固定提示语可缓存，但必须分别报告缓存命中/未命中指标。
5. **先半双工。** 播放本系统 TTS 时暂停或明确标记收音，避免自唤醒；电视外放背景不能靠“暂停本机收音”解决，先测实际音频入口。需要打断/全双工时，单列带参考回放的 AEC 与时钟对齐任务，RNNoise/DeepFilterNet 不替代 AEC。
6. **降噪和唤醒默认关闭。** 有稳定音频与指标后再逐一加入，确保有明确增益再保留。Wyoming/HA 网关仅在已有设备或跨服务需求时新增。

**规划预算，未实测：** 基础 ASR+VAD sidecar 单路软目标峰值 RSS ≤512 MiB、总 CPU 配额起点2核；TTS 独立预算 ≤1 GiB/2核，CPU 繁忙时限流而非堆积。4核/8 GiB 仅作为试验排期参考条件，不是飞牛已确认硬件或最低兼容承诺。重型对照引擎可另给 ≤2 GiB/2核实验预算，超预算不自动放宽生产目标。模型缓存磁盘预算基础链路≤1 GiB，超出须记录模型与依赖的实际分解，不能把整个开发 venv 算成模型。

后续 Docker 要求：单独可选 profile/sidecar；运行时与模型卷分离、模型只读、离线启动、健康探针、架构专用 wheel/hash 和镜像 digest；不以 `latest` 或启动时联网取模型作为交付。若仅网络音频，不映射宿主全部音频设备；若 USB ALSA 收音，仅映射需要设备，不照搬上游 privileged 示例。当前生产四服务和运行时锁本轮均不变。

## 7. 分阶段交付与验收门槛

所有阶段均为**待执行**，依赖前一阶段证据；阶段通过也不意味着已批准切换默认。资源/延迟未满足时，应缩小候选或保留小爱，不以“模型更大”替代验收。

| 阶段 | 工作与可审查交付物 | 进入下一阶段的门槛 |
| --- | --- | --- |
| P0：证据冻结与评测契约 | 固定代码版本、权重/音色/词典 revision/hash、许可全文与来源日期；登记录音授权、音频设备与硬件。链接更新后的 B8 标准 CER 报告，不复制旧百分比 | 必需资产来源与许可100%有记录；Unknown/自定义/NC 单列且不默认分发；计时定义、数据拆分与合成/真人标签齐全。没有真实入口时仅进入离线实验 |
| P1：离线配对比较 | 同一批授权 WAV 比较 CTC、transducer greedy/beam/热词、Paraformer；SenseVoice/Whisper 作离线对照；输出逐样本 S/D/I、命令/槽位、专名、资源 | 固定 scorer 与规范化；保存原始 hypothesis，所有对照同样本且分层；可重复运行无丢样。初筛 RTF p95<1、无无限插入/失控内存；不靠离线首非空判实时通过 |
| P2：真实按键音频流 | 专用入口按真实时钟送16k PCM；接 VAD→在线ASR→现有路由；验证断流、重连、取消、重复 final；原始音频默认不留存 | 达到第8节实时/质量门槛；连续100轮无重复执行、无跨会话确认；网络断开后会话终止/过期，不补执行旧指令；用户可明确回到小爱 |
| P3：中文TTS及多轮闭环 | 固定许可音色；“搜索→候选→第一个→确认/取消”与过期/打断；CPU与媒体播放并发；需要时做 Wyoming/HA 可选网关 | TTS首声/RTF达标；不少于100个多轮场景无串会话和误确认；TTS故障不阻塞现有文本/小爱处理；同一任务只执行一次 |
| P4：可选增强/唤醒 | 分别比较无增强/RNNoise/DeepFilterNet；中文KWS；录入电视对白、自身TTS及静音负例；全双工另开AEC验收 | 增强不使clean CER恶化超过1个百分点，噪声改善需配对证据；KWS漏唤醒≤5%、误唤醒≤0.1次/小时，至少30小时负例及100次正例，报告置信区间；不满足则保持按键 |
| P5：灰度与默认决策 | 固定可选Docker profile、SBOM/notices、离线重建、模型哈希、升级/回滚手册、真实NAS播放负载24小时稳定性；人工复核失败样本 | 所有必需指标和许可门槛通过；24小时无崩溃、无音频队列持续增长、无跨会话执行，资源无持续泄漏；关实验链路后小爱仍可用。切默认需另行明确决策，不能由本计划/测试自动触发 |

P1 数据至少覆盖真人近场、约3米远场、电视声/家庭噪声、不同说话人/语速、常见短指令和长片名、同音不同意图、非命令对白/静音。建议独立验收集至少3位说话人、每个语音条件≥50句，合计≥150句；热词调参集与验收集分离并包含未见片名。可控噪声记录 SNR/混音来源，实录噪声不可假装已测 SNR。原有12条用于回归，样本规模不足以证明可靠替代。

## 8. 性能与质量指标定义（V4.1 目标，全部待验）

| 指标 | 计算/测量方法 | 目标门槛 |
| --- | --- | --- |
| CER | 标准 Levenshtein，NFKC、去标点和空白，报告 S/D/I 与参考字符数；语料级总编辑数/总字符数；CER 可超过100%，不可截断。以更新 B8 报告实际采用的 scorer 为准 | 真人clean≤5%；远场≤10%；噪声≤30%；插入异常逐条审核，静音/对白不得触发操作 |
| 指令与槽位 | 从同一输出到现有解析器，分别统计意图正确、片名/序号/集数精确正确；不能只以输出“含某关键词”判成功 | clean命令+必需槽位≥95%；远场/噪声≥90%；任何确认/取消/跨会话安全样本零错误执行 |
| 专名 | 预登记片名/“飞牛”等实体的规范化完全命中；报告分母、逐实体计数与无热词对照 | 精确召回≥90%；同时报告误插入和同音负例，不以提升召回掩盖假命中 |
| 首个 partial | 真实采集的语音起点→客户端收到非空partial，单调时钟；拆分音频chunk/网络/解码；同时记录从首帧计时值 | 首个非空partial p95≤500 ms，≤300 ms作为优化目标。不能用快速离线回放结果判达标，也不用于覆盖最终识别质量门槛 |
| 句尾最终结果 | 人声结束→final到达，包含VAD等待、right context和网络 | p95≤800 ms；另外记录VAD hangover设定和硬件/网络条件 |
| ASR计算RTF | 推理墙钟/原始音频时长；离线不加实时sleep；预热/冷启动、逐样本p50/p95和加权总值分列 | 单路预热p95≤0.5；媒体播放与TTS并发时p95<1且不积压；CPU秒与墙钟分别给出 |
| 端到端响应 | 人声结束→第一段实际可听TTS/确认提示；另记任务动作完成，区分LLM、网络和设备等待 | 确定性短指令p95≤1.5 s到首声；LLM/外部服务另列分阶段预算，不伪装所有任务都1.5 s完成 |
| TTS首声与合成 | 确定文本可用→首个可播放PCM，以及→客户端/扬声器首声；20–40汉字未缓存短句；缓存命中另报 | 未缓存首PCM p95≤500 ms，设备首声p95≤800 ms；合成RTF p95≤0.5；CPU并发下无播放饥饿 |
| TTS中文可懂度 | 真人盲听片名/多音字/数字/英文混读，≥50条；至少3位听者，分别统计关键字正确与自然度 | 关键字可懂度≥95%；自然度目标≥3.5/5，报告量表与人数，不把主观打分称标准MOS认证 |
| 启动与内存 | 进程启动到ready、模型工厂加载分开；峰值RSS、稳态RSS、容器cgroup峰值分开；模型/依赖磁盘分开 | 基础sidecar冷启动目标≤10 s、峰值RSS≤512 MiB；TTS≤1 GiB；任何放宽需独立记录而非删除失败数据 |
| VAD | 标注语音边界的漏检/误检、切头/切尾；专测“暂停/取消”等短句与电视对白 | 所有关键短指令测试不切掉影响语义的首尾；参数在验收集冻结；不得把VAD=人声视作用户授权 |
| 唤醒 | 正例漏检率、负例单位小时误唤醒数；电视片段/本机TTS/静音分别报告 | 见P4；额外验证唤醒后仍需正常会话与确认，不直接执行TV命令 |

每份报告需写硬件、架构、系统、CPU governor/线程/配额、引擎版本、模型revision/hash、采样率/chunk、VAD与解码参数、噪声条件、并发负载、样本数、p50/p95、失败样本和计时边界。小样本p95和置信区间不稳定应注明，不能用均值代替尾延迟。

## 9. 多轮状态与复用边界

复用现有对话/执行结构，只在未来适配音频事件。会话最少区分设备、session、turn、request、候选快照及待确认动作；“第一个”绑定当前会话候选版本，“确认”只作用于当前未过期动作，“取消”立即使其失效。并行设备、迟到final、TTS中断、断网恢复、超时、重放都要覆盖。不得把自由文本“是/好”跨会话解释成旧任务确认。

本次读取的 [Wyoming 官方协议 README 的 Context 部分](https://github.com/OHF-Voice/wyoming#context) 明确列出自由扩展 `context` 对象中的 `conversation_id`、`device_id`、`satellite_id`；这些是有则传递的上下文键，未知键应忽略，不是所有服务都保证支持的会话管理能力。可用这些键或 HA pipeline `conversation_id` 关联请求，适配层仍需维护自己的 session/turn 映射；本项目的候选、权限、幂等、过期与二次确认不能委托给未验证的外部默认行为。复用协议与官方 pipeline 的价值在设备/服务互通，不是替换现有 Agent/ADB/媒体服务，也不是另建一套记忆库。若接入已有 HA，只选一端作为最终执行者并核对事件顺序、错误和取消传播。

## 10. 尚未闭合的事项

- 本选型报告对应的 V4.1 工作仅为调研与计划，不实现或部署上述链路，不改变生产默认；其实施需按上述阶段另行开展。
- 新 ASR 结果以更新后的 [ASR_COMPARISON_B8.md](ASR_COMPARISON_B8.md) 为唯一实测引用；本计划不保留旧百分比副本，也不从旧快速回放“首帧”推断真实延迟达标。
- 未获得真实独立麦克风远场连续链路验收，无法断言持续监听可用、KWS可用、电视回声可控或可替代小爱。
- sherpa中文KWS具体权重、低延迟TTS选定ONNX导出/音色/词典、RNNoise所选模型包的独立许可待核验；Piper huayan已观测Unknown，保持分发阻断。
- SenseVoice权重自定义许可和FunASR特定模型快照区别已记录；标签名并非完整供应链证明，进入P0仍需解析SHA并读取许可文件全文。
- 维护日期是官方API检索快照；RNNoise主仓最新日期、每项release历史/安全通告与所有依赖未全面审查。网页README与原始文件可能缓存不同，实际接入以固定commit原文为准。
- 没有对其他候选做本机质量、CPU/RSS、Docker多架构构建或中文听感测试；本文不据上游营销数字给质量冠军、不保证CPU速度。
