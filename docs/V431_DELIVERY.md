# V4.3.1 交付报告：AI 管家事实可靠性修复

版本 **4.3.1**（`adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` 一致）。
基线：V4.3.0（`~/ai-tv-butler-backups/pre-v43-20261112T233443/source-private.tar.gz`）。
范围：只修 AI 管家的事实可靠性（事实字段绑定、中文自然语言绕过、播放进度识别），
不重构 ADB、媒体服务器、SQLite、MCP、语音系统或播放执行链路；不新增语音模型。

---

## 1. 已确认的根因

| # | 现象 | 根因 | 复现用例（4.3.0 上失败） |
|---|---|---|---|
| 1 | `total=2, movies=1, series=1, episodes=4` 时，模型答"本地共有 2 部电影"仍 SUCCESS | `grounded()` 把一次运行所有字段的数字并成一个集合 `numbers`，只判断"2 是否出现过"，不判断它属于哪个字段 | `test_total_correct_but_movie_count_wrong_is_rejected` |
| 2 | 电影数正确、剧集数错误仍通过 | 同上，数字集合不区分 movies / series | `test_movie_correct_but_series_count_wrong_is_rejected` |
| 3 | 用集数冒充作品数 / 总数冒充电影数 | 同上 | `test_episode_count_is_not_a_work_count`、`test_same_number_in_two_fields_does_not_leak` |
| 4 | 模型零工具调用答"我查到十二部电影"仍 SUCCESS | `grounded()` 只认阿拉伯数字 `(\d+)(部/集/个)`，中文数字与无单位数量不被识别；无观测时只校验 `《》` | `test_chinese_numeral_fabrication_without_tool_is_rejected` |
| 5 | 无书名号的虚构片名通过 | 只匹配 `《...》`，裸片名不校验 | `test_unbracketed_fabricated_title_without_tool_is_rejected`、`test_unbracketed_fabricated_title_after_real_tool_is_rejected` |
| 6 | 工具失败后仍可为 `answer_source=local_catalog` 背书 | `answer_source()` 只看 `trace` 里出现过哪些工具名，不看该调用是否成功 | `test_failed_tool_observation_never_earns_local_catalog_source` |
| 7 | 播放进度工具成功返回 `title/season/episode`，模型答"上次看的是《三体》第2集"却被判 ungrounded → NEEDS_CLARIFICATION | 进度工具结果里的 `title/season/episode` 没有被纳入事实集合；且旧 `_facts` 只收 `items` 和顶层计数 | `test_real_playback_progress_is_accepted` |
| 8 | 进度工具无记录时不能如实作答 | `_plan` 没有进度分支、`_run_tool` 回退时未把 `state` 传入 | `test_missing_playback_progress_is_reported_honestly` |

在 4.3.0 源码上运行 `tests/test_assistant_v431.py`，20 项中 **16 项失败**，逐条对应上表缺陷：
见 [evidence/V431_FAILURE_REPRODUCTION.txt](evidence/V431_FAILURE_REPRODUCTION.txt)。

---

## 2. 修复内容

| 文件 | 修改 |
|---|---|
| `adbtv/assistant_loop.py` | 新增**结构化事实台账** `_build_ledger()`：把每个成功观测转成按字段类型化的事实（`movies/series/total/episodes/seasons/episode_numbers/season_numbers/titles`）。重写 `grounded()`：数量按对白字段绑定校验；无观测禁止任何数字/片名；裸片名（无《》）通过"动词候选 + 台账消解 + 残余中文字符"判定；无 `updated_at` 时拒绝精确时间点。`answer_source()` 只认**成功**观测。新增中文数字解析 `cn_number()`。新增 `PLAYBACK_PROGRESS_RESULT_SCHEMA` 与 `catalog_get_playback_progress.returns`；`_progress_from_state()` 区分 `device_status / recent_task / none`；新增确定性进度渲染 `_render_progress()` 与 `_plan` 进度分支；回退渲染时把 `state` 传给工具。 |
| `tests/test_assistant_v431.py` | 新增 20 项对抗测试（见第 4 节）。 |
| `adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` | 版本 `4.3.1`。 |
| `tests/test_deployment.py` | 版本断言同步为 `4.3.1`（镜像 tag 与代码版本精确相等）。 |
| `CHANGELOG.md`、`docs/DEPLOY.md`、`docs/V431_DELIVERY.md`、`docs/evidence/*` | 变更记录、部署说明、交付与验证证据。 |

