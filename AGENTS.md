# 项目指令 — ai-tv-butler

> 用户级全局规则见 `~/.dsh/AGENTS.md`（自动加载）。本文件是本项目的补充约定。

## 开发完成后必须自动同步 GitHub

**每次完成开发任务并通过验收后，立即执行，不需要用户提醒：**

```bash
git-autosync --repo ~/ai-tv-butler
```

- 远程：`origin` → `git@github.com:yankanxuan/ai-tv-butler.git`（SSH，密钥 `~/.ssh/id_ed25519_github`）
- 自动提交目标分支：`agent/dev`（首次会自动创建并 `-u` 关联）
- `main` 是稳定版本，**永不自动提交/推送**，由人工合并
- 测试入口 `make test`（`tests/test_suite.py` + `unittest discover`），构建检查 `python3 -m compileall adbtv`
- 报告输出：`~/dsh-autosync/reports/`
- 若同时有其他改动（其他会话/人工未完成的修改）不该被提交，用 `--only` 限定路径：
  `git-autosync --only docs/x.md --only adbtv/y.py`

### 硬性禁止

- `git push --force` / `--force-with-lease` / `+branch`
- `git reset --hard` / `git clean -fd` / `git checkout -- .`
- 提交 `.env`、`config.yaml`、`state/`、`logs/`、`deploy/data/`、密钥、数据库、媒体文件
- 测试失败、冲突、认证失败时仍然上传

### 收尾汇报格式

```
分支: agent/dev
Commit: <short sha> <msg>
测试: 通过/失败 (make test)
构建: 通过/失败 (compileall)
Push: 成功/失败
远程校验: 远程 agent/dev = <sha>
报告: ~/dsh-autosync/reports/<file>.md
```

详细说明见 `docs/AUTO-SYNC.md`。
