# V4.0.0b8 最后一轮工程修复验收

日期：2026-11-12（Asia/Shanghai）。版本保持 4.0.0b8。本轮完成标准 CER、严格恢复演练、小范围回归与交付文档统一，以及 V4.1 开源语音选型和阶段计划；没有开发 V4.1 新功能或切换生产 ASR。

## 源码修改

| 文件 | 最终行为 |
|---|---|
| `poc/asr_compare.py` | 使用 JiWER 标准 Levenshtein 最短编辑对齐，复用既有 NFKC/去标点空白策略；保存整数 S/D/I 和完整编辑列表；整句及专名同一归一化；按总字符加权聚合；报告 scorer 版本、样本清单及音频哈希 |
| `poc/requirements-eval.txt` | 固定 JiWER 4.0.0、RapidFuzz 3.14.3、Click 8.1.8，仅独立评测环境安装；生产依赖及镜像保持当前状态 |
| `tests/test_asr_compare_metrics.py` | 12 项：替换/插入/删除、重复字符反例、长重复自动匹配问题、空假设、空参考拒绝、归一化、CER>1、完整编辑及加权聚合；短字符串穷举与既有 DP 交叉检查 |
| `scripts/rollback_drill_b8.py` | 归档全成员预检，手工复制普通文件；真实配置加载器在隔离环境解析路径；按实际媒体目录验证 32 字节密钥及清单摘要；按实际 memory.path 打开只读 SQLite，完整 integrity_check 严格唯一 ok；任何关键项失败阻止成功及 --apply |
| `tests/test_rollback_drill_b8.py` | 27 项：正常、不完整、缺失/损坏配置、实际数据库丢失/损坏、decoy 库、实际媒体密钥路径及缺失/错长/摘要不符、环境隔离、manifest 失败、安全归档和 CLI 阻断 |

归档拒绝穿越、绝对路径、非 data 根、符号/硬链接、设备/FIFO、未知类型及重复成员；不恢复不可信权限/owner。配置指向归档 `/data` 外的关键数据直接拒绝，不能用宿主同名文件补齐。真实数据库不要求 `.sqlite3` 扩展名；存在无关数据库不算成功。

本工作目录是已解包源码，没有 Git 元数据。公开源码包可与原 b8 公共包对比；私有生产配置、数据、音频和模型不入包。

## 实际执行的最终测试

| 执行 | 实际结果 | 范围与限制 |
|---|---|---|
| `/tmp/osfv/bin/python -m unittest discover -s tests -p 'test_*.py' -v` | **669 项通过，0 失败、0 跳过，75.918 s，exit 0** | 原 630 项加新增 39 项；独立环境包含 pydantic 与 JiWER，原可选依赖用例也实际运行。不是沿用旧 629 通过/1 跳过数字 |
| `/tmp/osfv/bin/python tests/test_suite.py` | **22 项通过、0 失败，exit 0** | 模拟电视集成检查；不是本轮真人电视操控验收 |
| 标准 CER 定向回归 | **12 项通过，exit 0** | 另在最终 669 项全量中再次运行 |
| 强化恢复定向回归 | **27 项通过，exit 0** | 临时生成夹具，另在最终全量中运行；不接触 Docker |
| 原部署脚本回归 | **7 项通过，exit 0** | 最终全量中包含；测试中的回滚输出为 mock，不当实机回滚证据 |
| 原 12 条授权真人录音重跑 | **完整推理与新计分成功，exit 0** | 哈希验证后的原录音；本机 CPU 两线程；不上传音频 |
| 原真实私有备份严格临时恢复 | **通过，exit 0** | 归档 58,604,747 B；配置有效、实际密钥与 manifest 摘要相符、实际 memory.sqlite3 integrity_check=ok；未传 --apply |
| 当前 Docker 四服务只读核验 | **全部 running/healthy，版本 b8，内置 healthcheck 全通过，exit 0** | 使用 verify_services(expected_version='4.0.0b8', wait=False)，未重建/重启/回滚容器 |

