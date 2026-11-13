# 第三方开源组件清单（THIRD PARTY NOTICES）

本项目遵循 Open Source First：优先复用成熟、许可证兼容的开源实现，仅在无合适方案时自研。
本文件记录**实际引入**的组件，以及**仅完成调研、未进入生产依赖**的候选。

核对时间：2026-11-12（b8）。版本以 `requirements-runtime.lock` / `requirements-test.lock` / `adbtv/web/vendor/SHA256SUMS` 为准。

## 零、b8 运行时锁定

| 组件 | 锁定版本 | 许可证 | 说明 |
|---|---|---|---|
| PyYAML | 6.0.3 | MIT | 运行时唯一必需依赖 |
| pydantic | 2.14.3 | MIT | 结构化校验，缺失自动回落 |
| pydantic-core | 2.50.4 | MIT | 由 pydantic 固定 |
| annotated-types | 0.8.0 | MIT | 传递依赖 |
| typing-extensions | 4.16.0 | PSF-2.0 | 传递依赖 |
| typing-inspection | 0.4.5 | MIT | 传递依赖 |
| setuptools | 75.8.0 | MIT | 仅构建 |
| wheel | 0.45.1 | MIT | 仅构建 |
| playwright | 1.65.0 | Apache-2.0 | **仅测试** |
| greenlet | 3.5.6 | MIT | Playwright 传递依赖，仅测试 |

全部以 `--require-hashes` 安装；运行镜像内不含 sherpa-onnx / onnxruntime / torch / piper / mcp / uiautomator2。

## 一、已引入（生产或测试）

### Alpine.js 3.14.9 — 前端语音对话组件

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/alpinejs/alpine |
| 来源 | npm `alpinejs@3.14.9`，取 `dist/cdn.min.js` |
| 许可证 | **MIT**（`package.json` 声明，仓库 LICENSE.md） |
| 引入方式 | 本地静态文件 `adbtv/web/vendor/alpine-3.14.9.min.js`（sha256 `3ed1eed2…0ad3`）与 CSP 构建 `alpine-csp-3.14.9.min.js`（sha256 `820bc950…b273`），**未修改** |
| 公网依赖 | 无。由 `/vendor/<file>` 本地路由提供，不使用任何 CDN |
| 体积 | 约 44 KB（压缩后） |
| 回退 | 删除 `x-data` 组件即可回到纯表单；不影响 `/api/runs` |

许可证文本为上游 `v3.14.9/LICENSE.md` 原文（sha256 `5f05c492…657d`），存放于 `adbtv/web/vendor/alpine-3.14.9.LICENSE.txt`；
所有 vendor 文件哈希记录在 `adbtv/web/vendor/SHA256SUMS`，并有测试逐项核对。

### Pydantic 2.14.3 — 结构化数据校验

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/pydantic/pydantic |
| 许可证 | **MIT** |
| 引入方式 | 可选依赖。`adbtv/schemas.py` 在已安装时使用 Pydantic 模型，否则使用等价内置校验（同一字段/边界） |
| 体积 | pydantic 0.5 MB + pydantic-core 约 2.3 MB（manylinux x86_64 wheel） |
| 回退 | 未安装即自动回落，功能不降级；见 `schemas.availability()` |
| 安全边界 | 仅校验**结构**；权限、白名单、确认与二次校验仍由原模块负责 |

### Playwright 1.65.0 — 端到端网页测试

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/microsoft/playwright |
| 许可证 | **Apache-2.0** |
| 引入方式 | **仅测试依赖**，不进入生产镜像；`tests/e2e/` 下运行 |
| 浏览器 | `playwright install chromium`，缓存于用户目录（实测解压后约 309 MB），可随时删除；本机经代理下载成功 |
| 回退 | 不安装时 e2e 测试显式 skip 并说明原因，不计入通过 |

