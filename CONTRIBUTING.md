# Contributing / 参与贡献

感谢你帮助改进 Feishu Mentions Inbox。小而清晰、带测试和文档的变更最容易被合并。

## 开始之前

1. 安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。
2. 功能或行为变化先开 issue，说明使用场景、边界与兼容性影响。
3. Issue、测试数据和截图必须脱敏，禁止提交真实企业消息、成员信息和凭证。

## 本地开发

需要 Python 3.11+。建议使用独立虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pre-commit install
```

运行与 CI 相同的检查：

```bash
ruff format --check app tests
ruff check app tests
mypy app
pytest --cov=app
docker build -t feishu-mentions-inbox:dev .
```

## 代码与测试约定

- 支持 Python 3.11–3.13；公共函数和异步边界应带类型标注。
- 使用 Ruff 格式化和检查，行宽 100；不要手工调整成与格式化器冲突的风格。
- 修复缺陷时先增加能复现问题的测试；外部 API 使用 mock，不能依赖真实租户。
- 数据库变化必须可重复执行，并说明向前/回滚策略。
- 新增环境变量、飞书权限、Base 字段或接口时同步更新 README 和相关 `docs/` 页面。
- 日志只记录操作标识、计数和脱敏错误，不记录完整消息正文或令牌。

## Pull request

保持每个 PR 只解决一个主题。标题建议使用 `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` 等前缀。PR 描述应包含动机、验证结果、安全/隐私影响以及任何破坏性变化。

提交贡献即表示你同意按本项目 [MIT License](LICENSE) 发布代码，并遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
