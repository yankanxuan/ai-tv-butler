# V4.3 测试报告

日期：2026-11-12。版本：**4.3.0**（代码 `adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` 一致）。

## 1. 汇总

| 类别 | 结果 |
|---|---|
| Python 语法/导入 | 通过（`python -m compileall adbtv`，全部模块可导入） |
| 单元测试 | **955 通过 / 0 失败 / 0 跳过**（V4.2 的 925 + V4.3 新增 30） |
| 模拟电视集成 | 22 通过 / 0 失败（`tests/test_suite.py`） |
| Docker Compose 配置 | `docker compose -p ai-tv-butler config --quiet` 通过 |
| 发布包敏感信息审计 | `scripts/package.py public --check` 通过（230 文件） |
| 真实 NAS/电视/LLM | 部分执行，逐项标注（见第 4 节） |

执行命令：

```bash
python3 -m compileall -q adbtv
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/test_suite.py
docker compose -p ai-tv-butler config --quiet
python3 scripts/package.py public --check
```

## 2. V4.3 新增测试（`tests/test_assistant_v43.py`，30 项）

### 2.1 事实校验（P0）

| 用例 | 期望 | 结果 |
|---|---|---|
| 模型输出"本地有12部电影"且未调用任何工具 | 拦截，不得出现 12 | 通过 |
| 模型虚构片名《不存在的神剧》（无工具） | 拦截 | 通过 |
| 调用真实工具后仍虚构片名 | 拦截，只保留真实条目 | 通过 |
| 真实工具结果 + 正确数量 | 接受，`answer_source=local_catalog` | 通过 |
| 纯闲聊、无工具、无事实 | 允许，标记 `unverified` | 通过 |
| 工具抛异常 | 记为 `tool_error`，不假成功 | 通过 |
| 空媒体库 | `state=empty`，回答"0 部" | 通过 |
| 根目录不存在 | `state=unavailable`，回答"不可用" | 通过 |
| 在线片源未接入 | 不得编造在线数量 | 通过 |

### 2.2 超时与预算（P1）

| 用例 | 结果 |
|---|---|
| 模型调用收到的是**剩余预算**而非 90s 配置 | 通过 |
| 慢模型被预算切断，回退确定性答案 | 通过 |
| `LLMTimeout` / 连接失败 / 500 / 429 分类 | 通过 |
| 429/5xx 在预算内重试一次 | 通过 |
| 预算耗尽时不发起模型调用 | 通过 |
| HTTP 客户端把超时钳制到剩余预算 | 通过 |

### 2.3 统一资源工具（P1）

7 个 `catalog_*` 工具存在且可调用；旧 `library_*` 为同一实现的别名；`catalog_status` 区分 `ok/empty/unavailable`；`catalog_get_episodes` 返回真实季集；`catalog_get_playback_progress` 来源为 `device_status`；结果不含文件路径。

### 2.4 自然语言与多轮（P0/P1）

| 语句 | 期望 | 结果 |
|---|---|---|
| 现在有哪些资源？/ 资源库里有什么？/ 有哪些电视剧？ | 真实库回答，不追问片名 | 通过 |
| 推荐一部好看的。 | 基于真实库推荐 | 通过 |
| 播放第二个。→ 播放第二集。 | 先选作品再选集，二者不混淆 | 通过 |
| 我说的是第二部，不是第二集。 | 纠正为选作品 | 通过 |
| 继续播放刚才那部。 | 用真实播放历史 | 通过 |
| 现在播放的是什么？ | 事实查询，不误判为控制 | 通过 |
| 我上次看到了哪里？（无历史） | 如实说没有记录 | 通过 |
| 播放这部的第一集。 | 锁定已选作品并进入确认 | 通过 |
| 指标含 `answer_source` | `local_catalog` | 通过 |

## 3. 保留回归

V4.1/V4.2 全部测试保留：ADB 设备控制、应用打开、音量/按键、播放/暂停/继续、指定影片与集数、媒体 HTTP Range、播放进度记录、Web 控制台、MCP、语音监听、定时任务、SQLite、Docker 配置、健康检查、媒体密钥、会话与确认、幂等与实例租约、取消与恢复。

唯一修改的既有断言：`test_image_version_matches_code_version` 原先硬编码 `4.0.0b8`，现改为断言 `4.3.0` 且 **镜像 tag 与代码版本精确相等**（比原来的子串匹配更严格），原因是本次版本升级。

## 4. 真实环境验收状态

| 项目 | 状态 |
|---|---|
| 生产四容器健康 | 已执行（见交付报告） |
| 真实 LLM 结构化决策 | 已执行：`local_catalog` 回答与工具调用一致 |
| 真实媒体库查询 | 已执行：35 部 / 0 电影 / 8483 集 / 0 元数据 |
| 真实电视播放验证 | 视设备状态而定，结果如实记录（PARTIAL 不算成功） |
| 手机真人麦克风 | **NOT_TESTED**（无可用手机） |
| 在线片源（fnmedia-ext） | **NOT_TESTED**（未接入，明确拒绝编造） |
| ARM64 | **NOT_TESTED** |
| 24h 长稳 | **NOT_TESTED** |

## 5. 未解决问题

- 媒体库无 NFO：类型/地区/时长缺失，题材筛选只能如实回答"无法判断"。
- 本机 LLM 单次 1.7–9s，多步循环接近预算上限；预算 14s 超时即回退确定性答案。
- 电视 ADB 在长时间会话后偶发 `dumpsys` 超时，影响播放核验与 voice 健康检查（设备侧）。
