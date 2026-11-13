# 自动提交与 GitHub 同步（GitHub Auto-Sync）

本项目已接入 NAS 级的「开发完成后自动上传 GitHub」能力。规则与脚本位于 Agent 全局指令，本文件是项目侧说明与人工使用手册。

## 一句话用法

```bash
git-autosync --repo ~/ai-tv-butler
```

脚本在 `~/.local/bin/git-autosync`（PATH 内可直接调用）。

## 流水线（10 步，任何一步失败即中止上传）

| # | 步骤 | 失败行为 |
|---|------|----------|
| 1 | 安全前置检查：merge/rebase/cherry-pick 未完成、unmerged 冲突、detached HEAD | 中止（退出码 5） |
| 2 | 变更检测：无文件变更 | 直接结束，**不产生空提交** |
| 3 | 敏感信息扫描：路径黑名单 + 内容正则 | 中止（退出码 3） |
| 4 | 大文件告警（>5MB） | 仅告警 |
| 5 | 运行项目测试（`make test`） | 中止（退出码 2） |
| 6 | 运行构建/语法检查（`compileall`） | 中止（退出码 2） |
| 7 | 复核 `git diff --cached --stat` | — |
| 8 | 自动生成 Conventional Commit 信息 | — |
| 9 | 提交到 `agent/dev` 并推送 `origin agent/dev` | 中止（退出码 6） |
| 10 | `git ls-remote` 校验远程 SHA 与本地一致 | 仅告警 |

## 分支约定

- `main`：**稳定版本，只由人工合并**，自动同步永不直接提交或推送。
- `agent/dev`：Agent 的默认开发分支，所有自动提交落在这里。
- `pre-push` 钩子（`.git/hooks/pre-push`，模板在 `~/.dsh/git-hooks/pre-push`）会拦截：
  - 任何非快进 / 强推（覆盖远程历史）
  - 直接推送 `main` / `master`（人工确需推送时设 `ALLOW_PROTECTED_PUSH=1`）

## 敏感信息防护

`.gitignore` 已排除：`.env` 与 `.env.*`（保留 `.env.example`）、`config.yaml`、`state/`、`logs/`、`deploy/data/`、`*.pem`、`*.key`、`*.token`、`*.sqlite3`、`*.db`、`credentials.json`、`*.bak`。

扫描器额外拦截：私钥块、`ghp_*` / `github_pat_*`、`sk-*`、AWS / Google / Slack / GitLab token、JWT、带引号的长口令、含密码的数据库 DSN。

## 报告

每次成功或失败的推送都会生成 Markdown 报告：

```
~/dsh-autosync/reports/<YYYYmmdd-HHMMSS>-<repo>.md
```

内容包含：修改文件清单与增删行数、测试结果、构建结果、Commit Hash、分支、Push 结果、远程 SHA 校验、GitHub 提交链接。

## 常用参数

```bash
git-autosync --dry-run            # 只跑检查，不提交不推送
git-autosync --no-push            # 只本地提交
git-autosync -m "fix(adb): ..."   # 指定 commit 信息
git-autosync --test "make test" --build "python3 -m compileall adbtv"
git-autosync --skip-tests         # 应急跳过测试（不建议）
git-autosync --only docs/x.md --only adbtv/y.py   # 只提交指定路径（其他未完成改动不受影响）
```

## 禁止事项

`git push --force`、`git push --force-with-lease`、`git reset --hard`、`git clean -fd`、`git checkout -- .` 一律禁止使用。

## 验收记录

| 日期 | 项目 | 结果 |
|------|------|------|
| 2026-11-13 | SSH 认证（`~/.ssh/id_ed25519_github` → GitHub 账号 `yankanxuan`） | 通过 |
| 2026-11-13 | `pre-push` 守卫：推 main / 强推 / 无法验证祖先关系 | 全部按预期拦截 |
| 2026-11-13 | `git-autosync` 流水线（测试 + 扫描 + 提交 + 推送 + 远程校验） | 通过，远程 `agent/dev` 可见提交 |
