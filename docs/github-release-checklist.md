# GitHub 发布前检查清单

这份清单用于把项目公开到 GitHub 前的最后一次审计。它不能替代组织的数据安全审查，但能降低把本机配置、企业数据或构建产物误提交的风险。

## 1. 确认公开边界

- [ ] 仓库中没有 `.env`、数据库、备份、日志、抓包文件、截图或导出 CSV。
- [ ] 所有用户、群、租户、Base、消息和 URL 都是合成示例；不能仅把真实值“部分打码”。
- [ ] `.env.example` 只包含占位符，不包含可用 ID、密码、Token 或回调域名。
- [ ] 文档、测试、Issue 和 PR 模板明确禁止粘贴生产数据。
- [ ] 第三方标识、商标和许可证声明已经复核。

在仓库根目录执行以下只读检查。命令只显示可能有风险的文件名；发现结果后先人工确认，再处理文件。

```bash
git check-ignore .env data/mentions.db backups/mentions-backup.db
rg -l --hidden \
  --glob '!.git/**' \
  --glob '!*.lock' \
  '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}|app_secret=|access_token=|refresh_token=)' .
```

PowerShell 可以使用：

```powershell
git check-ignore .env data/mentions.db backups/mentions-backup.db
rg -l --hidden --glob '!.git/**' --glob '!*.lock' 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}|app_secret=|access_token=|refresh_token=' .
```

上述检索命中不必然代表泄露：字段名、测试假数据和 `.env.example` 都可能匹配。任何不能确认是合成数据的结果都不得提交。

## 2. 运行发布前质量门禁

```bash
python -m pip install -e ".[dev]"
pre-commit run --all-files
pytest --cov=app
pip-audit --strict
docker build -t feishu-mentions-inbox:local .
```

如需运行 PostgreSQL 集成测试，设置一个只用于本机临时数据库的 `TEST_DATABASE_URL`，不要指向共享或生产数据库。完整命令见 [CONTRIBUTING](../CONTRIBUTING.md)。

## 3. 初始化并审阅 Git 历史

首次发布前，在项目根目录执行：

```bash
git init -b main
git add .
git diff --cached --check
git diff --cached --name-only
git status
```

逐项确认暂存文件只包含源码、测试、文档、许可证、配置模板和 GitHub 自动化。不要仅依赖 `.gitignore`；它不能移除曾经被 Git 跟踪过的敏感文件。

确认无误后再创建首个提交：

```bash
git commit -m "chore: prepare public repository"
```

在 GitHub 创建空仓库后，再按 GitHub 页面提供的远程地址执行 `git remote add origin ...` 和首次推送。不要在公开 Issue、提交信息或 CI 日志中粘贴 Token。

## 4. 配置 GitHub 仓库

创建仓库后，建议完成以下设置：

- [ ] 仓库描述明确为“self-hosted enterprise Feishu/Lark mentions inbox”。
- [ ] 添加 Topics：`feishu`、`lark`、`fastapi`、`bitable`、`sqlite`、`postgresql`、`docker`、`oauth`、`privacy`。
- [ ] 设定默认分支为 `main`，开启分支保护：要求 Pull Request、CI 通过、至少一次人工审阅，并限制强制推送。
- [ ] 在 Security 页面启用 Dependabot alerts、Dependabot security updates、secret scanning、push protection、CodeQL 和私密漏洞报告。
- [ ] 审核 Actions 权限，保持工作流最小权限；本项目默认只读源码，CodeQL 仅写入安全扫描结果。
- [ ] 不在 GitHub Actions Secrets 中长期保存生产 App Secret；生产部署应使用组织密钥管理服务。

本项目已经提供 CI、CodeQL、Dependabot、Issue 表单和 PR 模板。它们会在推送后生效。

## 5. 首个公开版本

- [ ] README 首页、英文摘要、截图和徽章不包含真实企业信息。
- [ ] `CHANGELOG.md` 与包版本一致。
- [ ] 先使用预发布标签，例如 `v0.1.0-alpha.1`，明确真实飞书权限和三账号隔离仍需租户验收。
- [ ] 发布说明包含已验证范围、已知限制和升级/回滚建议。
- [ ] 不上传数据库、Base 导出、事件 payload、测试账号信息或任何备份作为 Release 附件。

## 6. 发现误提交时

立即停止推送和发布，轮换可能已暴露的 Token、App Secret、回调凭证和数据库密码。即使随后删除文件，公开 Git 历史、Fork、缓存和通知中仍可能保留内容。确认影响后，再使用 GitHub 的敏感信息处理指南和组织安全流程清理历史。