### 2.1 事实台账（核心）

每类观测只把数字写进它实际属于的字段：

- `catalog_summary` / `catalog_status` → `total / movies / series / unknown / episodes`
- `catalog_list`（`catalog_list`、`library_browse`，无 genre/query 过滤）→ 其 `total` 是真实计数，可作 `total/movies/series`；**分页条数 `len(items)` 不参与**
- `catalog_search` / `catalog_recommend` / `library_recent` → 只作 `matched`（"找到 N 部"），不作库总量
- 任何列表项 → `titles`、`item_episodes`、`seasons/season_numbers`
- `catalog_get_playback_progress` → `titles`、`episode_numbers`、`season_numbers`、`has_updated_at`

`grounded()` 的判定顺序：无观测（禁数字/片名）→ 书名号片名 → `N部<类型>` 字段绑定 →
裸 `N部`（不得用分页量）→ `N集/N季` → `第N部/个/集/季` 序号 → 精确时间（需 `updated_at`）→ 裸片名。
任何一步不能证明即返回 False，由调用方回退到**原始工具数据的确定性渲染**。

> 说明：正则只用于"抽取模型声称了什么"，真正的裁决是结构化台账；不再新增"放宽数字"的逃生口。

### 2.2 播放进度 Schema（P1）

```json
{
  "source": "device_status | recent_task | none",
  "available": true,
  "evidence": "device_observation | recent_play_record",
  "title": "真实片名",
  "season": 1, "episode": 2,
  "state": "playing", "playing": true,
  "updated_at": null
}
```

- 设备实时观测优先（`device_status`）；否则最近一次已确认播放记录（`recent_task`）；都没有则 `available=false`。
- 只有 `episode` 记录、没有 `updated_at` 时，回答中若出现 `HH:MM` / `点X分` 之类时间点会被判不可信，绝不把"第二集"解释成精确播放时间。

---

## 3. 修改前后行为

| 场景 | V4.3.0 | V4.3.1 |
|---|---|---|
| `total=2,movies=1,series=1,episodes=4`，模型答"2 部电影" | SUCCESS，返回错答 | 判 ungrounded，回退确定性渲染"电影 1 部、剧集 1 部" |
| 集数 4 被当成作品数 / 电影数 | 通过 | 拒绝 |
| 零工具答"十二部电影" | SUCCESS | 拒绝，回退真实索引（0 部电影…） |
| 无《》虚构片名 | 可能通过 | 拒绝 |
| 工具失败后回答"查询成功" | 可能 SUCCESS | `fallback_reason=tool_error`，`answer_source=unverified`，绝不假成功 |
| 真实播放进度（title/season/episode） | 误判 ungrounded → NEEDS_CLARIFICATION | 正确接受，`answer_source=device_status` |
| 无播放记录 | 可能瞎猜 | 如实"还没有记录" |
| 进度来源 | 一律 device_status | 区分 device_status / recent_task / none |

---

## 4. 新增对抗测试（`tests/test_assistant_v431.py`，20 项）

要求覆盖的九类全部包含：

