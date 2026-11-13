# V4.1 独立片名候选 PoC

仅建议片库中真实存在的片名，不修改生产模块/配置，不猜测媒体路径，不调用 Agent、ADB 或播放，不写索引或别名。生产小爱默认保持原状。RapidFuzz 3.14.3（MIT）沿用独立评测依赖；相似度分数不是识别概率，也不是内容或播放正确率。

## 接口与输入

```python
from poc.title_candidates import TitleSuggester
suggester = TitleSuggester.from_cache('existing-media-index.json')
result = suggester.suggest('搜索穿成一神我靠香火斩诸神')
# 可选：显式给定宿主可见媒体根，只读扫描，不保存 cache
suggester = TitleSuggester.from_roots(['/mounted/media'])
```

复用 `adbtv.media_index.MediaIndex.load` / `scan`，不使用会自动写缓存的 `index_from_config`。缓存必须是 MediaIndex version 1：`entries[{base,title,year,season_hint,seasons:{season:{episode:path}}}]`，另有 roots、scanned_at。

只接受“搜索/查找/播放 + 明确片名”槽位，允许“请/帮我”和“一下”、书名号及显式“第N季”。裸片名、暂停/继续/确认、序号/集数选择、类型请求、注入及复合控制命令拒绝。空输入追问；超过256字符或非字符串抛 ValueError；片名槽位最多128字符。缓存最多16 MiB/20000条，合计最多200000个episode槽位；别名最多1000项，扫描最多8个根、深度4层。扫描可能仍耗时，应由调用方在后台/受控预算下运行，不能在高频请求中反复扫描。

## 输出与保守策略

- 原始 `hypothesis` 完整不变；`query` 是提取后的片名，不回写原文/CER。
- 返回 `index_fingerprint`、`requires_confirmation=true`、status、reason 和最多5个候选。候选仅包含稳定ID、真实标题、年份、季号、现存集数、相似度及确认标识，不含路径、目录、roots或扫描错误。
- `suggested` 仍仅为需用户确认的建议；不返回自动选中ID，不授权任何执行。`needs_clarification` 表示低分/小分差/同名多季或无匹配；`rejected` 表示不符合明确标题槽位或不安全输入。
- 使用 RapidFuzz `fuzz.ratio`，不使用容易将短片名误判满分的部分匹配；只列分数≥50候选。默认最佳分数≥70且与次名相差≥10才标 suggested。阈值是待真实数据调参的实验策略，不是已验收质量门槛。短片名同音字不会被拼音猜测自动改写。
- 候选按规范化标题/年份/季聚合；同一索引项重复出现不重复候选或episode计数。候选ID基于该语义身份生成，新增episode不改变ID；fingerprint包含现存episode身份及文件stat和已验证别名，不受缓存扫描时间、条目顺序影响。
- 同名多季且没有显式季号时追问；同名不同年份也因小分差追问。指定季不存在不会退到其他季。即使分数100也需要确认。
- 缓存episode必须具有合法季/集编号、视频扩展名且 `Path.is_file()` 当前可访问；只有空目录、已失效缓存或无媒体条目不会产生建议。它不解析视频内容，不等于验证可播放。

## 已审核别名

```python
suggester = TitleSuggester.from_cache('existing-media-index.json',
                                      aliases={'已审核别名': '真实索引标题'})
```

也可将目标设置为该快照中的稳定候选ID。目标必须对应现存候选；标题目标可以覆盖其真实多个季，但不能含混指向不同年份翻拍；禁止覆盖另一真实标题、指向不存在/无媒体条目或含路径/注入字符的别名。代码无法证明审核者身份，所以配置本身应由可信调用方提供；不得把用户随口要求自动保存成别名。

## CLI

```bash
/tmp/osfv/bin/python -m poc.title_candidates \
  --index deploy/data/media_index.json --text '搜索穿成一神我靠香火斩诸神'
/tmp/osfv/bin/python -m poc.title_candidates \
  --root /mounted/media --text '播放真实剧名第二季'
# 可选 --aliases reviewed-aliases.json
```

stdout为公开JSON；缺依赖、缓存不存在/损坏等返回2，stderr仅提供通用错误，不回显含私有路径的异常。CLI不会执行输出建议。

## 本轮验证

定向命令：`/tmp/osfv/bin/python -m unittest discover -s tests -p test_title_candidates_v41.py -v`。**30项全部通过，0跳过，1.620秒，退出0**。覆盖近似、同名多季/翻拍、低分/小分差、库外/同音负例、空/控制指令/注入、重复索引/无媒体、别名、稳定ID/fingerprint、隐私、输入规模及CLI只读行为。

现有 `deploy/data/media_index.json` 已在宿主只读调用测试：退出0，调用前后SHA-256与mtime均不变，暂停被拒绝，所有结果均需确认。但该快照的episode路径在当前宿主不可访问，严格过滤后为0个现存季候选，所以实际查询均保守追问，不宣称真实片名命中已验收。容器缓存可能使用容器挂载路径，应在同样路径可见环境运行；需要宿主扫描时显式使用from_roots。禁止通过猜测路径重写或跳过存在性验证冒充成功。

随后显式使用宿主真实媒体根进行只读 `from_roots` 扫描，未保存缓存：得到42个有现存episode的季候选，fingerprint为 `04319870318776a0db1645ba2ede9b4bd18d3512167077edb749f4418a83edd2`。近似输入“搜索穿成一神我靠香火斩诸神”找到真实片名“穿成野神我靠香火斩诸神”，相似度90.9091；因同名多季，结果为needs_clarification/ambiguous_seasons，最多展示5项且全部需确认。精确片名100分也同样要求明确季；“暂停”拒绝、库外“量子企鹅宇宙旅行”无候选。此为真实库小样本功能演练，不是纠错质量率或生产播放验收；公开结果未包含媒体文件路径。

**Phase 2 作品级会话（2026-11-12）**：新增 `poc/title_session.py`，按作品分组，季/集二级选择，候选过期，审核别名，当前播放上下文和一次性确认。它取代本文件早期“按季平铺”的演示接口；原始 `title_candidates.py` 的平铺接口仍保留用于兼容/对照。真实库同一作品现识别为 **11 季、1305 集**；早期容器只展示了 5 个季（105/118/103/102/107）是平铺接口最多 5 项的限制，不代表作品只有 5 季。实测识别“穿成一神…”得真实作品 90.9091，选择第 1 季第 1 集后才生成确认命令。

**容器内真实联调（2026-11-12）**：把生产缓存 `deploy/data/media_index.json` 以单文件只读挂到 `/index/media-index.json`，并把 `/vol1/1000/影视` 只读挂到 `/media`，实验服务显式 `--title-cache`。CTC 识别 `搜索穿成一神我靠香火斩诸神` 后，真实返回 5 个 `穿成野神我靠香火斩诸神` 季候选（相似度 90.9091、集数 105/118/103/102/107），全部 `requires_confirmation=true`，状态 `needs_clarification / ambiguous_seasons`，未自动选择或播放。缓存文件哈希在调用前后不变。脱敏证据见 `docs/evidence/v41_phase1_docker.json`。
