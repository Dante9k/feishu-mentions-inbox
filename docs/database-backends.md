# 数据库模式

项目同时支持 SQLite 和 PostgreSQL。两种模式使用相同的业务模型、API、飞书配置和多维表格字段，但部署边界不同。

## 如何选择

| 场景 | 推荐后端 | 原因 |
|---|---|---|
| 本地体验、几名用户、单机试点 | SQLite | 单应用容器、无需独立数据库服务 |
| 100～200 人正式上线 | PostgreSQL | 并发写入、运维工具、备份与扩容能力更成熟 |
| 多应用实例或高可用 | PostgreSQL | 支持行锁和 `SKIP LOCKED` 任务领取 |

SQLite 模式不是内存演示：事件、待办、OAuth Token 密文、outbox 和 Base 映射都会持久化到 Docker volume。它的限制是只能运行一个应用进程，并且数据库文件必须位于应用所在主机的本地磁盘。

## SQLite Lite 模式

`.env` 中使用：

```dotenv
DATABASE_URL=sqlite:////data/mentions.db
```

启动：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Compose 把数据库保存在 `mentions_sqlite` volume。应用启动时自动建表，并启用：

- `PRAGMA journal_mode=WAL`；
- `PRAGMA foreign_keys=ON`；
- `PRAGMA busy_timeout=5000`；
- `PRAGMA synchronous=FULL`。

SQLite repository 使用单连接和 `BEGIN IMMEDIATE` 串行化写事务。不要通过 `uvicorn --workers`、多个 Compose app 副本或 Kubernetes replicas 启动多个应用进程。

不要把数据库文件放在 SMB、NFS、NAS 映射目录、OneDrive 或其他同步盘中。WAL 依赖同一主机的文件锁和共享内存语义。

## PostgreSQL 模式

保留 `.env` 中的 `POSTGRES_PASSWORD`，使用叠加 Compose 文件启动：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgres.yml \
  config --quiet

docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgres.yml \
  up -d --build
```

Windows PowerShell 可以使用同一命令并写在一行。PostgreSQL 模式覆盖应用的 `DATABASE_URL`，增加 PostgreSQL 16 服务和健康依赖。SQLite volume 仍可能显示在合并后的配置中，但不会被应用访问。

PostgreSQL repository 使用连接池、行级锁和 `FOR UPDATE SKIP LOCKED`，适合多个后台任务并发领取事件和 outbox。

## 健康检查

两种模式使用同一接口：

```bash
curl --fail http://127.0.0.1:8090/healthz
```

预期结果：

```json
{"status":"ok","database":true}
```

数据库无法访问时返回 HTTP 503。

## 备份

SQLite 应使用 SQLite backup API 创建一致性快照，不要在应用运行时直接复制 `.db`、`.db-wal` 和 `.db-shm` 中的某一个文件。参考命令见 [部署指南](deployment.md#5-数据库备份)。

PostgreSQL 使用 `pg_dump` 并定期做恢复演练。无论使用哪种后端，备份都可能包含员工身份、消息正文和 OAuth Token 密文，必须加密并限制访问。

## 后端切换

当前 Alpha 版本不会自动在 SQLite 与 PostgreSQL 之间搬迁现有数据。新试点可以直接选择 SQLite；需要切换正式生产后端时，应先停写、创建加密备份，再使用经过版本验证的迁移工具。

跨后端迁移工具列在 Roadmap 中。在工具发布前，不建议通过手工 CSV 导出迁移，因为它无法完整保留 UUID、版本、幂等键、Token 密文和 Base record 映射。
