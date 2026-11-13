# V4.0 开源优先集成记录

依据 Open Source First 原则，本轮先核实候选组件的许可证与代价，再决定"复用 / 适配 / 自研"。
本文件记录**决策依据**；实际版本、许可证与回滚见 `THIRD_PARTY_NOTICES.md`。

当前状态（2026-11-12 最后一轮修复）：sherpa-onnx 已完成授权真人录音本地重评，尚未进入生产；采用 JiWER 标准 CER。V4.1 全候选、模型许可及阶段门槛统一见 [开源技术选型报告](VOICE_V41_OPEN_SOURCE_PLAN.md)。下文标为历史的记录不代表当前未测试。

## 一、结论速览

| 候选 | 决策 | 依据 |
|---|---|---|
| Alpine.js 3.14.9 | **已复用**（本地 vendored，CSP 构建） | MIT；44 KB；无构建步骤；可单独移除 |
| Pydantic 2.14.3 | **已复用**（可选后端） | MIT；结构校验与既有手写校验等价；缺失时自动回落 |
| Playwright 1.65.0 | **已复用**（仅测试） | Apache-2.0；b8 历史真实 Chromium e2e 9 项，麦克风为模拟 |
| sherpa-onnx 2.0.1 | 独立 PoC 已实测，未接入生产 | Apache-2.0 代码，权重许可分别核验；已有 12 条授权真人录音，独立麦克风持续监听待验 |
| JiWER 4.0.0 | **已复用**（仅独立评测） | Apache-2.0；RapidFuzz MIT；整数 S/D/I、标准 Levenshtein、回归及完整重评已执行 |
| MeloTTS 0.1.1 | 仅调研，未接入 | PyPI 未声明许可证、权重另有条款；依赖显著重于 Piper；未实测不得比较性能 |
| MCP Python SDK 2.3.0 | 仅调研，后续独立任务 | MIT；迁移协议层风险独立，避免与语音闭环混做 |
| uiautomator2 3.7.1 | 仅调研，未接入 | MIT；需设备端 `atx-agent`，必须先做兼容实验并给出卸载方式 |

## 二、为什么是"适配"而不是"重写"

- **前端**：原页面已有登录、任务、SSE、Skills、调度等大量逻辑。Alpine 只接管「语音对话」这一个区块，
  其余代码与 `/api/runs` 完全不动。没有引入构建工具链，也没有换成 SPA 框架。
- **校验**：`adbtv/schemas.py` 是**适配层**——有 Pydantic 时用模型，没有时用等价内置校验。
  安全判断（权限、白名单、确认、二次校验）仍在原模块，Schema 只保证形状。
- **测试**：Playwright 只进测试依赖，不进生产镜像；浏览器缓存可随时删除。

## 三、实际踩到的兼容问题（真实记录）

1. **CSP 与 Alpine**：默认 Alpine 使用 `new Function` 求值，被严格 CSP 拒绝。
   解决：改用官方 `@alpinejs/csp` 构建，并把 `x-model`、`x-init="init()"`、带参 `@click` 改成
   `Alpine.data` + 原生事件绑定。CSP 无需放开 `unsafe-eval`。
2. **CSP 与 Playwright**：页面 CSP 会阻止 `wait_for_function` 的字符串求值。
   解决：e2e 改为 Python 侧轮询 DOM（`wait_text`）。
3. **Playwright 自带 node 缺 `libatomic.so.1`**：NAS 未提供该库。
   解决：测试侧把 driver 的 `node` 换成系统 node（v22.22.2）的包装脚本；仅影响测试环境。
4. **浏览器版本不匹配**：Playwright 1.65 期望 Chromium 1254，本机仅有 1248，且 CDN 下载在代理下不稳定。
   解决：e2e 支持 `AI_TV_CHROMIUM` 显式指定已安装的 Chromium；实测 1248 可正常驱动。
5. **`/api/config` 未暴露 Beta 开关**：页面无法判断是否显示语音区。
   解决：显式返回 `voice.intelligence.enabled`（非密钥）。

## 四、当前未做与原因

- **sherpa-onnx / MeloTTS 未接入生产**：独立 ASR 真人重评未达准确率门槛，MeloTTS 未本机实测；真实独立麦克风持续监听未验收，保留 `external_asr` 适配位和小爱默认。
- **MCP SDK 未迁移**：属独立任务，本轮若迁移会与语音闭环回归混在一起。
- **uiautomator2 未接入**：需要设备端安装组件，必须先实验并告知卸载方式；现有 ADB 控制已验证，不替换。
- **未把上游 README 的性能当作本机性能**：sherpa-onnx 当前仅报告 [本地录音重评](ASR_COMPARISON_B8.md) 的实际值；MeloTTS 等未实测，不能比较本机性能。

## 五、历史 PoC 导入实测（本机，2026-11-11）

安装位置：**测试 venv `/tmp/osfv`**（`--system-site-packages`），**不进入生产镜像**。
安装方式：`https_proxy=http://127.0.0.1:7890 pip install sherpa-onnx mcp uiautomator2`。

| 组件 | 实测 |
|---|---|
| sherpa-onnx 2.0.1 | 导入成功；`OnlineRecognizer` / `OfflineRecognizer` / `VadModel` 可用；**导入 984 ms，RSS +10.9 MB** |
| mcp 2.3.0 | 导入成功；`Client` / `CallToolRequest` 等可用；**导入约 5059 ms**（依赖较重，迁移需单列任务） |
| uiautomator2 3.7.1 | 导入成功；设备端需 `atx-agent`；本机 `<电视ADB地址>:5555` 在线，但**未向电视安装任何组件** |

本节只记录 2026-11-11 的导入探测。当时没有中文模型；之后已获取固定模型并完成合成和真人录音识别。当前准确率与 RTF 以 [b8 重评报告](ASR_COMPARISON_B8.md) 为准，不沿用导入耗时为推理耗时。

**结论**：sherpa-onnx 的能力面符合需求（流式 + VAD + 关键词检测），可经 `external_asr` 适配；
但真实独立麦克风持续监听仍待验证；本地录音推理不构成持续监听验收。

## 六、代理与网络（本机实测）

- 直连 PyPI 约 8 kB/s；经 `http://127.0.0.1:7890` 约 120 kB/s。
- Playwright `chromium` 下载在直连下停滞在 40%；经代理完成 **Chromium 1254**（约 309 MB 解压后）。
- Playwright 默认无头模式需要 `chrome-headless-shell`；本机未装该独立包，
  e2e 改用完整 Chromium 的新无头模式：`chromium.launch(channel="chromium")`，实测可用。

## 七、复现方式

```bash
# 元数据调研（离线也能跑，只报告 unknown）
python3 scripts/research_oss_poc.py
https_proxy=http://127.0.0.1:7890 python3 scripts/research_oss_poc.py

# 前端组件与许可证
ls adbtv/web/vendor/            # alpine-3.14.9.min.js / alpine-csp-3.14.9.min.js / LICENSE

# 结构化校验双后端
python3 -m unittest tests.test_schemas_v40          # 内置后端（可能 skip pydantic 用例）
PYTHONPATH=. .venv/bin/python -m unittest tests.test_schemas_v40   # 有 pydantic 时

# 真实浏览器 e2e（测试依赖，不进生产镜像）
AI_TV_CHROMIUM=/path/to/chrome .venv/bin/python -m unittest tests.e2e.test_voice_ui
```