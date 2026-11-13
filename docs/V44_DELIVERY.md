# V4.4 交付说明：自然语言 AI 管家闭环

版本：`4.4.0`（基线 `4.3.2`，增量开发，未重写既有架构）
日期：2026-11-13
目标：用户不需要记固定口令，用自然中文交流即可让管家查 NAS 资源、理解连续对话、推荐真实存在的
影片，并在确认后准确控制电视。

---

## 一、真实数据基线（开发前实测，不是假设）

| 指标 | 4.3.2 实测 | 4.4.0 实测 |
|---|---|---|
| 作品总数 | 35 | 35 |
| 电影 | 0 | 0 |
| 剧集 | 35（短剧被并入） | **2** |
| 短剧 | 未区分 | **33** |
| 总集数 | 8483 | 8483 |
| 有元数据 | **0** | **35** |
| 有剧情简介 | 未统计 | **33** |
| 季数合计 | 未统计 | 42 |
| 最近 7 天新增 | 未统计 | 21 |

**根因**：真实 NFO 用 `<plot>`/`<tag>`/`<studio>`/`<uniqueid type="hongguo">`，
旧解析器只读 `<genre>`，所以 `with_metadata` 恒为 0；短剧又因为没有独立类型被计入「剧集」。

字段覆盖实测（45 个 NFO）：`plot` 43、`tag` 2、`studio` 43、`uniqueid` 43、`runtime` 0、`genre` 0。

## 二、代码变更清单

| 文件 | 变更 |
|---|---|
| `adbtv/media_index.py` | 新增 `parse_nfo` / `NfoCache` / `nfo_evidence` / `classify_kind`；五级证据类型判定 |
| `adbtv/library_tools.py` | `summary/overview/all_items/browse/search/recommend/recent/item` 扩展；投影加 `plot/genres/studio/kind_label/added_days_ago`；`reasons` 只引用真实字段 |
| `adbtv/assistant_loop.py` | 新增 `catalog_overview/catalog_recent/catalog_detail`；事实台账扩展（`short_dramas/seasons/plot_text/studios/added_days`）；plot 复述校验；`_referenced_item_id`；偏好驱动的推荐规划；工具失败返回 `UNKNOWN` |
| `adbtv/conversation.py` | `reference/preferences/update_preferences/focus_on/focus/note_played/set_candidates`；候选投影扩展；export/hydrate 兼容旧快照 |
| `adbtv/voice_runtime.py` | `LibraryVoiceCatalog` 委托 `LibraryTools`（消除重复统计）；`_reference_decision` 解析「就看刚才那个/打开这部剧第三季」；会话偏好注入助手；`catalog_detail` 后设 focus |
| `tests/*` | 新增 4 个测试文件（见「测试结果」） |
| `scripts/v44_smoke.py` | 真实库 + 真实 LLM 闭环冒烟 |
| `docs/V44_CONTRACT.md` | 并行开发接口契约（字段/签名/写作用域） |

无新增运行时依赖（NFO 解析优先 `defusedxml`，缺失时用标准库 `ElementTree`，均不解析外部实体）。

## 三、新增工具与调用链

| 工具 | 参数 | 返回要点 |
|---|---|---|
| `catalog_overview` | `{}` | 作品/电影/剧集/短剧计数、集数、季数、元数据覆盖、7/30 天新增 |
| `catalog_recent` | `{limit, kind}` | 按入库时间倒序，含 `added_days_ago` |
| `catalog_detail` | `{item_id}` | `plot`（NFO 原文，截 300 字）、`genres`、`studio`、季集结构 |

原有 `catalog_summary / catalog_status / catalog_list / catalog_search / catalog_recommend /
catalog_get_episodes / catalog_get_playback_progress` 名称与参数不变（旧名仍为别名）。

调用链（一次真实对话）：

```
用户话术
  → IntelligentVoiceRouter（规则优先，模型兜底）
  → VoiceDialogue.handle（会话上下文 + 设备观测）
  → AssistantLoop.run（多步只读工具循环，最多 4 步，14s 预算）
      → LibraryTools（overview/recent/detail/search/recommend/episodes）
      → 事实台账 grounded()：数量/年份/类型/题材/时长/播放状态/简介逐项校验
  → 通过 → 自然语言回答（含真实候选）
  → 不通过 → 确定性渲染（只用观测数据）
  → 用户「确认」→ 既有幂等 pending → revalidate → ADB 执行 → 状态验证
```

## 四、多轮对话与推荐

- 指代：`第一个/第2部/最后一个/刚才那个/上一个/这个/它/片名子串`；越界不夹边界，并列不猜。
- 偏好继承：`不要太长的`→`max_episodes`；`不要恐怖的`→`exclude_genres`；`换一个`→`exclude_ids`；
  `有没有类似的`→继承当前作品 `kind+genres`。
- 推荐理由只能来自真实字段：题材标签、集数、新增天数、出品方、是否看过。**缺题材时只给结构化理由**，
  不会凭空说「悬疑/喜剧」。
