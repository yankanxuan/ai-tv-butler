# V4.3 安全审计

范围：v4.2 → v4.3 的增量修改（`assistant_loop.py`、`library_tools.py`、`llm.py`、`voice_runtime.py`、`voice_intelligence.py`、`conversation.py`、`config.py`）。

## 1. 密钥与日志

| 检查 | 结论 |
|---|---|
| API Key / Token 写日志 | 未发现。`LLMHTTPError` 只保留状态码与前 300 字符响应体；调用方日志只记阶段与耗时 |
| 响应体泄露密钥 | `/api/config` 递归脱敏；新增诊断字段只有 model 名、工具名、耗时、来源标签 |
| 媒体密钥 | 仅比对 sha256，不进入任何回复或诊断 |
| 语音回复 | `_clean_reply` 去除控制字符，长度 ≤1000；schema 校验拒绝控制字符 |

## 2. 文件路径

- `library_tools` 只返回投影字段（id/title/year/kind/genres/region/episodes/seasons/runtime/updated_at），**不含路径、URL、文件名**。
- 测试 `test_projected_items_never_expose_filesystem_paths`、`test_catalog_items_never_leak_paths` 断言不含根目录、`/media`、扩展名。
- 媒体目录只读挂载；`library_tools` 无写入接口。

## 3. 工具与参数校验

- 工具白名单固定（7 个 `catalog_*` + 5 个别名），未知名直接 `ToolError`。
- 参数 JSON Schema 子集校验：类型、枚举、范围、长度、`additionalProperties: false`。
- 文本/参数禁用模式：`adb`、`shell`、`;`、`||`、反引号、`$(`、`http(s)://`、`file:`、`/media/`、`/vol*`、`.sqlite`、`select *`、`insert into`、`drop table`。
- 模型不能输出命令；播放只产生 proposal，经既有确认链路。

## 4. 事实与提示注入

- 模型输出的事实必须可追踪到工具观测：无观测 ⇒ 不得含片名/数量；有观测 ⇒ 片名必须精确匹配，数字必须是观测值或本次返回列表的序号。
- 在线资源视为不可信输入：未接入时一律回答"无法核实"，不编造。
- 模型返回的 `text` 也过禁用模式，避免把注入内容回显成指令。

## 5. 网络与暴露面

- 控制台仍只监听配置的局域网地址；`/api/*` 全部要求令牌。
- 本次未新增监听端口、未新增外呼地址（模型地址来自既有配置）。
- 未引入新依赖：SQLite/JSON/urllib 均为标准库。

## 6. 破坏性操作

- 只读工具自动执行；播放/切换沿用确认 + 幂等 + 实例租约 + 设备锁。
- 有副作用的操作**不做自动重试**；只有只读的模型调用与只读工具在预算内允许一次重试。
- 用户取消立即清除 pending，不执行旧指令。

## 7. 发布包

`scripts/package.py public --check` 通过：排除 `config.yaml`、`.env`、`*.token`、`*.sqlite3`、`state/*`、`logs/*`、`deploy/data/*`，并扫描局域网地址、疑似 Key、控制台令牌、凭据赋值与用户主目录路径。

## 8. 残留风险

- 单令牌部署模型：同一令牌的客户端共享历史（非多租户）。
- 幂等记录保留 7 天；过期后同 key 可新建任务。
- 电视 ADB 授权在设备侧；令牌轮换需人工。
