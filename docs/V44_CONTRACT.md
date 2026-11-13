# V4.4 内部接口契约（并行开发基线）

本文件是 V4.4 并行开发的**唯一接口约定**。所有模块按此实现，避免各自定义字段名。

## 真实库事实（2026-11-13 实测，勿假设不变）

- `/media` 根：2 个顶层剧目录（`师兄太稳健 (2026)`、`我不是大师 (2026)`）+ `短剧/` 下 43 个目录。
- 索引 45 条 entry，`LibraryTools.summary()` 实测：`total=35, movies=0, series=35, episodes=8483, with_metadata=0, metadata_missing=35`。
- 45 个 `tvshow.nfo`，字段覆盖：`plot` 43、`tag` 2、`studio` 43、`uniqueid` 43（全部 `type="hongguo"`）、`runtime` 0、`genre` 0。
- 因此 `with_metadata=0` 的根因是**解析器只认 `<genre>`**，不是库没有元数据。

## 一、`adbtv/library_tools.py` 投影契约

每个 item 投影（`_project`）在 V4.3.2 字段基础上**新增**：

| 字段 | 类型 | 语义 |
|---|---|---|
| `plot` | `str \| None` | NFO `<plot>` 原文，截断 300 字；无则 None（不得编造） |
| `genres` | `list[str]` | `<genre>` + `<tag>` 合并去重；无则 `[]` |
| `studio` | `str \| None` | `<studio>` 原文 |
| `kind` | `str` | `movie` / `series` / `short_drama` / `unknown` |
| `kind_label` | `str` | `电影` / `电视剧` / `短剧` / `未知` |
| `runtime_minutes` | `str \| None` | `<runtime>` 原文（保留原格式，不做换算猜测） |
| `added_days_ago` | `float \| None` | 由文件 mtime 计算，`recent()`/`browse(sort='recent')` 必填 |

`kind` 判定必须**有证据**，优先级：

1. 目录含 `movie.nfo`，或 NFO `<type>movie</type>` → `movie`
2. `<studio>` 含「短剧」，或 `<uniqueid type="hongguo">`，或路径包含 `/短剧/` → `short_drama`
3. 结构上存在 `SxxExx` 集号（≥1 集）→ `series`
4. 单文件且无集号 → `movie`
5. 其余 → `unknown`

`series` 与 `short_drama` 都属于「剧」，`summary()` 的 `series` 计数必须**只算 `kind=='series'`**，短剧单列。

### 方法签名（全部只读，返回值不得含文件系统路径）

```python
summary() -> dict      # 兼容旧键：available,total,movies,series,unknown_kind,episodes,
                       # with_metadata,metadata_missing,ambiguous_folders,ambiguous_videos,
                       # roots,index_age_seconds,errors
                       # 新增键：short_dramas, seasons, with_plot, recent_added_7d, recent_added_30d
overview() -> dict     # 面向自然语言问答的稳定视图，含 state/kind 计数/最近新增/元数据覆盖
status() -> dict       # 旧键保留，新增 short_dramas
browse(kind='any', limit=10, sort='title') -> list[dict]
                       # kind 枚举扩展为 any/movie/series/short_drama/unknown
search(query=None, genre=None, year=None, kind='any', limit=10) -> list[dict]
                       # genre 对 genres 做包含匹配；无匹配返回空列表（不得放宽成模糊乱配）
recent(limit=5, kind='any') -> list[dict]        # 按 updated_at 倒序，带 added_days_ago
recommend(preferences=None, exclusions=None, limit=5, kind='any',
          max_episodes=None, exclude_genres=None, include_seen=False) -> list[dict]
                       # 每项额外带 reasons: list[str]，只能引用真实字段
                       # （如 "类型：悬疑"、"共 12 集，时长较短"、"7 天内新增"）
item(item_id) -> dict  # 全字段详情
episodes(item_id, season=None) -> dict
```

`with_metadata` 的判定改为：`plot` 或 `genres` 或 `studio` 或 `year` 任一存在即算有元数据。

## 二、`adbtv/conversation.py` 契约

保留全部现有方法与语义（`set_results` / `select` / `refine` / `set_pending` / `take_pending` /
`hydrate` / `export` / `snapshot` / `remember_turn` / `update_observation`），新增：

```python
set_candidates(tv_id, rows, source='library') -> list[dict]   # 同 set_results，别名
reference(tv_id, text) -> dict | None
    # 解析 "第二个" / "第3部" / "刚才那个" / "上一个" / "这个" / "它" / 片名子串，
    # 命中返回候选 dict（含 id/title/kind/episodes/genres），否则 None
preferences(tv_id) -> dict
    # 稳定键：kind, genres, exclude_genres, max_episodes, min_episodes,
    # mood, exclude_ids, last_query
update_preferences(tv_id, delta) -> dict       # 合并而非覆盖；exclude_ids 取并集
note_played(tv_id, item_id, season=None, episode=None) -> None   # 记录会话内已播放
focus(tv_id) -> dict | None                    # 最近一次被引用的候选
```

约束：

- 会话上下文默认有 TTL（现有 `ttl_seconds`），过期即清空候选与偏好。
- `export()` 输出的 JSON 必须能 `hydrate()` 还原新字段；旧快照缺字段时按默认值兼容，不得抛错。
- 不保存原始 UI 文本、路径、录音、token。

## 三、`adbtv/assistant_loop.py` 契约（Lead 负责）

新增工具（`TOOL_SCHEMAS` 同步）：

| 工具 | 参数 | 说明 |
|---|---|---|
| `catalog_overview` | `{}` | 等同 `LibraryTools.overview()` |
| `catalog_recent` | `{limit:int, kind:enum}` | 最近新增 |
| `catalog_detail` | `{item_id:str}` | 单片详情（含 plot/genres/studio） |

已有 `library_summary/browse/search/recommend/episodes/playback_progress` 保持名称与参数不变。

事实台账（`grounded`）新增可验证字段：`short_dramas`、`seasons`、`plot`（只能复述观测到的 plot 片段）、
`genres`、`studio`、`runtime`、`added_days_ago`。规则不变：**模型输出 = 自然语言 + 已验证事实引用**。

## 四、并行边界（写作用域）

| 角色 | 独占文件 |
|---|---|
| media-index | `adbtv/media_index.py`、`adbtv/library_tools.py`、`tests/test_library_metadata_v44.py` |
| dialogue | `adbtv/conversation.py`、`tests/test_dialogue_context_v44.py` |
| lead | `adbtv/assistant_loop.py`、`tests/test_assistant_v44.py`、`docs/V44_*.md` |
| acceptance | `tests/test_v44_dialogue_acceptance.py`、`scripts/v44_*` |

禁止跨域修改；禁止执行任何 git 命令（提交由 Lead 统一走 `git-autosync`）；
禁止修改 `config.yaml`、`state/`、`logs/`、`deploy/data/`。
