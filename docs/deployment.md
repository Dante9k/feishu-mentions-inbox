# 部署与运维

本文提供参考生产流程。组织已有容器平台、密钥管理、数据库和网关规范时，应优先遵循内部基线。

## 1. 前置条件

- Linux 主机或容器平台；
- Docker Engine 24+、Docker Compose v2；
- 几名用户试点使用本机 SQLite volume，或为扩展部署准备 PostgreSQL 16；
- 公网 HTTPS 域名，飞书能访问回调路径；
- 可访问飞书开放平台 API；
- 已按 [飞书配置](feishu-setup.md) 和 [多维表格配置](bitable-setup.md) 准备资源。

SQLite Lite 试点可从 1 vCPU、1 GiB 内存和 10 GiB 可持久化磁盘起步；PostgreSQL 模式建议至少 2 vCPU、2 GiB 内存和 20 GiB 可持久化磁盘。实际容量取决于消息量、正文长度、Base 限流和保留周期。

数据库选择和单实例限制见 [数据库模式](database-backends.md)。

## 2. 密钥与配置

复制模板并限制文件权限：

```bash
cp .env.example .env
chmod 600 .env
```

四个服务端密钥分别生成：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

不要复用密钥。正式环境应从 Vault、云厂商 Secret Manager、Docker/Kubernetes Secret 或公司密钥系统注入，而不是长期保存在工作目录。

`TOKEN_ENCRYPTION_SECRET` 用于解密已有 OAuth Token，轮换它之前必须设计数据重加密流程；直接替换会使已有用户需要重新授权。

## 3. 启动前检查

默认 SQLite Lite：

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 app
```

PostgreSQL 模式：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
```

检查：

- App 为 healthy；PostgreSQL 模式下数据库容器也必须为 healthy；
- `https://你的域名/healthz` 返回 HTTP 200；
- 生产环境没有公开 `/docs`、`/redoc` 和 `/openapi.json`；
- `/admin/*` 从公网不可访问；
- 日志没有 Token、App Secret 或完整消息正文。

## 4. HTTPS 反向代理

Compose 默认只把服务绑定到 `127.0.0.1:8090`。以下 Nginx 片段仅展示必要原则，证书、访问日志脱敏、可信代理和公司认证需要按实际环境补充：

```nginx
server {
    listen 443 ssl http2;
    server_name mentions.example.com;

    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 30s;
    }

    location /admin/ {
        allow 10.0.0.0/8;
        deny all;
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

推荐在网关增加：

- 请求体大小与速率限制；
- 公司身份认证或 VPN；
- 安全响应头；
- OAuth callback 与事件接口的异常率告警；
- 访问日志字段白名单，避免记录 query 中的 OAuth code。

## 5. 数据库备份

至少每日加密备份，并将备份复制到独立故障域。

### SQLite

运行中的 SQLite 应通过 backup API 创建一致性快照，不要只复制一个活动 `.db` 文件：

```bash
mkdir -p backups
docker compose exec -T app python -c "import sqlite3; source=sqlite3.connect('file:/data/mentions.db?mode=ro', uri=True); target=sqlite3.connect('/tmp/mentions-backup.db'); source.backup(target); target.close(); source.close()"
docker compose cp app:/tmp/mentions-backup.db ./backups/mentions-backup.db
```

复制完成后加密备份，并通过在隔离环境打开副本、执行 `PRAGMA integrity_check` 和应用健康检查验证恢复。`/tmp` 位于临时文件系统，容器重启后自动清除。

### PostgreSQL

参考逻辑备份：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres pg_dump \
  --username mentions \
  --dbname mentions \
  --format=custom \
  > mentions.dump
```

恢复必须先在隔离环境演练。恢复时停止应用写入，使用匹配版本的 `pg_restore`，然后检查表数量、用户数量、未完成事件和 outbox 队列深度。

不要把数据库备份、导出 CSV 或 Base 快照提交到 Git；它们可能包含消息正文和员工身份。

## 6. 升级与回滚

1. 阅读 [CHANGELOG.md](../CHANGELOG.md)；
2. 备份数据库并记录当前镜像标签；
3. 在预发布环境启动新镜像并完成健康、迁移和三账号回归；
4. 低峰期部署，观察事件失败和 outbox 堆积；
5. 如需回滚，恢复旧镜像；若版本包含不可逆数据库变更，按版本说明处理。

在项目引入正式版本化迁移之前，不建议跳过多个版本升级。

## 7. 监控与告警

最低限度监控：

- `/healthz` 非 200；
- 容器重启次数、CPU、内存和磁盘；
- `incoming_events` 中 failed/processing 的数量与最老等待时间；
- `outbox_jobs` 中 failed/processing 的数量与最老等待时间；
- OAuth 授权失效人数；
- 群覆盖率下降和待加机器人群数；
- 主数据库备份成功率及磁盘增长；PostgreSQL 模式还应监控连接数。

应用日志只应发送到访问受控的日志系统。排障前先脱敏，公开 issue 中不得粘贴原始事件 payload。

## 8. 停止与卸载

停止应用但保留数据库：

```bash
docker compose stop
```

`docker compose down -v` 会删除 SQLite 或 PostgreSQL 数据卷，属于不可恢复的数据删除操作。只有在已验证备份且明确需要销毁全部数据时才可执行。PostgreSQL 模式的停止命令应同时带上两个 Compose 文件。