最初用宿主 Python 复现基线时是 630 tests、2 skipped（缺可选依赖），不作为最终验收环境。最终统一在具备所需可选依赖的 `/tmp/osfv` 执行，669 项均运行成功。该 venv 含系统 site-packages，是测试与评测环境，不宣称本轮重新完成了干净 runtime-lock 安装。现有 runtime-lock 未修改。

历史 b8 有 9 项 Chromium e2e；本轮未改 UI、生产路由或浏览器代码，没有再次运行浏览器 e2e，也未重新采集遥控器麦克风。单元与模拟集成不能推导真实断网、真实麦克风或 ARM 已验收。

## 真人 ASR 的新结果

| 条件 | S / D / I | 错误 / 字符 | CER | 专名 |
|---|---:|---:|---:|---:|
| clean 7 条 | 3 / 0 / 0 | 3 / 46 | 6.5217% | 1/2 |
| 远场 3 条 | 3 / 1 / 0 | 4 / 21 | 19.0476% | 1/2 |
| 噪声 2 条 | 1 / 0 / 13 | 14 / 8 | 175.0000% | 1/1 |
| 全部 12 条 | 7 / 1 / 13 | 21 / 75 | 28.0000% | 3/5（60%） |

这些是 12:28:53 +08:00 成功重跑的数据，与历史百分比四舍五入后相同并不表示沿用旧结果。完整方法、逐条计数、实际新资源与计时见 [ASR 重评](ASR_COMPARISON_B8.md)。首非空部分结果 302.8 ms 是快速文件回放值，不当真实端到端延迟。当前质量门槛未通过，小爱保持默认。

## Docker 与回滚统一口径

1. **历史实际部署**：11:07–11:12 已构建 b8、启动四健康容器，执行实际镜像回滚后恢复 b8；见 [交付记录](V408_DELIVERY.md)。
2. **历史回滚版本边界**：清单记录的旧镜像 `sha256:d2682f9304eae7f095e08b98ba2a773c0c86f278c040dcb5a6baf044c76fd61b` 在回滚记录中仍报告 b8；pre-b8 标签不是版本证明，不能称为已回 b7。
3. **本轮实际数据恢复检查**：对上述历史同一数据备份用修复后的脚本临时恢复并严查配置/密钥/数据库；成功不等于已向生产卷写回数据。
4. **本轮当前容器观察**：四容器实际镜像均为 `sha256:0fcb7301cf2bd1d4484a8be9cc8f0586c81017d41af7891335e9d5d8ca5f0341`，全部健康及 b8；本轮修改的是宿主源码/运维评测脚本，未把新源码重新构建入该生产镜像。

按 [NAS 操作说明](NAS_UPGRADE_B8.md) 可区分默认数据恢复检查和显式 `--apply` 镜像回滚。无需为证明当前修复而额外中断生产电视服务。

## 开源选型与待完成事项

[V4.1 选型与阶段计划](VOICE_V41_OPEN_SOURCE_PLAN.md) 比较 sherpa-onnx、FunASR、SenseVoice、whisper.cpp、Silero VAD、RNNoise、DeepFilterNet、MeloTTS、Kokoro、Piper、KWS/openWakeWord、Wyoming 和 Home Assistant，逐项记录代码/权重许可、中文适配、资源、维护、Docker 和接入成本。

优先 sherpa-onnx + Silero VAD 的可选 CPU sidecar；现有 CTC 不能直接启用官方 hotwords，必须独立验证 transducer + modified_beam_search，片名纠错限定真实片库并保留追问/确认。TTS 对照 Melo/Kokoro 的许可闭合导出物；降噪与唤醒后置。具体顺序为许可/证据冻结→离线同源对比→真实按键流式→中文TTS与多轮→可选增强/唤醒→灰度及默认决策，所有新开发阶段均待执行。

尚未验收：真实独立麦克风远场/持续监听与端到端延迟、真实断网重连、ARM64、扩大真人数据集、其他候选本机性能/中文听感、选定 KWS/TTS 资产完整许可及 Docker 构建。小样本报告不构成替代小爱的依据。

机器可读计数和脱敏核验信息见 [ASR 证据](evidence/asr_reassessment_b8.json) 与 [最终测试/部署/恢复摘要](evidence/b8_final_validation.json)。
