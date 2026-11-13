# V4.0.0b5 测试与验收（开源优先集成）

日期：2026-11-11。基线 V4.0.0b4（552 项）。**模拟测试、真实浏览器、真实电视分开报告**。

## 1. 自动化

```
make test   → 22 项模拟集成检查通过
            → Ran 560 tests … OK (skipped=2)
make e2e    → Ran 6 tests … OK   （真实 Chromium，非模拟 DOM）
```

`skipped=2` 是 Pydantic 专属用例：宿主机 Python 未装 pydantic（PEP 668 限制），
在项目 venv（已装 2.14.3）中运行同一文件为 **7 tests OK，无跳过**。

## 2. 前端：Alpine.js（真实浏览器）

用 Playwright + Chromium 1248 驱动真实页面（服务端为模拟电视 + 临时媒体库）：

| 用例 | 结果 |
|---|---|
| 错误令牌被拒绝 | 通过 |
| 语音区渲染并调用 `/api/voice`（不是 `/api/runs`） | 通过 |
| 真实候选渲染 → 点击选择 → 继续对话 | 通过 |
| 播放进入待确认 → 点击「取消」后确认区消失 | 通过 |
| TTS 任务轮询到 READY → 用带令牌的 fetch 取音频 | 通过 |
| 普通任务表单仍走 `/api/runs` | 通过 |

真实浏览器暴露并修复的缺陷：

1. CSP 只允许 `'unsafe-inline'`，本地 Alpine 脚本被拒 → 改为允许 `'self'`（并加 `media-src blob:`）。
2. 默认 Alpine 需要 `unsafe-eval` → 改用官方 **CSP 构建**，并移除 `x-model`、带参 `@click`、`x-init="init()"`，
   改为 `Alpine.data` + 原生事件绑定，**未放开 `unsafe-eval`**。
3. `/api/config` 未暴露 Beta 开关 → 显式返回 `voice.intelligence.enabled`。
4. 语音组件在登录前初始化，拿不到配置 → 登录后刷新标志。

## 3. 结构化校验：Pydantic（双后端）

| 后端 | 结果 |
|---|---|
| 内置（无 pydantic） | 通过（2 项 pydantic 用例 skip） |
| Pydantic 2.14.3 | **7/7 通过**，含 strict 类型、额外字段、边界 |

同一批非法输入（未知 route、布尔/NaN 置信度、缺字段、含 shell 的 pending、非法 job 状态）
在两个后端都被拒绝。**Schema 不替代安全判断**：权限/白名单/确认仍在原模块，并有既有测试覆盖。

## 4. 依赖代价实测

| 组件 | 实测 |
|---|---|
| Alpine CSP 构建 | 44 KB，本地文件，无 CDN |
| Pydantic | pydantic 0.5 MB + pydantic-core 约 2.3 MB |
| Playwright | wheel 36.6 MB；Chromium 198 MiB（测试机缓存，非生产镜像） |
| 直连 PyPI 速度 | 约 8 kB/s；经本机代理 127.0.0.1:7890 约 120 kB/s |

上表为本机实测，不引用上游 README 的性能宣称。

## 5. 未验证 / 限制

- **真人麦克风 ASR**：未做。页面麦克风依赖浏览器 Web Speech，本轮未用真实麦克风验收。
- **电视扬声器播报**：仍未实现（实测不可用），TTS 只在浏览器/手机出声。
- **Docker 镜像**：本会话无 Docker socket 权限，4.0.0b5 镜像未构建；需 NAS 侧 `docker compose build && up -d`。
- **sherpa-onnx / MeloTTS / MCP SDK / uiautomator2**：仅完成元数据调研与决策，未接入生产（见 `docs/OPEN_SOURCE.md`）。
- Playwright 在本机需 `AI_TV_CHROMIUM` 指定已安装的 Chromium 1248；Playwright 1.65 期望 1254，CDN 在代理下下载不稳定。