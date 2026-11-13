# 飞牛 NAS b8 部署、恢复检查与镜像回滚

当前状态（2026-11-12 最后一轮修复）：**b8 已实际部署**。本轮重新读取四服务 console / scheduler / media / voice，均 running/healthy，版本均 `ai-tv 4.0.0b8`，内置 healthcheck 全部通过。当前镜像 ID 为 `sha256:0fcb7301cf2bd1d4484a8be9cc8f0586c81017d41af7891335e9d5d8ca5f0341`。

11:07–11:12 的部署及镜像回滚记录见 [b8 交付报告](V408_DELIVERY.md)。最后一轮源码修复未重建生产镜像，也未再次切换镜像；本轮用强化脚本对同一真实数据备份重新做了临时恢复检查，配置、实际密钥与实际 SQLite 完整性均通过。旧的“本会话无 Docker 权限、部署待执行”描述已被此状态取代。

## 检查与部署

```bash
cd "$HOME/ai-tv-butler"
python3 -c "import adbtv; print(adbtv.__version__)"
docker compose -p ai-tv-butler ps
python3 scripts/deploy_b8.py               # 只展示计划
python3 scripts/deploy_b8.py --apply       # 需要实际部署时执行
```

部署脚本先 `docker info`；权限失败不操作数据。读取四服务的实际镜像 ID、唯一共享 `/data` 来源并比对现有媒体密钥；保存实际镜像、停止容器冷备共享数据及配置，再构建和启动。最终重新读取四服务健康、版本、运行依赖和密钥摘要；失败用记录的镜像 tag 恢复服务。`up -d` 退出 0 本身不是验收。

私有备份在 `~/ai-tv-butler-backups/pre-b8-<时间>/`，目录 0700，文件 0600。归档包含密钥、配置及会话，不应放入公开交付包。当前有效记录为 `pre-b8-20261112T110705/manifest-private.json`。

## 先验证数据可恢复性

```bash
python3 scripts/rollback_drill_b8.py \
  --manifest "$HOME/ai-tv-butler-backups/pre-b8-20261112T110705/manifest-private.json"
```

默认仅在临时目录提取归档、加载备份实际配置、验证配置所指的媒体密钥及 SQLite。密钥要求 32 字节且与清单已有摘要相符；SQLite 要求实际路径存在、有效文件头、只读 `PRAGMA integrity_check` 唯一返回 `ok`。缺少关键项、不完整备份、损坏库、错误路径及不安全归档全部失败，不得输出成功。

此命令输出 `DRILL_DRY_RUN_OK` 只说明备份数据可恢复，不代表镜像回滚已执行。最后一轮对真实 58,604,747 B 归档重新检查成功。

## 镜像回滚演练

```bash
python3 scripts/rollback_drill_b8.py \
  --manifest "$HOME/ai-tv-butler-backups/pre-b8-<时间>/manifest-private.json" \
  --apply
```

先通过严格数据恢复检查，才允许调用镜像回滚；回滚后重读四服务健康、版本和实际镜像 ID，与清单记录逐项比对。只有全部通过才可报告 `ROLLBACK_DRILL_VERIFIED`。

`deploy_b8.py --rollback <manifest>` 是较低层的镜像恢复入口，不自动做上述数据演练。两种入口均只切镜像、不覆盖在线数据。数据实际写回生产卷是独立维护操作，不能把临时恢复检查说成生产数据已写回。

必须使用清单中的实际 ID。`pre-b8-*` 是备份时间标签，**不保证镜像版本是 b7**；现有历史回滚记录实际仍报告 b8。不要靠重新打一个 `4.0.0b7` tag 冒充版本回退。若要退到 b7，需要另选经过核验的真实 b7 镜像及相容备份。

## 数据范围与验证边界

- 四服务共享 `/data`。实际密钥由 `media.state_dir`（空值由 runtime 父目录推导）决定；实际 SQLite 由 `memory.path` 及配置默认路径决定，不能靠扫描扩展名判断。
- 示例部署通常使用 `/data/media_key`、`/data/memory.sqlite3`、runtime token/session。媒体索引当前是 JSON，不应假定存在 `media.sqlite3`。
- 媒体库 `/vol1/1000/影视` 只读挂载；备份/演练不写媒体库。
- 当前生产小爱保持默认；独立 ASR 评测依赖不进入镜像。可选 Piper 的代码与声线权重许可证需分别核验，参见 [V4.1 开源选型](VOICE_V41_OPEN_SOURCE_PLAN.md)。
- 镜像构建需访问固定版本 wheel；ARM64、真实断网重连及真实独立麦克风仍待验收。

最终回归、部署观察和恢复证据统一见 [工程验收记录](B8_FINAL_ENGINEERING.md)。
