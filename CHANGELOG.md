# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 的结构，并计划在稳定版后遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Added

- GitHub Actions CI、Dependabot、issue/PR 模板和开源治理文件。
- 事件时间戳防重放校验、生产环境 API 文档关闭和容器最小权限配置。
- SQLite Lite 持久化后端、单容器默认部署和双数据库测试。
- GitHub 发布前检查清单、提交前格式化钩子和公开仓库安全配置说明。

### Changed

- 数据库不可用时健康检查返回 HTTP 503。
- 无法确认群属性时不再默认按内部群处理，而是等待事件重试。
- 默认快速开始切换为 SQLite；PostgreSQL 通过叠加 Compose 文件保留。
- Docker 配置检查改为静默模式，避免在终端或 CI 日志中展开环境变量。

## [0.1.0] - 2026-08-27

### Added

- 企业级多用户飞书个人 @收件箱首个可运行版本。
- PostgreSQL 主数据、幂等事件处理、outbox、多维表格投影和状态回写。
- 用户 OAuth、管理员启用名单、群覆盖检查、撤回和正文保留策略。
- Docker Compose 部署、单元测试和三账号验收文档。
