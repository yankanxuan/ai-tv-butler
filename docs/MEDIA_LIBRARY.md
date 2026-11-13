# 飞牛媒体库直连播放（V3.2）

飞牛影视与 AI 电视管家部署在同一台 NAS 上。此前播放走「ADB 抓界面 + 遥控导航」，慢且依赖页面加载。本版本改为**直接读取本机媒体库文件并让电视播放**。

## 结论

| 指令 | 旧（UI 导航） | 新（媒体库直连） |
|---|---|---|
| 播放穿成野神第一部第一集 | 81.2 秒，PARTIAL | **2.3 秒，SUCCESS** |
| 播放不存在的剧 | 12.5 秒后才失败 | **0.1 秒，UNKNOWN（明确说明未找到）** |
| 暂停 / 继续 | 2.3 秒（限飞牛） | **2.2–2.5 秒，可重复** |
| 播放野神第十季第五集 | — | **2.1 秒，SUCCESS** |
| 播放我，史前霸王龙第一集 | 解析失败 | **2.4 秒，SUCCESS** |

真机：小米 MiTV-ASTP0 / Android 9 / 192.0.2.20:5555。

## 三个技术发现

### 1. 媒体库可直接扫描（不依赖飞牛 API）

飞牛影视的 HTTP API（`/v/api/v1/...`）需要 `authx` 签名（算法已解出：`md5(secret_path_nonce_ts_md5(params)_apiKey)`）**外加用户会话令牌**。本机没有可用令牌，且不应猜密码。

因此改为直接读影视库文件系统：`/vol1/1000/影视/<分类>/<剧名 (年份)>/Season NN/<剧名> SxxExx.mp4`。实测 45 部剧、8483 集，扫描 0.06 秒。

### 2. 电视能直接播放 URL，但需要 Range 支持

小米电视内置 `com.mitv.mivideoplayer/com.mitv.videoplayer.VideoPlayerActivity` 接受 `VIEW` + `http` + `video/*`，可播放 NAS 上的文件。

但 `python -m http.server` **不支持 Range 请求**（返回 200 而非 206），播放器拿不到时长会立即退出（logcat 显示 `preparedTime:1982`、`duration:0`）。因此实现了支持 206 的只读媒体服务。

### 3. 视频渲染时 uiautomator 无法取快照

播放中 `uiautomator dump` 报 `could not get idle state`。所以：
- 直接播放模式下内容身份由**请求本身**确定（Agent 知道发的是哪个文件），不靠抓 UI；
- 播放状态只用媒体/音频证据，不碰 UI；
- 观察按目标分级，`direct_play` 走轻量路径。

## 实现

新增模块：

- `adbtv/media_index.py` — 扫描影视库，解析剧名/季/集，支持别名、口语近似、缓存。`resolve()` 返回真实文件路径；找不到或季集不存在时明确报告，不替换内容。
- `adbtv/media_server.py` — 只读 HTTP 媒体服务，支持 `Range`（206/416）、`HEAD`、路径逃逸防护、可选令牌，仅服务配置的根目录。
- `device.launch_video(url)` — 用 VIEW intent 交给电视播放器，失败明确返回而非假装成功。

集成点：

- 目标解析阶段（`_resolve_library`）就把片名映射到文件；未命中立即失败，不猜替代内容。
- `media.serve: true` 时播放动作转为 `direct_play`：起媒体服务 → 生成 URL → 电视播放 → 等真实播放证据。
- 暂停/继续按应用选择按键：飞牛与小米播放器用 DPAD_CENTER（实测），其他播放器用媒体键；已满足目标状态时为 no-op。

## 配置

```yaml
media:
  enabled: true                    # 启用媒体库索引
  roots: ["/vol1/1000/影视"]        # 只读扫描的目录
  cache: ""                        # 留空 -> state/media_index.json
  serve: true                      # 启用直连播放（起媒体服务）
  serve_host: "0.0.0.0"
  serve_port: 8899
  serve_token: ""                  # 需要时设置，电视侧用 ?token= 访问
  advertise_host: "192.0.2.10"    # 电视可访问的 NAS 地址；留空自动探测
```