本机约束（实测）：Playwright 自带 node 需要 `libatomic.so.1`，NAS 未提供；测试侧改用系统 node
（`/vol1/1000/.tools/node/bin/node`，v22.22.2）包装后可用。该改动只发生在测试 venv，不影响项目运行。

## 二、仅调研（PoC 已实测导入，未引入生产）

### sherpa-onnx 2.0.1

- 许可证：**Apache-2.0**（PyPI 描述声明）；模型权重需**逐个**确认（多数为 Apache-2.0，部分社区模型另有条款）。
- 体积：x86_64 wheel 约 2 MB + 模型（中文流式 ASR 常见 40–200 MB）。
- 用途：可选独立 ASR 服务（VAD、流式识别、关键词检测）。
- 本机 PoC：测试 venv 内导入成功（**984 ms，RSS +10.9 MB**），`OnlineRecognizer`/`VadModel` 可用。
- 结论：接口可通过 `VoiceInputRegistry` 的 `external_asr` 适配；**本轮未接入生产**，因为 NAS 无麦克风、无中文模型、权重许可证未逐个确认。
- b8 已用**用户授权的 10 条本地合成短句**实测（x86_64/2 线程）：RTF **0.347**、加权 CER **16.67%**（10/60）、
  峰值 RSS 112,545,792 B、冷启动 5.22 s；专有名词错误明显，**未接入生产**。
- 模型：`sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01`，模型 26,342,340 B（sha256 `68c9c943…`）、
  tokens 13,366 B（sha256 `6fed8c6c…`）。ONNX 卡无 license 字段，权重来源 `icefall-streaming-zipformer-small-ctc-zh-2025-04-01` 为 **Apache-2.0**。
- ARM64 未实机运行。
- 风险：模型权重许可证必须逐个核对；CPU 实时率需实测，不能引用上游 README。

### MeloTTS 0.1.1

- 许可证：PyPI 元数据未声明（上游仓库 MIT，需以其 LICENSE 为准）；**模型权重另有条款**，商用前须确认。
- 体积：依赖较重（torch 系），安装与内存成本显著高于 Piper。
- 结论：保留为可配置 TTS Adapter 的候选；**本轮不替换 Piper**。本项目实测 Piper 首次合成约 9.4 s（模型加载），MeloTTS 未在本机实测，不作性能承诺。

### MCP Python SDK 2.3.0

- 许可证：**MIT**；wheel 约 0.4 MB。
- 本机 PoC：测试 venv 内导入成功，但**导入耗时约 5 s**（依赖较重）。
- 结论：可作为后续独立任务，把现有手写 JSON-RPC 迁移到官方 SDK；**本轮不动**，避免与语音闭环混在一起造成回归。

### uiautomator2 3.7.1

- 许可证：**MIT**；wheel 约 4.9 MB；需在设备端安装 `atx-agent` 等组件。
- 本机 PoC：测试 venv 内导入成功（约 0.9 s）；设备端需 `atx-agent`，**未向电视安装任何组件**。
- 结论：需真机兼容性实验并明确告知设备端安装/卸载方式；**本轮不引入**，现有 ADB 控制不变。

### Piper（piper-tts 1.2.0）— 可选语音合成，**默认不进镜像**

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/rhasspy/piper |
| 许可证 | PyPI 包 `piper-tts` 为 **GPL-3.0-or-later**；上游 piper 源码为 MIT，但发布物含 GPL 组件 |
| 引入方式 | 构建参数 `INSTALL_PIPER=1` 才安装；默认关闭，容器内 TTS 明确报不可用 |
| 模型 | `zh_CN-huayan-medium.onnx` 由操作者自行放入 `deploy/data/piper/`；**权重许可证需自行确认** |
| 义务 | 启用并分发镜像时需遵守 GPL-3.0-or-later（提供对应源码/许可）；仅自用不受分发义务约束 |
| 回退 | 不启用即无该依赖；`voice.tts.enabled=false` 关闭回执 |

