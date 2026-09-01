# Security Policy / 安全策略

## Supported versions

安全修复只保证进入最新的 `main` 分支和最新发布版本。项目仍处于 `0.x` 阶段，升级前请阅读 [CHANGELOG.md](CHANGELOG.md)。

## Reporting a vulnerability

请不要通过公开 issue、讨论区、日志附件或群聊披露漏洞。优先使用 GitHub 仓库的 **Security → Report a vulnerability** 私密报告入口；如果仓库所有者尚未启用该功能，请通过其 GitHub 个人资料中公布的私密联系方式报告。

报告建议包含：

- 受影响版本或提交；
- 可复现的最小步骤和影响分析；
- 已脱敏的请求、响应或日志；
- 你建议的缓解方式（可选）。

请不要上传真实 App Secret、OAuth Token、消息正文、用户 ID、群 ID 或租户数据。维护者确认问题前，请不要在生产租户执行破坏性验证。

## Deployment responsibilities

本项目处理企业通讯内容，部署方必须自行完成飞书应用权限审查、多维表格行级隔离、TLS、密钥管理、数据库加密备份、日志脱敏和数据保留合规。默认配置不是对任何特定法规或组织安全基线的认证。