- 会话上下文有 TTL（默认 180s），过期后候选与偏好一起失效；不保存原始 UI 文本与路径。

## 五、部署与配置

1. 版本：`adbtv/__init__.py`、`pyproject.toml`、`docker-compose.yml` 统一 `4.4.0`。
2. 生产配置（`config.yaml`，不入库）新增并开启：
   ```yaml
   voice:
     intelligence: {enabled: true, llm_timeout_seconds: 20, confidence_threshold: 0.8}
     assistant:    {enabled: true, max_steps: 4, budget_seconds: 14}
   ```
   备份：`~/dsh-autosync/backups/config.yaml.bak-v44-<时间戳>`。
3. 构建与升级：`docker compose -p ai-tv-butler build && docker compose -p ai-tv-butler up -d`。
4. 四个容器（console / scheduler / media / voice）均需 healthy；`docker compose exec -T console ai-tv --version` 应为 `4.4.0`。

## 六、测试结果

| 项 | 结果 |
|---|---|
| 全套单元测试 `make test` | 见 `docs/TEST_REPORT.md` 与最终运行日志 |
| 22 项模拟电视集成测试 | 全部通过（既有，未改动） |
| 新增 `tests/test_library_metadata_v44.py` | 35 项 |
| 新增 `tests/test_dialogue_context_v44.py` | 29 项 |
| 新增 `tests/test_assistant_v44.py` | 24 项 |
| 新增 `tests/test_v44_dialogue_acceptance.py` | 15 条话术 + 对抗测试 |
| 真实库 + 真实 LLM 闭环冒烟 | `docs/evidence/V44_LEAD_CLOSED_LOOP.json` |
| 真实 LLM 验收 A–O | `docs/evidence/V44_REAL_LLM_ACCEPTANCE.json` |

真实闭环（`scripts/v44_smoke.py`，`/vol1/1000/影视` + `cn:deepseek-v4.1-flash`）：

```
现在资源库有什么？ → 共 35 部（电影 0 / 剧集 2 / 短剧 33）
推荐三部好看的。   → 3 部真实短剧 + NFO 简介理由
第二个讲什么？     → 命中第 2 部，复述其真实 plot
不要太长的。       → 只留 18 集的作品
就看刚才那个。     → 已选择《从布衣开始成圣》
第一集。           → 确认提示
确认。             → 执行 1 次，返回成功
```

延迟：P50 4.0s / P95 8.1s（本机 LLM 网关，含多步工具调用），峰值 RSS 35.8 MB，媒体扫描 644 ms。

## 七、安全与可靠性（保留并验证）

- SQLite 幂等、进程租约、崩溃恢复、取消确认、会话 TTL、登录注销：既有测试全部保留并通过。
- 播放仍需二次确认；`确认` 只接受规则来源，模型推测不能当授权。
- 确认前 `_revalidate` 重新校验片名唯一性、候选是否仍在会话中、媒体文件是否仍存在、设备状态是否已满足。
- 工具失败/未知一律 `UNKNOWN`，不盲目重复有副作用的操作。
- API Key 不回传、不入库；公开包审计排除 `config.yaml`、密钥、数据库、录音与媒体文件。

## 八、许可证与依赖核查

- 未引入新依赖；`requirements*.lock` 未变。
- NFO 解析：标准库 `ElementTree`（可选 `defusedxml`，同为宽松许可），不解析外部实体/DTD。
- 外部元数据服务：**本版本未接入**（阶段一的可选增强项留待后续，需先解决作品身份匹配与许可合规）。

## 九、回滚方案

1. 镜像回滚：`docker compose -p ai-tv-butler up -d` 前把 `docker-compose.yml` 的 image 标签改回
   `ai-tv-butler:4.3.2`（镜像仍在本地，已用 `pre-v432-*` 打过快照），再 `up -d`。
2. 配置回滚：用 `~/dsh-autosync/backups/config.yaml.bak-v44-*` 覆盖 `config.yaml` 后重启 voice 容器。
3. 代码回滚：`git checkout agent/dev` 到上一个 commit（`5f054df`）后重新构建。
4. 既有 `docs/ROLLBACK.md` 的四容器健康校验流程保持不变。

## 十、未验收项（明确标注，未伪造）

| 项 | 状态 | 原因 |
|---|---|---|
| 真人手机麦克风录音 → 上传 → ASR → 播放 | **未验收** | 需要真人持手机在 HTTPS 网关下操作；本轮未做真人测试 |
| 小米电视真机播放 | **未验收** | 冒烟与验收用 stub device/agent，确认链路走通但未在物理电视上执行 |
| TTS 首帧延迟 | 未测 | 依赖生产 TTS 服务，本轮未纳入指标 |
| 外部元数据（TMDB 等）增强 | 未实现 | 需先解决身份匹配与许可合规 |

以上项保留验收入口：`scripts/v44_smoke.py` 与 `tests/test_v44_dialogue_acceptance.py` 可直接
在真机环境复跑；真人语音链路沿用 V4.1 语音门户，验收步骤见 `docs/VOICE_CONTROL.md`。
