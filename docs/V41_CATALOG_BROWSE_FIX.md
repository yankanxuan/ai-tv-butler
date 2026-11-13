# AI-TV-Butler v4.1 Final5：资源库总览与降级提示修复

本补丁以 `ai-tv-butler-v4.1-final5-20261112-prod-public.tar.gz` 为基线，并已在当前生产环境实际部署。保留原版本标识 `4.0.0b8`、原有电视控制/确认/任务状态机，不接管或改写飞牛影视服务接口。

## 现象与原因

- 「现在有哪些资源」「现在资源库里边有什么」在旧版本不属于有限意图集合；没有已确认的本地库总览工具，通常落入模型或泛化的失败回复。
- 旧版 `IntelligentVoiceRouter._fallback` 对“模型没启用、繁忙、超时、出错、JSON Schema 无效”统一返回「请说具体的片名...」，用户看不到真正失败原因。
- `LibraryVoiceCatalog.search()` 最多返回 32 个候选，不能将返回的 32 条误说成全库数量；线上资源站 / `fnmedia-ext` 尚未与 AI-TV-Butler 的只读查询接口建立连接。

## 修复范围

1. 引入模型可识别、schema 校验的只读 `browse_library` 意图，支持 `kind=any/movie/series` 和 `scope=local/online/all`。常见资源盘点语句离线也能快速路由；更口语化的表达可由模型按新 schema 分类。
2. 添加本地资源 `overview`：按真实索引分组统计，核查至少一条真实存在的媒体文件路径，最多展示 8 部带 ID 的真实候选；可继续说「第二部」进行选择，不会自动播放。类别按既有路径里的「电影」目录推断，并不是第三方元数据保证。
3. 模型失败回复区分未配置、忙碌、超时、API 异常、schema 不符合协议。诊断 API 返回 `decision.reason` 和 `decision.source`，不记录用户原话/密钥。
4. 在线源请求明确说明尚无在线资源站清单查询工具，不能编造片名/数量。**本补丁不提供 fnmedia-ext 在线 API 接入**，也不代表飞牛已安装该扩展。
5. 保留旧 `pause/resume/open/query_media/query_app` 和危险操作人工确认机制。此补丁主要针对 `VoiceDialogue`（控制台语音文本入口）；`ai-tv cmd` CLI 的 Agent 规划不是同一入口，未做总览扩展。

## 在真实飞牛 NAS 上安装（先备份）

基线为 Final5 生产版本。如果已经对这些 Python 文件进行过私人修改，请先人工审查补丁差异，不要覆盖你自己的改动。公开文件不含 `.env`/密钥/真实 NAS 的数据库。

```bash
cd "$HOME/ai-tv-butler"
set -e
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p "$HOME/ai-tv-butler-backups"
umask 077
# 不改动数据目录，单独保存被更新的源文件
backup="$HOME/ai-tv-butler-backups/catalogfix-$stamp.tar.gz"
tar -czf "$backup" adbtv/voice_intelligence.py adbtv/voice_runtime.py
# 保留当前已在 NAS 上的生产镜像，方便回滚
docker image tag ai-tv-butler:4.0.0b8 "ai-tv-butler:pre-catalogfix-$stamp"

# 将补丁包放在 NAS 后替换 /path/to 的实际路径
tar -tzf /path/to/ai-tv-butler-v4.1-final5-catalogfix1-patch.tar.gz
tar -xzf /path/to/ai-tv-butler-v4.1-final5-catalogfix1-patch.tar.gz -C .

# 若本机有 Python 3 与相关依赖可直接跑新增测试
action='python3 -m unittest -q tests.test_catalog_browse_v41_1 tests.test_voice_intelligence_v40 tests.test_voice_dialogue_v40'
sh -c "$action" || { echo '单测失败，禁止上线'; exit 1; }

docker compose -p ai-tv-butler config --quiet
docker compose -p ai-tv-butler build
docker compose -p ai-tv-butler up -d
docker compose -p ai-tv-butler ps
# 验证生产镜像里确实安装了新意图；不要只看版本号（原版本标识不变）
docker compose -p ai-tv-butler exec -T console python3 -c \
  'from adbtv.voice_intelligence import INTENTS; assert "browse_library" in INTENTS; print("browse_library OK")'
```

注意：生产 Compose 将 NAS 的 `${MEDIA_ROOT}` 只读挂载为**容器内 `/media`**；因此容器的持久化 `/data/config.yaml` 中 `media.roots` 应使用 `/media`（而不是宿主 `/vol1/...`），同时 `media.enabled: true`。配置修改之前先做好独立备份；不要覆盖已有 `config.yaml`、`.env`、`media_key`、会话数据或数据库。

在控制台语音/文字对话分别验证：

- 「现在有哪些资源」→ 若本地已配置且扫描成功，返回真实数量及部分片名。
- 「资源库里边有什么」「有哪些电影」→ 同样列出本地索引真实条目。
- 「在线片源有哪些」→ 明确提示暂不支持查询，而不是编造影视作品。
- 「现在播放的是什么」→ 仍走播放状态；「暂停」「继续」「打开飞牛」→ 仍保留原流程。
- 保持 `voice.intelligence.enabled: true`（保证创建语音对话处理器），即使大模型 provider 不可用，确定性本地库总览仍可用；其他复杂请求会提示实际降级原因。

若仍只得到原来的固定答复，检查控制台返回的 `decision.source` / `decision.reason`，并确认浏览器页面请求确实打到了更新后的 `VoiceDialogue` 服务；独立 `voice-portal` 的后端如果没有调用该路径，就需要单独对它的处理链做适配。

## 回滚

在原部署目录执行：

```bash
cd "$HOME/ai-tv-butler"
# 先停止，确保没有新任务进入；不要删除 /data
# stamp/backup 替换为备份时实际生成的路径和时间
docker compose -p ai-tv-butler stop
tar -xzf "$HOME/ai-tv-butler-backups/catalogfix-$stamp.tar.gz" -C .
docker image tag "ai-tv-butler:pre-catalogfix-$stamp" ai-tv-butler:4.0.0b8
docker compose -p ai-tv-butler up -d --no-build
```

## 验证边界

本补丁已在当前 NAS 实际应用：**878 项单元测试通过、0 失败、0 跳过**；**22 项模拟电视集成通过**。生产镜像已重建并部署，四服务 healthy；真实 `/media` 本地索引查询返回 35 部作品（8 个候选），在线资源请求返回 UNSUPPORTED，`query_media` 仍保持原路由。详细证据见 `docs/evidence/v41_catalogfix1_acceptance.json`。

仍未完成：Android Chrome / iPhone Safari 真人麦克风验收；fnmedia-ext 在线资源接口仍未接入，不能查询或编造在线片库。