1. 总数正确但电影数错误 — `test_total_correct_but_movie_count_wrong_is_rejected`
2. 电影数正确但电视剧数错误 — `test_movie_correct_but_series_count_wrong_is_rejected`
3. 中文数字编造 — `test_chinese_numeral_fabrication_without_tool_is_rejected`、`test_arabic_numeral_without_tool_is_rejected`、`test_chinese_numerals_bind_the_same_way`
4. 不带书名号的虚构片名 — `test_unbracketed_fabricated_title_without_tool_is_rejected`、`test_unbracketed_fabricated_title_after_real_tool_is_rejected`
5. 工具成功但回答引用错误字段 — `test_tool_success_but_wrong_field_answer_is_replaced`
6. 工具失败后模型仍声称查询成功 — `test_tool_failure_is_not_reported_as_success_by_the_model`、`test_failed_tool_observation_never_earns_local_catalog_source`
7. 真实播放进度被错误拒绝 — `test_real_playback_progress_is_accepted`、`test_progress_tool_reports_source_and_schema`
8. 真实播放进度不存在 — `test_missing_playback_progress_is_reported_honestly`
9. 相同数字出现在多个不同字段 — `test_same_number_in_two_fields_does_not_leak`、`test_episode_count_is_not_a_work_count`
10. 两个不同工具连续调用时事实串用 — `test_two_tools_in_one_turn_do_not_cross_contaminate`

另含 `test_list_page_size_is_not_the_library_total`（分页条数不得当总量）、
`test_progress_record_is_not_a_precise_timestamp`（仅有集号不得当时间点）、
`test_bracketed_fabricated_title_is_rejected`、`test_correct_field_bound_answer_is_accepted`。

---

## 5. 回归结果

执行环境：飞牛 NAS 本机，Python 3.11.2。

| 项目 | 命令 | 结果 |
|---|---|---|
| 编译 | `python3 -m compileall -q adbtv` | 通过 |
| 单元测试 | `python3 -m unittest discover -s tests -p 'test_*.py'` | **975 通过 / 0 失败**（原 955 + 新增 20；14 跳过为可选依赖用例） |
| 模拟电视集成 | `python3 tests/test_suite.py` | **22 通过 / 0 失败** |
| Docker Compose | `docker compose -p ai-tv-butler config --quiet` | 通过 |
| 版本一致性 | `tests.test_deployment` | 38 通过 |

> 保留原有 955 项单元测试与 22 项模拟电视集成测试，全部仍通过。

原始回归日志摘要：`Ran 975 tests ... OK (skipped=14)`；集成 `结果：22 通过 / 0 失败`。

---

## 6. 真实 LLM + 真实媒体库验证

设备：飞牛 NAS；媒体库根 `<MEDIA_ROOT>`（脱敏）；真实库观测
`total=35, movies=0, series=35, episodes=8483, metadata_missing=35`。
模型：`cn:deepseek-v4.1-flash`（真实 LLM，经本机网关）。每次都保存了工具调用、
**实际工具观测**、最终回答、状态与 `answer_source`；
**不以"模型输出 SUCCESS"作为事实正确的依据，而是逐条比对回答中的数字/片名与工具观测**。

完整脱敏记录：[evidence/V431_REAL_LLM_VERIFICATION.json](evidence/V431_REAL_LLM_VERIFICATION.json)。

| 用户语句 | 实际工具 | 实际观测（关键字段） | 最终回答（脱敏） | 状态 / 来源 | 事实核对 |
|---|---|---|---|---|---|
| 现在资源库里有什么？ | `library_summary` | total=35, movies=0, series=35, episodes=8483 | 共 35 部作品：电影 0 部、剧集 35 部、8483 集；35 部缺元数据 | SUCCESS / local_catalog | 一致 |
| 一共有多少部电影？ | `library_summary` | movies=0 | 目前有 0 部电影，另 35 部剧集、8483 集 | SUCCESS / local_catalog | 一致（未把 35 当电影数） |
| 有多少部电视剧？ | `library_summary` | series=35, episodes=8483 | 共有 35 部电视剧，合计 8483 集 | SUCCESS / local_catalog | 一致 |
| 给我推荐三部。 | `library_recommend` | 返回 3 个真实条目（total=35） | 从本地已入库内容挑了 3 部（脱敏为《作品N》，集数 1305/257/351） | SUCCESS / local_catalog（模型答案因引用分季细节被判 ungrounded，回退确定性渲染） | 一致（3 个真实条目） |
| 我上次看到哪一集？ | `catalog_get_playback_progress` | available=false | 目前没有查到播放记录，还不确定上次看到哪一集 | SUCCESS / device_status | 一致（如实说明无记录） |
| 我上次看到哪一集？（注入真实作品的设备观测） | `catalog_get_playback_progress` | available=true, source=device_status, season=3, episode=1, state=PLAYING | 你上次看到的是《作品1》第3季第1集，设备显示仍在播放中 | SUCCESS / device_status | 一致（未被误拒） |