默认 `media.enabled: false`：未配置媒体库的机器不受影响，仍走原 UI 导航。

## 命令

```bash
ai-tv media stats                    # 剧集/集数统计
ai-tv media search 穿成野神           # 搜索
ai-tv media resolve 野神 --season 10 --episode 5   # 解析到具体文件
ai-tv media rebuild                  # 重建索引缓存
ai-tv media serve                    # 常驻媒体服务（长期运行建议）
```

## 安全

- 媒体服务只读，仅 `GET`/`HEAD`，只服务配置的根目录，拒绝 `..` 逃逸与目录列表。
- 可选令牌（`serve_token`），校验使用常量时间比较。
- 不写入任何文件；不暴露飞牛 API 凭据；不需要飞牛账号密码。

## 在飞牛TV 内播放（本轮新增）

用户要求播放发生在飞牛TV App 内（保留它的播放记录与界面），而不是系统播放器。已确认与优化：

| 场景 | 优化前 | 优化后 |
|---|---|---|
| 已在飞牛TV 内播放一部短剧 | 24.6 秒 | **6.8 秒** |
| 冷启动飞牛TV 再播放 | — | **22.3 秒**（含应用启动） |
| 小米播放器直连同一集 | — | 2.3 秒（备选方案） |

优化点：`activate()` 原来最多 8 次 UI dump（每次约 2.4 秒）改为「点击后用页面状态确认，必要时才 dump」，`find_title` 改为整屏滑动查找。UI dump 从 19.9 秒降到 6.5 秒。

### 飞牛TV 的 DLNA renderer（已探测，未采用）

飞牛TV 注册了 `com.trim.dlna.DLNARendererService`，会作为 UPnP MediaRenderer 广播（`飞牛影视 TV MiTV-ASTP0`，端口 38833）。但实测 `SetAVTransportURI` 被接受后 `NrTracks=0`、`MediaDuration=00:00:00`、无音频——它不加载外部 HTTP URL，因此不作为播放入口。

注意：小米电视同时预装 B站的 DLNA 栈（9958 端口），两者会争抢投屏；投屏时若被 B站 接管会显示 `ProjectionPlayerActivityV2`。

### 飞牛TV 搜索（已定位，交互待完善）

飞牛TV 有 `SearchActivity`，入口是 `resource_id=search` 的图标（列表页左上角），支持「首字母或关键字」和自带屏幕键盘（A–Z、数字、`全键盘`、`系统键盘`）。

已确认：`tap` 聚焦 + `KEYCODE_DPAD_CENTER` 可逐字母输入（实测输入 `CYS` 成功显示在搜索框）。但搜索框**不接受** `adb shell input text` 或 ADBKeyboard 广播（自定义控件），因此指定剧集的自动搜索选择尚未完成，当前仍靠列表滚动查找。

### 内容身份证据（重要）

飞牛TV 播放器渲染视频时 `uiautomator` 无法取快照，播放器界面读不到片名。因此内容身份改为由**导航过程实际点击的标签**提供（`content_confirmed`：列表卡片文本、选集标签里的 `S01E01`），再由 `Verifier` 采信。这仍是应用 UI 的真实观测，不是模型推测。

### 一个已修复的严重回归

放宽简称匹配（让「穿成野神」匹配「穿成野神我靠香火斩诸神」）时，曾把「山海归途」匹配到「山海归途续集」——即**播错内容却报成功**。已加续作后缀保护（续集/前传/外传/第N季/数字开头一律不匹配），并补了回归测试。

## V3.4.1：播放链接鉴权修复

### 发现的问题

`url_for()` 生成地址时不带凭据，而 `_authorized()` 在配置了 `serve_token` 时要求凭据——一旦启用认证，电视播放必然 401。且长期共享令牌一旦泄露，等于永久可读整个媒体库。

### 修复：按文件、短时签名

