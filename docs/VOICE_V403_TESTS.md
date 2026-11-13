# V4.0.0b3 测试与验收

日期：2026-11-11。基线 V4.0.0b2（496 项）。所有数字来自本次实际运行，模拟 / 真实模型 / 真实电视分列。

## 1. 自动化回归

```
make test → 22 项模拟集成检查通过
          → Ran 524 tests … OK
```

新增 28 项针对性测试（`tests/test_voice_v403.py` 25 项 + 相关扩充），覆盖两个 P0 与两个 P1 的确定性缺陷。无跳过、无退化。

## 2. P0-1 跨容器确认（新增 9 项测试）

| 场景 | 结果 |
|---|---|
| voice 提交 → console 确认 | 通过（修复前：NEEDS_CLARIFICATION） |
| console 提交 → voice 确认 | 通过 |
| 两会话 session_id 一致 | 通过 |
| 二次确认（一次性） | 拒绝执行 |
| 过期确认（时钟推进 30s） | 拒绝执行 |
| 新请求作废旧提议 | 拒绝执行 |
| 确认前目标消失（媒体库不再解析） | 拒绝执行，不调用 Agent |
| 4 线程并发提交 | 无异常、会话文件完整 |
| 落盘内容 | 无 shell/分号，仅规范化命令 + nonce |

**真实电视**：voice 说"播放穿成野神第一部第一集" → NEEDS_CONFIRMATION；console 提交"确认" → **SUCCESS，独立观测 playing=True/PLAYING**（修复前 PARTIAL 且不播放）。

## 3. P0-2 TTS 线程（新增 5 项测试）

| 场景 | 结果 |
|---|---|
| `audio_base_url` 为空 + 合成成功 | 线程存活，标记 `audio_base_url_missing`，不计 delivered |
| 连续 100 次请求 | 线程存活，`failures=0`，`delivered+overflow=100`，`spoken==delivered`（不再重复计数） |
| 队列满 | `speak=false, reason=tts_queue_full`，不谎报成功 |
| 合成抛异常 | 记 `failures` 与 `last_error`，后续请求继续成功 |
| `status()` | `tv_audio=false` + 实测原因、交付通道、认证方式 |

## 4. P1-1 跨容器打断（新增 5 项测试）

| 场景 | 结果 |
|---|---|
| console 任务被 voice 取消 | INTERRUPTED，control 文件写入 |
| scheduler 任务被取消 | INTERRUPTED |
| 另一台电视的任务 | NO_TASK，不写 control |
| 未带前缀的打断 | UNAUTHORIZED，不执行 |
| 慢 ADB（1.5s）下的 `handle()` | 调用方 <0.5s 返回 `PENDING`，后台完成 |

**真实电视**：任务运行中"取消任务" → **166.7ms** 返回 INTERRUPTED，任务以 CANCELLED 结束（3.1s）。

## 5. P1-2 复杂语义

六条指定表达均有确定性测试；真实模型 100 条额外样本（带真实已播上下文，`tests/fixtures/voice_zh_v403_model.json`）：

| 意图 | 样本 | 正确 | 准确率 |
|---|---|---|---|
| search | 30 | 28 | 93.3% |
| play（含 select/resume_episode） | 20 | 17 | 85.0% |
| clarify | 34 | 34 | 100% |
| sequence | 16 | 16 | 100% |
| **合计** | **100** | **95** | **95.0%** |

其中 32 条真正落到模型（其余由确定性规则处理，`real_model_requests=32`）；模型 Schema 合法率 32/32。

阶段耗时（同一批样本）：LLM p50 **2938ms** / p95 3639ms；路由 p50 2805ms / p95 4004ms（含模型等待）。规则路径无需模型，实测 p50 1.4ms（161 条夹具基准）。

真实电视端阶段耗时（本次验收）：ASR→路由 1.2–1.5s（含观察）、暂停 5.1s、继续 4.7s、打断 166.7ms；TTS `tts_ms=0`（默认关闭）。

## 6. 未验证 / 边界

- **真人麦克风 ASR 端到端**仍未做：本次真机验收为**文字注入**（`source=xiaomi_logcat/web_microphone`），不是真实语音识别。
- **4.0.0b3 镜像构建与四容器健康**：本会话无 Docker socket 权限，未自行构建；需 NAS 执行 `docker compose build && up -d` 后由 readiness 版本确认。
- 电视端出声仍不可用（P0-2 只保证"合成→可下载"链路正确），`played` 恒为 false。
- 100 条样本的期望标签由人工标注，`play` 的 3 条偏差集中在"刚看的那集从第一集开始"这类自相矛盾表达；不作为整体准确率外推。
- 跨容器共享会话依赖 `shared_dir` 位于四个容器共享的 `/data`；不配置时退化为单进程（预期行为）。