结论：
- "一共有多少部电影" 在真实库（movies=0）下**没有**把 35 部作品当电影——P0 字段错配已修复。
- 推荐走确定性渲染仍给出 3 个真实条目，事实与观测一致。
- 真实播放进度（注入真实作品的设备观测）被正确接受，`device_status`；无记录时如实回答——P1 已修复。

限制：本轮真实库没有跨重启持久化的观看位置，"我上次看到哪一集"天然无历史，回答"无记录"是正确行为；
"注入真实作品设备观测"用于验证"有真实进度时必须正确接受"，不代表真实电视曾播放该内容。电视 ADB 本轮未做真机播放复验。

---

## 7. Docker 更新与回滚

### 7.0 本轮实际执行结果

- 已构建镜像 `ai-tv-butler:4.3.1`（173MB，镜像 ID `156348835340`），`docker compose ... build` 成功。
- 已打回滚镜像 `ai-tv-butler:pre-v431-20261113T082435`（原 4.3.0）。
- 已 `docker compose -p ai-tv-butler up -d` 更新四容器：console/media/scheduler/voice 均为 `ai-tv-butler:4.3.1`，`health=healthy`。
- `ai-tv --version` → `ai-tv 4.3.1`；容器内 `TOOL_SCHEMAS` 含 `catalog_get_playback_progress`（带 `returns`）。
- `media_key` sha256 `6300f53d…` 部署前后一致。
- 部署后容器内真实 LLM 复验见 [evidence/V431_CONTAINER_VERIFICATION.txt](evidence/V431_CONTAINER_VERIFICATION.txt)：`movies=0` 正确、`series=35` 正确、无播放记录时如实说明。

### 7.1 更新（标准步骤）

```bash
cd "$HOME/ai-tv-butler"
stamp=$(date +%Y%m%dT%H%M%S); bk="$HOME/ai-tv-butler-backups/pre-v431-$stamp"
mkdir -p "$bk"; chmod 700 "$bk"
tar -czf "$bk/source-private.tar.gz" adbtv config.yaml config.example.yaml
cp deploy/data/config.yaml "$bk/config.yaml.private"
docker image tag ai-tv-butler:4.3.0 "ai-tv-butler:pre-v431-$stamp"
sha256sum deploy/data/media_key | tee "$bk/media_key.sha256"

docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler exec -T console ai-tv --version   # ai-tv 4.3.1
```

### 7.2 回滚（秒级，镜像 tag 切换）

```bash
docker image tag ai-tv-butler:pre-v431-<stamp> ai-tv-butler:4.3.1
docker compose -p ai-tv-butler up -d --no-build
docker compose -p ai-tv-butler ps
docker compose -p ai-tv-butler exec -T console ai-tv --version   # 回到旧值
```

### 7.3 源码 / 配置回滚

```bash
tar -xzf "$HOME/ai-tv-butler-backups/pre-v431-<stamp>/source-private.tar.gz" -C "$HOME/ai-tv-butler"
cp "$HOME/ai-tv-butler-backups/pre-v431-<stamp>/config.yaml.private" deploy/data/config.yaml
```

### 7.4 数据

- **无需**迁移/回滚数据库：V4.3.1 未改 SQLite、任务 JSON、`media_key`、会话文件格式。
- **禁止** `docker compose down -v`（会删命名卷）。
- 部署前后应确认 `media_key` sha256 未变（见备份目录 `media_key.sha256`）。

---

## 8. 风险与未验证项