### JiWER 4.0.0 / RapidFuzz 3.14.3 / Click 8.1.8 — 独立 ASR 评测

- 复用 [JiWER](https://github.com/jitsi/jiwer) 的 `process_characters` 实现标准最短编辑 CER，替代比较脚本的 SequenceMatcher；不是生产识别依赖。
- 已核对实际 wheel 的 LICENSE/METADATA：JiWER **Apache-2.0**、[RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) **MIT**、[Click](https://github.com/pallets/click) **BSD-3-Clause**。按各许可保留版权及许可文本；本项目通过依赖安装复用，不复制其实现。
- JiWER 4.0.0 wheel SHA-256：`7efaf0bd336b095d99ddef9dd67e1ee829d75d58aa2a81d9639870b01d6d95ea`。
- 固定依赖见 `poc/requirements-eval.txt`；仅独立评测 venv 安装，不变更生产 runtime lock、Docker 镜像或小爱默认。实际重评结果见 `docs/ASR_COMPARISON_B8.md`。

### V4.1 实验（非生产镜像）

| 组件/权重 | 许可 | 边界 |
|---|---|---|
| sherpa-onnx 2.0.1 / core | Apache-2.0（wheel LICENSE 实测） | 仅实验镜像与独立评测环境；主镜像不含 |
| Silero VAD v5.1.2 权重 | MIT（固定 revision LICENSE + ONNX SHA-256） | 仅实验可用；本轮默认关闭 |
| 中文 small CTC / 中英流式 Transducer / FunASR Paraformer 导出 | Apache-2.0 上游声明；CTC 的 ONNX 卡本身未独立声明，保留源 checkpoint 证据链 | 权重 SHA-256 固定于 `poc/models-v41.lock.json`；不随主包分发 |
| JiWER 4.0.0 / RapidFuzz 3.14.3 / Click 8.1.8 | Apache-2.0 / MIT / BSD-3-Clause | 仅评测与实验服务，哈希锁 `poc/requirements-v41.lock` |

回滚：移除独立实验 compose 项目与实验镜像；生产镜像、数据、四容器和小爱默认不受影响。

### SQLite（Python 标准库 `sqlite3`）— Final5 持久化幂等

SQLite 为 Public Domain；本项目不新增第三方包，仅使用 CPython 标准库 `sqlite3` 创建 `runtime/idempotency.sqlite3`。记录仅含令牌哈希、幂等键、task_id、命令哈希、状态和时间，不含命令正文或令牌明文。

## 三、复用规范执行情况

| 检查项 | 结论 |
|---|---|
| 原始仓库与维护状态 | 均已核对 GitHub 仓库与 PyPI 元数据 |
| LICENSE 与权重许可 | Alpine/Pydantic/Playwright/MCP/uiautomator2 已确认；sherpa-onnx/MeloTTS 权重待逐个确认 |
| Python/系统兼容 | 本机 Python 3.11；Pydantic 需 ≥3.10，符合 |
| 飞牛 x86_64 适配 | pydantic-core、playwright、sherpa-onnx 均有 manylinux x86_64 wheel |
| 镜像体积/内存 | Pydantic 约 3 MB；Playwright 仅测试侧；未把 198 MiB 浏览器放进生产镜像 |
| 接口兼容 | schemas 不改变既有 API；Alpine 只改前端渲染 |
| 供应链风险 | 固定版本；Alpine 走本地静态文件（防 CDN 篡改）；pip 走代理并锁定版本 |
| 可禁用/回滚 | 三项均可单独关闭：不装 pydantic 自动回落；移除 Alpine 脚本回到原生表单；不装 Playwright 只影响测试 |

## 四、禁止事项与已知限制

- 未复制任何未确认许可证的源码。
- 未把上游 README 的性能宣称当作本机实测：本文件中的耗时仅标注为"本机实测"或"未实测"。
- Playwright 浏览器约 198 MiB，只在测试机安装；不支持在无浏览器环境下运行 e2e。