- 播放地址形如 `http://<nas>:8899/<相对路径>?expires=<epoch>&sig=<hmac>`，`sig = HMAC-SHA256(secret, 相对路径 + expires)` 截断。
- 默认有效期 `link_ttl: 300` 秒，**只对该文件有效**：换路径、改签名、过期都拒绝。
- 签名与 Range 兼容：实测 `206 Partial Content` + `Content-Range` 正常，播放器可拖动进度。
- `serve_token` 保留为可选的长期令牌（仅供 API/调试），电视侧不再需要它。

### 网络范围默认收紧

- `serve_host` 默认留空 → 只监听电视可达的网卡地址，不再默认 `0.0.0.0`。
- `private_only: true` → 仅允许回环与私有网段访问，其他来源返回 403。
- 路径穿越（`..`、编码变体）与目录列表仍然拒绝。

### 实测

真机 `MiTV-ASTP0`：签名链接播放 `SUCCESS` 3.8 秒，`playing=true`；暂停/继续各约 2.3 秒。

## V3.4.2：播放会话与独立媒体服务

### 鉴权不再匿名

V3.4.1 存在绕过：**未配置 `serve_token` 时直接 `return True`**，实测无令牌、无签名也能 200 读到文件。
现在必须满足其一：

- 有效**播放会话**链接 `?sid=..&sig=..`（签名绑定单个文件）；或
- 显式配置的长期令牌（`serve_token`，`X-Auth-Token` 头或 `?token=`），校验通过。

未配置令牌 ≠ 允许匿名。`/health` 是唯一公开端点，只返回服务信息。

### 会话可续期（支持长片与拖动）

链接不再依赖固定过期时间，而是"会话 + 滑动空闲窗口"：

| 参数 | 默认 | 作用 |
|---|---|---|
| `link_ttl` | 300s | 首次访问必须在此窗口内发生，防止陈旧链接长期有效 |
| `session_idle` | 1800s | 每次授权成功都会刷新；持续播放/拖动不会中断 |

实测等价测试：模拟已播放 10 分钟、60 秒前有请求的会话，仍可 `206` 拖动；空闲超时后拒绝。

### 只有一个进程持有媒体端口

`console`、`scheduler`、`voice` 都可能执行播放任务，若各自绑定 8899 必然冲突。现在：

- 媒体服务独立运行：`ai-tv media serve`（或 Compose 的 `media` 服务）。
- 各 worker 只做两件事：探测 `/health`、用共享密钥签发 URL；**不绑定端口**。
- 签名密钥与会话文件存在运行目录，`flock` 保护，多进程一致。

实测 12 路并发播放全部 `206`。第二个实例绑定同端口会明确报错退出（退出码 2）。

## 真机验收（V3.4.2，2026-11-11）

| 项 | 结果 |
|---|---|
| 直连播放 | SUCCESS 3.1s，`playing=True/PLAYING` |
| 暂停 | SUCCESS 3.1s，`False/PAUSED` |
| 继续 | SUCCESS 3.0s，`True/PLAYING` |
| 15 分钟长片播放 5 分钟后 | 仍 `PLAYING`；同一链接 Range **206**（会话续期生效） |
| 长片暂停后 Range | **206**；继续后 **206** |
| 无播放内容时暂停 | UNKNOWN（如实报告，不假成功） |
| 冷启动首播 | 需 20s+ 出声，故 `ui.direct_play_timeout: 45` |

## 已知限制

- 依赖电视存在支持 `VIEW`+`http`+`video/*` 的播放器（本机为小米播放器）；其他电视需探测。
- 媒体服务需电视可访问 NAS 的端口；跨网段或防火墙需另行放行。
- 小米播放器不注册 MediaSession，进度只能从播放器界面读取，当前未用于核验。
- 飞牛 App 内的「继续观看」进度与直连播放不共享，从直连播放开始会从文件开头播放。
- 剧名重名、缺季缺集时明确报错；不做模糊替代。

## 测试

`tests/test_media_index.py`（6 项）、`tests/test_direct_play.py`（10 项，含 Range 206/416、令牌、路径逃逸）、`tests/test_request_plans.py` 新增逗号剧名用例。

全量：**22 项集成检查 + 288 项单元测试通过**。
