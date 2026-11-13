# V4.0.0b8 真人 ASR 重评（标准 Levenshtein CER）

2026-11-12 12:28:53 +08:00，使用现有 12 条真人录音重新执行识别与计分，完整运行退出码 0。**小爱保持生产默认**；当前独立 ASR 尚未达到替换门槛。

## 方法与开源选择

替换 `poc/asr_compare.py` 中的 `difflib.SequenceMatcher`：它寻找匹配块，不能保证最短编辑距离，重复字符尤其容易产生不同结果。本轮采用成熟语音评测库 [JiWER 4.0.0](https://pypi.org/project/jiwer/4.0.0/)，调用 `process_characters`，底层使用 RapidFuzz 的 Levenshtein 对齐。保留项目原有归一化：Unicode NFKC、去标点和空白、保留大小写、按 Unicode 码点计字符。空归一化参考拒绝；空假设计删除；CER 可以大于 100%。

已读取实际下载 wheel 的 LICENSE 与 METADATA：JiWER 为 **Apache-2.0**，RapidFuzz 3.14.3 为 **MIT**，Click 8.1.8 为 **BSD-3-Clause**。JiWER wheel SHA-256：`7efaf0bd336b095d99ddef9dd67e1ee829d75d58aa2a81d9639870b01d6d95ea`。对比标准库手写 DP、直接用 RapidFuzz、JiWER 后，选 JiWER：它直接提供语音评测的 S/D/I 计数及对齐，避免再次维护自写计分器。旧 PoC `sherpa_asr.cer` 已是 Levenshtein DP，本轮用于独立交叉校验，不是新比较脚本的回退算法。

逐条记录完整编辑列表及整数替换 S、删除 D、插入 I；总 CER = Σ(S+D+I) / Σ参考字符数，不平均句子百分比，也不从浮点 CER 反推错误数。专有名词及整句指令召回使用相同归一化后的完整子串命中。专名命中不代表整条识别正确。

## 模型、运行环境与样本

- 固定模型：`csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01`，revision `a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da`。
- 模型 26,342,340 B，SHA-256 `68c9c943840f7d9cf3e8a4970ba50f404feb5277f611fa82b7e72267786fa84a`；tokens 13,366 B，SHA-256 `6fed8c6c248516f38e7faa19404b57413e8ce259f1cbc1fa4aebc86eac32fdfd`。本次重新通过固定清单大小与哈希验证。
- [ONNX 卡](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01/blob/a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da/README.md) 未独立声明许可证；[来源权重卡](https://huggingface.co/csukuangfj/icefall-streaming-zipformer-small-ctc-zh-2025-04-01/blob/6768aa8b65bc68e128af2ee08a7142dd0abe4be4/README.md) 声明 Apache-2.0。保留派生来源证据，不视为 ONNX 卡自带许可。
- Linux x86_64、Python 3.11.2、sherpa-onnx 2.0.1、CPU 两线程、greedy CTC；JiWER 与识别依赖仅安装在独立评测环境，不进入生产镜像。
- 同一批操作者授权的真人普通话：近场安静 7 条、约 3 米远场 3 条、背景噪声 2 条；PCM16 / 单声道 / 16 kHz，总长 50.261 s。本轮重新读入并校验全部音频哈希，未上传音频。
- 样本清单 SHA-256：`60f86f01071d68e4201ed66d0d7a5205e17acb6514ba0340b992585162496d52`。

## 本轮实际重评结果

| 条件 | 样本 | S / D / I | 错误 / 参考字符 | CER | 指令召回 | RTF | 专名命中 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 近场 clean | 7 | 3 / 0 / 0 | 3 / 46 | **6.5217%** | 5/7 | 0.2405 | 1/2 |
| 远场 far-field | 3 | 3 / 1 / 0 | 4 / 21 | **19.0476%** | 1/3 | 0.3060 | 1/2 |
| 噪声 noise | 2 | 1 / 0 / 13 | 14 / 8 | **175.0000%** | 1/2 | 0.4427 | 1/1 |
| 总计 | 12 | 7 / 1 / 13 | 21 / 75 | **28.0000%** | 7/12 | 0.2967 | **3/5（60%）** |

以上百分比均由本轮整数编辑数重新计算。它们与旧文档的四舍五入百分比相同，是这批输出恰好得到相同最短距离；不能据此认为 SequenceMatcher 等价。新增重复字符反例 `ababab → aaaaab` 的标准距离为 2，旧匹配块算法会计 4。

| 样本 | 条件 | S / D / I | 错误 / 字符 | CER | 专名命中 |
|---|---|---:|---:|---:|---:|
| human-01 | clean | 0 / 0 / 0 | 0 / 4 | 0% | 1/1 |
| human-02 | clean | 1 / 0 / 0 | 1 / 13 | 7.6923% | 0/1 |
| human-03 | clean | 0 / 0 / 0 | 0 / 7 | 0% | — |
| human-04 | clean | 0 / 0 / 0 | 0 / 7 | 0% | — |
| human-05 | clean | 2 / 0 / 0 | 2 / 4 | 50% | — |
| human-06 | clean | 0 / 0 / 0 | 0 / 4 | 0% | — |
| human-07 | clean | 0 / 0 / 0 | 0 / 7 | 0% | — |
| human-08 | far-field | 0 / 0 / 0 | 0 / 4 | 0% | 1/1 |
| human-09 | far-field | 2 / 1 / 0 | 3 / 13 | 23.0769% | 0/1 |
| human-10 | far-field | 1 / 0 / 0 | 1 / 4 | 25% | — |
| human-11 | noise | 1 / 0 / 6 | 7 / 4 | 175% | 1/1 |
| human-12 | noise | 0 / 0 / 7 | 7 / 4 | 175% | — |

专名错误集中在影视剧名“穿成野神我靠香火斩诸神”；“飞牛”在三种条件均包含于输出，但噪声输出夹带大量额外文字。只有五次专名出现，不能推广为大规模准确率。

## 本轮资源与延迟

| 指标 | 本轮值 |
|---|---:|
| 识别器工厂初始化墙钟 | 4.3115 s |
| 第一条首个非空部分结果 | 0.3028 s |
| 第 2–12 条平均推理墙钟 | 1.2769 s |
| 总推理墙钟 / 原始音频时长 | 14.9113 / 50.261 s |
| 进程 CPU 时间 | 28.6833 s |
| 进程生命周期峰值 RSS | 165,265,408 B（157.61 MiB） |

CPU 时间沿用脚本 `time.process_time()` 的进程生命周期口径，包含初始化等开销；RSS 为整个进程的峰值，包含新评测库开销，不能解释为模型增量内存。RTF 为各句推理墙钟之和除以音频时长。本机文件按 200 ms 块尽快喂入，没有实时节拍；首个非空部分结果不是麦克风到输出的端到端延迟，也不是“首帧”。没有采集真实独立麦克风链路、抖动或端点延迟。

## 小爱关系与生产门槛

小爱结果来自真实遥控器 logcat，无法把上述同一音频送入小爱，因此本轮不提供同音频 A/B 优劣结论。此前遥控器验收是独立历史记录，见 [V408_DELIVERY.md](V408_DELIVERY.md)。

沿用 b8 替换门槛：近场 CER ≤5%、远场 ≤10%、噪声 ≤30% 且无大量插入、专名 ≥90%。本轮四项均未达标。首部分结果回放测得 302.8 ms，不能作为实时 ≤300 ms 的验收证据。小爱继续作为生产默认；独立识别与 V4.1 新功能未上线。

## 复现与证据

```bash
/tmp/osfv/bin/python -m pip install -r poc/requirements-eval.txt
/tmp/osfv/bin/python -m unittest discover -s tests -p test_asr_compare_metrics.py -v
umask 077
/tmp/osfv/bin/python -m poc.asr_compare \
  --manifest "$HOME/.cache/ai-tv-b8-asr/model.json" \
  --samples "$HOME/.cache/ai-tv-b8-asr/human/samples.json" \
  --consent --num-threads 2 \
  --report "$HOME/.cache/ai-tv-b8-asr/human/report-levenshtein-b8.json"
```

完整私有报告含逐句假设、完整编辑、音频哈希、S/D/I、命中及计时。公开的 [脱敏重评证据](evidence/asr_reassessment_b8.json) 保留逐条整数计数和资源数据，不包含原音频或识别全文；完整报告 SHA-256：`f3bc2e7f5ec9c3916fdb854ce7b3adb45d744b6979a205bda15640981aa21bfb`。
