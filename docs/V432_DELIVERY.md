# V4.3.2 交付报告：媒体事实校验最后一轮专项修复

版本 **4.3.2**（`adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` 一致）。
基线：V4.3.1（`git 14729e0`，本地 `agent/dev`）。
范围：**只改事实校验**（`adbtv/assistant_loop.py` + 测试/文档/版本），保留 Agent、模型路由、媒体库、ADB、MCP、语音、SQLite 与 Docker 全部能力；未新增任何大模型/语音引擎/大型模块。

---

## 1. 根因

| # | 现象 | 根因 | 复现用例（V4.3.1 上失败） |
|---|---|---|---|
| 1 | movies=0、series=35，推荐 3 部剧集，模型答“推荐了三部电影”仍 SUCCESS | `references_returned_group()` 只用返回列表长度证明“N 部”，不校验返回条目的 `kind` | `test_zero_movies_three_series_cannot_be_called_three_movies`、`test_row_count_and_real_classification_mismatch`、`test_full_loop_replaces_mislabelled_recommendation` |
| 2 | 1 部电影 1 部剧集，被说成“2 部电影” | 同上，无类型区分 | `test_one_movie_one_series_not_two_movies` |
| 3 | “100 个电影”“电影有 100 个”绕过计数 | 只识别 `部/集/个` 的部分写法，缺 `个/条/部片子` 与倒序 | `test_hundred_ge_movies_rejected`、`test_hundred_tiao_movies_rejected`、`test_reversed_order_with_quantifier_rejected`、`test_full_loop_keeps_real_movie_count` |
| 4 | 只有数量统计时，模型答“拍摄于 2099 年”“时长 120 分钟”“正在播放…”仍被接受 | 只校验片名与数量，不校验年份/类型/题材/时长/播放状态等属性 | `test_fabricated_year_with_real_title_is_rejected`、`test_year_without_metadata_is_rejected`、`test_runtime_without_metadata_is_rejected` 等 |

在 V4.3.1 源码上运行 `tests/test_assistant_v432.py`：**24 项中 11 项失败**，
见 [evidence/V432_FAILURE_REPRODUCTION.txt](evidence/V432_FAILURE_REPRODUCTION.txt)。

---

## 2. 修复（`adbtv/assistant_loop.py`）

### 2.1 结构化事实台账扩展

每次工具观测除计数外，还记录**可验证属性**：

- `lists`：每个列表结果的逐条 `kind` 组成（`['series','series',...]`）与长度/总数；
- `title_kinds`：片名 → 观测到的类型；
- `years`、`genres`/`genres_by_title`、`runtimes`；
- `playback`（播放状态/进度）、`recommend_reasons`。

### 2.2 数量 × 类型共同校验

新增 `_group_count_ok(ledger, field, value)`：分组声明（“推荐/挑了/这三部…”）的 N
必须由**真实为对应类型的返回行**满足：

- `N 部电影` → 返回行中 `kind=movie` 的数量 ≥ N；
- `N 部剧集` → `kind=series` 的数量 ≥ N；
- `N 部作品` / 裸 `N 部` → 任意作品行数 ≥ N；
- 类型 `unknown` 不能被说成电影/剧集。

`_count_supported()` 仍负责库级计数（summary/browse）；两者都不通过才拒绝。

### 2.3 量词与词序

`_COUNT_BEFORE` / `_COUNT_AFTER` / `_BARE_WORK` 支持 `部 | 个 | 条`、`片子`，
以及“电影有 100 个”“电影数量为 0 部”等连接词/词序；数值仍绑定字段，不放宽字段。

### 2.4 属性证据校验

`_attributes_grounded()` 在片名/计数之后校验：

| 属性 | 规则 |
|---|---|
| 上映年份 | 文本中的 `YYYY年` 必须在观测年份集合中 |
| 媒体类型 | “《片名》是电影/剧集…”必须匹配该片名观测 kind；“类型未知”只能称未知 |
| 题材/类型标签 | 仅在“题材是X / X片 / X剧”等显式声明时检查，X 必须在观测 genres 中；允许“没有类型标签/无法判断” |
| 时长 | `N 分钟/N 小时` 必须匹配观测 runtime |
| 播放状态/进度 | “正在播放/正在观看/播放中”需 `playback.available && playing`；百分比进度需真实 playback |
| 推荐依据/其他 | `评分/豆瓣/票房/IMDb/获奖/口碑/导演/主演/演员/编剧` 工具从不返回 → 拒绝 |

任何属性无法由观测证明时，回退到**原始工具数据的确定性渲染**（不是继续加正则）。
「自然语言表达 + 已验证事实引用」：数量与属性由工具填充，模型只组织语言。

---

## 3. 修改前后行为

| 场景（真实：movies=0, series=35） | V4.3.1 | V4.3.2 |
|---|---|---|
| 模型：“推荐了三部电影：《A》《B》《C》”（实为 3 部剧集） | SUCCESS，误称电影 | 判 ungrounded，确定性渲染 3 部真实剧集，不出现“电影” |
| 1 电影 + 1 剧集 → “2 部电影” | 通过 | 拒绝 |
| “100 个电影”（真实 1 部/0 部） | 通过 | 拒绝，回退真实计数 |
| “拍摄于 2099 年”/“时长 120 分钟”/“正在播放…”（无证据） | 通过 | 拒绝 |
| “推荐三部作品”“选第三部”“真实播放进度” | 通过 | 仍通过（不被误伤） |