1. 媒体库 35 部全部缺 NFO，类型/年份无法判断；`movies=0` 是真实观测，不是漏算。
2. 真实 LLM 有时把"前 20 部/挑了这三部"等分页或指代写法说出，可能被判 ungrounded 而回退确定性渲染；
   最终事实仍来自工具观测（更保守、可审计），不影响正确性。
3. 无跨重启的持久化播放位置：重启后"上次看到哪一集"可能无记录，属如实说明。
4. 真实电视播放链路本轮未复验（未连接真机播放）；不影响本次事实校验改动。

---


---

## 9. 追加修复：列表候选后的单词序号回答（网页端截图复现）

**现象**（控制台）：`找一部短剧` → 管家列出"1…8"并问"请选择第几部" → 用户答 `3` →
返回"大模型响应超时。我可以直接查询本地影视库…"、`status=NEEDS_CLARIFICATION`、`route=clarify`、`intent=unknown`。

**根因（两层）**：
1. 每回合 `handle()` 先 `_observe_context()`，其 `update_observation()` 会清空 `waiting_question`；
2. 网页端每回合经 `SharedSessionStore` 执行 `ConversationManager.hydrate()`，而 `hydrate()` 无条件
   `waiting_question = None`，把共享快照里携带的挂起问题丢弃。加上 `_context_decision()` 原本只识别
   "第3部/第3个"，裸 `3`/`第三` 失去上下文后交给模型，模型 4s 超时即回退通用澄清。

**修复**：
- `adbtv/voice_runtime.py`：`handle()` 在设备观测**之前**捕获 `waiting_question`，注入 `model_context`，
  并传给 `_context_decision(pending=...)`；挂起问题为 `selection`/`episode` 时，裸阿拉伯/中文数字
  （可带 `部/个/项/集`）直接解析为"选第 N 部"或"第 N 集"，不依赖模型；越界序号提示重选。
- `adbtv/conversation.py`：`hydrate()` 在**严格校验**（`kind` 白名单 + 整数 `details`）后恢复
  `waiting_question` 并重建问题 TTL，使跨进程共享快照保留挂起问题；缺失/非法值仍清空（不复活旧问题）。

**测试**：`tests/test_pending_selection_v431.py`（8 项）
- 裸 `3` / `第三` / `第3个` 选片；选片后裸 `2` 选第 2 集并要求确认；无挂起问题时不把孤立数字当选片；越界序号要求重选；
- 共享存储（`SharedSessionStore` + `VoiceInputRegistry`）跨回合往返后，裸 `3` 与裸 `2` 仍正确解析（复现生产路径）。

**回归**：全套单元测试 **983 通过 / 0 失败**（原 975 + 8；14 跳过为可选依赖）；模拟电视集成 **22 通过 / 0 失败**。

### 9.1 本轮发布与部署（2026-11-13）

- 已用**最终源码**（含 §9 两层修复）重建镜像：`ai-tv-butler:4.3.1`，镜像 ID `68821e86c9f2`。
- 四容器 `console/media/scheduler/voice` 均为 `ai-tv-butler:4.3.1`，`health=healthy`；
  容器内 `ai-tv --version` → `ai-tv 4.3.1`；`media_key` sha256 `6300f53d…` 不变。
- 控制台真实复现（修复后，连续两次一致）：
  - `找一部短剧` → "找到这些实际入库内容：1…8…请选择第几部。"
  - `3` → `intent=select`，`已选择《从小兵开始》。你想从第几集开始？`
  - `2` → `NEEDS_CONFIRMATION`，`确认执行 播放《从小兵开始》第4季第2集 吗？`
- 回滚镜像：
  - `ai-tv-butler:pre-v431-hydrate-20261113T091148`（回退到仅含 §9 单向修复的 4.3.1，`8a8f6254`）；
  - `ai-tv-butler:pre-v431-20261113T082435`（4.3.0 基线）。
- 公开发布包：`~/ai-tv-butler-backups/ai-tv-butler-v4.3.1-20261113-public.tar.gz`（内容审计通过）。