---

## 4. 新增测试（`tests/test_assistant_v432.py`，24 项）

覆盖任务要求：0 电影/3 剧集误称 3 部电影、1 电影+1 剧集误称 2 部电影、
`100个电影` 等量词变化、列表条数与真实分类不一致、真实片名年份虚构、
类型未知冒充电影、合法“推荐三部作品”、合法“选第三部”，另含量词倒序、
genre/runtime/playback/recommend-reason 属性编造、合法推荐与真实进度仍通过。

---

## 5. 回归

| 项目 | 命令 | 结果 |
|---|---|---|
| 编译 | `python3 -m compileall -q adbtv` | 通过 |
| 单元测试 | `python3 -m unittest discover -s tests -p 'test_*.py'` | **1007 通过 / 0 失败**（原 983 + 新增 24；14 跳过为可选依赖） |
| 模拟电视集成 | `python3 tests/test_suite.py` | **22 通过 / 0 失败** |
| 版本一致性 | `tests.test_deployment` | 通过（断言 4.3.2） |
| 发布包审计 | `python3 scripts/package.py public --check` | 通过 |

---

## 6. 真实 NAS + 真实 LLM 验证

真实库：`total=35, movies=0, series=35, episodes=8483, metadata_missing=35`；模型 `cn:deepseek-v4.1-flash`。
脱敏记录（含工具调用、实际观测、最终回答）：[evidence/V432_REAL_LLM_VERIFICATION.json](evidence/V432_REAL_LLM_VERIFICATION.json)。

| 场景 | 工具观测 | 最终回答（脱敏） | 状态/来源 | 核对 |
|---|---|---|---|---|
| 一共有多少部电影？ | summary movies=0, series=35 | 目前没有电影：35 部作品全部是剧集，8483 集 | SUCCESS/local_catalog | 正确，未把剧集当电影 |
| 有多少部电视剧？ | summary series=35 | 共有 35 部电视剧 | SUCCESS/local_catalog | 正确 |
| 给我推荐三部。 | recommend 3 部真实剧集 | 挑了 3 部，都是剧集，且暂无类型标签 | SUCCESS/local_catalog | 正确，未称电影 |
| 现在资源库里有什么？ | summary+browse | 35 部作品、电影 0、剧集 35 | SUCCESS/local_catalog（部分措辞触发回退，事实一致） | 一致 |
| 我上次看到哪一集？ | progress available=false | 没有播放记录（如实） | SUCCESS/device_status | 正确 |
| 【受控对抗】推荐 3 剧集说成“3 部电影” | recommend 3 部剧集 | 回退为“挑了这几部：1…2…3…”（真实标题），无“电影”声明 | SUCCESS/local_catalog, fallback=ungrounded_answer | **拦截成功** |
| 【受控对抗】“100 个电影” | summary movies=0 | 回退为电影 0 部 / 剧集 35 部 | SUCCESS/local_catalog, fallback=ungrounded_answer | **拦截成功** |
| 【受控对抗】真实片名 + “2099 年” | browse 真实年份 2026 | 回退为真实条目（2026） | SUCCESS/local_catalog, fallback=ungrounded_answer | **拦截成功** |

结论：真实库 movies=0 场景下，模型**无法**把推荐剧集说成电影；数量/年份/量词编造均被拦截并回退到真实工具数据。
说明：真实 LLM 措辞偶发触发保守回退（如资源总览、无记录进度），但最终回答一律来自真实观测且正确——这是「宁可回退也不编造」的预期行为，不影响正确性。

---

## 7. Docker 更新与回滚

### 7.1 更新

```bash
cd "$HOME/ai-tv-butler"
stamp=$(date +%Y%m%dT%H%M%S); bk="$HOME/ai-tv-butler-backups/pre-v432-$stamp"
mkdir -p "$bk"; chmod 700 "$bk"
tar -czf "$bk/source-private.tar.gz" adbtv config.yaml config.example.yaml
cp deploy/data/config.yaml "$bk/config.yaml.private"
docker image tag ai-tv-butler:4.3.1 "ai-tv-butler:pre-v432-$stamp"
sha256sum deploy/data/media_key | tee "$bk/media_key.sha256"

docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler exec -T console ai-tv --version   # ai-tv 4.3.2
```

### 7.2 回滚（秒级）

```bash
docker image tag ai-tv-butler:pre-v432-<stamp> ai-tv-butler:4.3.2
docker compose -p ai-tv-butler up -d --no-build
```

### 7.3 数据

- 无数据库迁移；`media_key`、任务 JSON、会话文件格式未变；禁止 `docker compose down -v`。

---

## 8. 风险与未验证

1. 真实库 35 部全缺 NFO，`movies=0` 是真实观测；模型只能如实说“类型未知/无标签”。
2. 真实 LLM 偶发措辞（如资源总览、无记录进度）会触发保守回退到确定性渲染；事实仍正确，只是表述更固定。
3. 真实电视播放链路本轮未做真机复验（不影响事实校验改动）。
4. 属性校验只覆盖工具可能返回的元数据；不改变媒体库扫描/索引能力。
