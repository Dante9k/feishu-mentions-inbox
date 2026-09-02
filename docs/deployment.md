# 部署操作手册

这份手册面向第一次接手项目的部署人员。请按顺序执行：先在 **SQLite Lite** 模式完成 3 个账号的试点，再决定是否使用 PostgreSQL 承载正式团队。

> [!WARNING]
> 本服务处理企业内部通讯内容。`.env`、数据库备份、飞书事件原文、OAuth 链接与令牌都可能包含敏感数据；不要提交到 Git、上传到工单或粘贴到公开聊天中。试点前必须阅读 [轻量试点](lite-pilot.md) 和 [多维表格配置](bitable-setup.md)。

## 1. 选择部署模式

| 目标 | 推荐 | 适用范围 | 起步资源 |
|---|---|---|---|
| 验证飞书权限和功能 | SQLite Lite | 1 台机器、3～10 名试点用户、1 个应用容器 | 1 vCPU / 1 GiB RAM / 10 GiB 磁盘 |
| 小团队稳定使用 | SQLite Lite | 单机、低并发、可接受单机故障 | 2 vCPU / 2 GiB RAM / 20 GiB 磁盘 |
| 部门或生产扩展 | PostgreSQL | 需要更成熟备份、较多并发或未来多实例 | 2 vCPU / 4 GiB RAM / 40 GiB 磁盘 |

默认 SQLite 并非内存演示：消息、待办、outbox、配置和 OAuth Token 密文都写入 Docker volume。限制是只能运行一个应用进程，且数据库必须使用本机磁盘，不能放在 NAS、SMB/NFS、OneDrive 或同步盘。完整差异见 [数据库模式](database-backends.md)。

## 2. 开始前检查

### 2.1 必需权限和资源

- 可运行 Docker Engine 24+ 与 Docker Compose v2 的 Linux 主机，或 Docker Desktop + WSL2；
- 一个可被飞书访问的 HTTPS 域名，例如 `mentions.example.com`；
- 飞书企业自建应用管理员权限；
- 一份独立飞书多维表格的管理员权限，且支持高级权限；
- 至少三个真实测试账号：管理员、员工 A、员工 B（建议再准备 C）；
- 管理接口使用的公司内网/VPN 或 SSO 网关访问方式。

首次验证不要连接正式全员群、外部群或包含敏感业务信息的群。

### 2.2 软件与端口检查

Linux / WSL：

```bash
docker --version
docker compose version
ss -ltn | grep ':8090' || true
```

Windows PowerShell：

```powershell
docker --version
docker compose version
Get-NetTCPConnection -LocalPort 8090 -ErrorAction SilentlyContinue
```

Docker Desktop 用户请确认启用 **Use the WSL 2 based engine**。8090 被占用时，请停止冲突服务或在 `.env` 中改用未占用的 `APP_PORT`；反向代理配置也必须同步修改。

## 3. 获取项目

在部署机上执行。所有后续命令均以项目根目录为当前目录。

Linux / WSL：

```bash
sudo mkdir -p /opt/feishu-mentions-inbox
sudo chown "$USER":"$USER" /opt/feishu-mentions-inbox
git clone https://github.com/Dante9k/feishu-mentions-inbox.git /opt/feishu-mentions-inbox
cd /opt/feishu-mentions-inbox
git status
```

Windows PowerShell：

```powershell
git clone https://github.com/Dante9k/feishu-mentions-inbox.git "$HOME\feishu-mentions-inbox"
Set-Location "$HOME\feishu-mentions-inbox"
git status
```

预期输出为工作区干净。若公司使用内部代码镜像，请替换 clone 地址，但不要把内部地址、账号或令牌提交到公开仓库。

## 4. 创建生产配置

### 4.1 复制并保护 `.env`

Linux / WSL：

```bash
cp .env.example .env
chmod 600 .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
icacls .env /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

`.env` 已被 `.gitignore` 和 `.dockerignore` 排除。真实配置只能放在部署主机或组织的密钥系统，**绝不能**回填到 `.env.example`。

### 4.2 生成四个独立密钥

下列命令执行四次，分别生成 `ADMIN_API_TOKEN`、`BITABLE_CALLBACK_TOKEN`、`OAUTH_STATE_SECRET`、`TOKEN_ENCRYPTION_SECRET` 的值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

这些值不得互相复用，也不得使用飞书 App Secret 代替。`TOKEN_ENCRYPTION_SECRET` 加密已保存的 OAuth Token；上线后直接更换会导致现有用户需要重新授权。

### 4.3 填写必需变量

使用本地编辑器打开 `.env`。以下字段必须换成真实值；保留模板里的其它安全默认值：

```dotenv
APP_ENV=production
PUBLIC_BASE_URL=https://mentions.example.com
DATABASE_URL=sqlite:////data/mentions.db

ADMIN_API_TOKEN=<独立随机值>
BITABLE_CALLBACK_TOKEN=<独立随机值>
OAUTH_STATE_SECRET=<独立随机值>
TOKEN_ENCRYPTION_SECRET=<独立随机值>

FEISHU_APP_ID=<飞书应用 App ID>
FEISHU_APP_SECRET=<飞书应用 App Secret>
FEISHU_VERIFICATION_TOKEN=<飞书事件 Verification Token>
FEISHU_ENCRYPT_KEY=<飞书事件 Encrypt Key>
FEISHU_TENANT_KEY=<租户标识>

BITABLE_APP_TOKEN=<多维表格 app_token>
BITABLE_INBOX_TABLE_ID=<个人@收件箱表 ID>
BITABLE_SETTINGS_TABLE_ID=<个人设置表 ID>
BITABLE_COVERAGE_TABLE_ID=<群覆盖管理表 ID>
BITABLE_USERS_TABLE_ID=<启用用户管理表 ID>
BITABLE_USER_ROLE_ID=<普通员工高级权限 role_id>
```

只检查是否仍有模板占位符（不要打印实际配置）：

```bash
grep -nE 'replace-me|replace-with|your-[a-z-]+|mentions\.example\.com|xxxxxxxx' .env \
  && echo '请替换上方占位符' \
  || echo '未发现常见占位符'
```

如果有输出，先修正配置。请勿把完整 `.env` 发给任何人排查。

## 5. 配置飞书和多维表格

启动服务前后都可以准备本节，但只有 HTTPS 服务可访问时才能通过飞书的 URL challenge。

1. 依照 [飞书应用配置](feishu-setup.md) 创建企业自建应用；先将应用可用范围限制到测试账号。
2. 依照 [多维表格配置](bitable-setup.md) 建立四张表、普通员工高级权限角色和三条 HTTPS 自动化。
3. 将 App ID、App Secret、Verification Token、Encrypt Key、Base/表/角色 ID 写入 `.env`。
4. 在飞书后台配置 OAuth 回调地址：

   ```text
   https://你的域名/auth/feishu/callback
   ```

5. 准备事件订阅地址：

   ```text
   https://你的域名/integrations/feishu/events
   ```

首次不要将机器人加入大量业务群。只使用一个内部测试群，且确认事件订阅已验证成功后再进行下一步。

## 6. 启动 SQLite Lite 服务

### 6.1 检查 Compose 配置

```bash
docker compose config --quiet
```

无输出且返回成功才继续。若报缺少变量，回到第 4 节修复 `.env`。

### 6.2 构建并启动

```bash
docker compose build --pull
docker compose up -d
docker compose ps
```

预期 `app` 先显示 `starting`，最多约两分钟后显示 `healthy`。查看启动日志：

```bash
docker compose logs --tail=100 -f app
```

按 `Ctrl+C` 仅退出日志跟踪，不会停止容器。

### 6.3 健康检查

Linux / WSL：

```bash
curl --fail http://127.0.0.1:8090/healthz
```

Windows PowerShell：

```powershell
curl.exe --fail http://127.0.0.1:8090/healthz
```

预期响应：

```json
{"status":"ok","database":true}
```

如果返回 503 或容器反复重启，执行：

```bash
docker compose logs --tail=200 app
docker compose ps
```

优先检查 `.env` 中的必填字段、端口、磁盘空间和数据库 URL。不要通过关闭 `FEISHU_REQUIRE_SIGNATURE`、改成开发模式或删除数据卷来绕过问题。

## 7. 配置 HTTPS 反向代理

Compose 只监听 `127.0.0.1:8090`，这是刻意的安全边界。必须通过企业网关、Nginx、Caddy 或云负载均衡器提供 HTTPS。公网只需要访问健康检查、飞书 OAuth/事件及多维表格回调；`/admin/*` 必须限制到内网或管理员身份。

### 7.1 Nginx 示例

替换域名、证书路径和内网网段。证书申请、公司 SSO、日志平台和 WAF 应遵循你们的内部标准。

```nginx
server {
    listen 443 ssl http2;
    server_name mentions.example.com;

    ssl_certificate     /etc/letsencrypt/live/mentions.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mentions.example.com/privkey.pem;
    client_max_body_size 2m;

    location = /healthz { proxy_pass http://127.0.0.1:8090; }

    location = /integrations/feishu/events {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 30s;
    }

    location = /auth/feishu/start { proxy_pass http://127.0.0.1:8090; }
    location = /auth/feishu/callback { proxy_pass http://127.0.0.1:8090; }
    location /integrations/bitable/ { proxy_pass http://127.0.0.1:8090; }

    location /admin/ {
        allow 10.0.0.0/8;
        deny all;
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / { return 404; }
}
```

测试并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail https://mentions.example.com/healthz
```

### 7.2 网关验收项

- `PUBLIC_BASE_URL` 与真实 HTTPS 域名完全一致，不带结尾 `/`；
- TLS 证书有效，服务器使用 NTP 同步时间；
- `/admin/*` 只从内网/VPN/SSO 可访问，且应用仍要求 `ADMIN_API_TOKEN`；
- 访问日志不记录 OAuth callback 的完整 query string；
- 限制请求体不超过 2 MiB，并为回调接口设置合理限流；
- 生产环境的 `/docs`、`/redoc`、`/openapi.json` 均返回 404。

## 8. 完成飞书回调与试点验收

### 8.1 验证事件订阅

从非服务器本机的网络检查：

```bash
curl --fail https://mentions.example.com/healthz
```

成功后，在飞书后台保存事件订阅地址，确认 URL challenge 成功，再订阅以下事件：

- `im.message.receive_v1`
- `im.message.recalled_v1`
- `im.chat.member.bot.added_v1`
- `im.chat.member.bot.deleted_v1`
- `im.chat.disbanded_v1`

### 8.2 以三个账号完成首轮验证

按 [轻量试点](lite-pilot.md) 从头执行。至少确认：

1. 管理员启用 A、B、C；三人分别完成 OAuth；
2. 机器人进入一个内部测试群，覆盖状态为已覆盖；
3. 同时 @三人的一条消息生成三条独立待办；
4. A 的状态和备注不影响 B/C；
5. 只有开启“包含@所有人”的成员收到 `@所有人`；
6. 撤回后正文立即清除，已处理记录保持状态、未处理记录转忽略；
7. 重启容器后数据保留，重复事件不产生重复待办；
8. A 无法读取、搜索、导出或编辑 B 的任何记录。

第 8 项有任意失败时，立即停止扩大试点，先修复多维表格高级权限。

## 9. 改用 PostgreSQL（正式扩展时）

> [!CAUTION]
> 当前 Alpha 版本不提供 SQLite 到 PostgreSQL 的自动数据迁移。已有 SQLite 数据不能通过 CSV 手工迁移；那会损失幂等键、版本、密文 Token 和 Base 映射。

新的生产环境需要 PostgreSQL 时，在 `.env` 填写高强度、URL 安全的 `POSTGRES_PASSWORD`，然后运行：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
```

预期 `postgres` 和 `app` 都为 `healthy`。以后所有日志、停止、备份命令均带上两个 Compose 文件，例如：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs --tail=100 app postgres
```

## 10. 日常运维

### 10.1 状态、日志与重启

```bash
docker compose ps
docker compose logs --tail=200 app
docker compose logs -f app

# 只重启应用，保留数据库 volume
docker compose restart app

# 停止全部容器，仍保留 volume
docker compose stop
docker compose up -d
```

日志仅在受控系统中保存。排障时先脱敏，不要粘贴完整飞书事件、Token 或消息正文。

> [!CAUTION]
> `docker compose down -v` 会删除 SQLite/PostgreSQL 数据卷。除非已经验证可恢复备份且明确需要销毁数据，否则绝不能执行该命令。

### 10.2 SQLite 一致性备份

每日生成加密备份并复制到不同于应用主机的故障域。运行中的 SQLite 必须使用 backup API，而非直接复制 `.db` 文件：

```bash
mkdir -p backups
docker compose exec -T app python -c "import sqlite3; source=sqlite3.connect('file:/data/mentions.db?mode=ro', uri=True); target=sqlite3.connect('/tmp/mentions-backup.db'); source.backup(target); target.close(); source.close()"
docker compose cp app:/tmp/mentions-backup.db ./backups/mentions-backup.db
```

在隔离环境每月至少验证一次：

```bash
sqlite3 ./backups/mentions-backup.db 'PRAGMA integrity_check;'
```

预期输出为 `ok`。备份包含员工身份、消息正文和 OAuth Token 密文，禁止提交到 Git。

### 10.3 PostgreSQL 备份

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  pg_dump --username mentions --dbname mentions --format=custom > mentions.dump
```

加密 `mentions.dump` 并传送到独立备份存储。恢复先在隔离环境演练，使用匹配版本的 `pg_restore`；恢复前停止应用写入。

### 10.4 升级与回滚

1. 阅读 [CHANGELOG](../CHANGELOG.md)，确认升级跨度与风险；
2. 记录当前提交/镜像版本，完成并验证备份；
3. 拉取目标版本：`git fetch --tags && git checkout <版本标签>`；
4. 执行 `docker compose config --quiet`；
5. 先在预发布环境完成健康检查和三账号回归；
6. 低峰期运行 `docker compose up -d --build`；
7. 至少观察 30 分钟的健康状态、事件失败和 outbox 堆积。

回滚优先恢复旧镜像/旧提交；出现不可逆数据库变更时必须按该版本的专门说明处理，不能盲目降级。

## 11. 常见问题

| 现象 | 首先检查 | 处理方式 |
|---|---|---|
| 容器不断重启 | `docker compose logs --tail=200 app` | 检查 `.env`、端口、磁盘和数据库 URL |
| `/healthz` 为 503 | 容器日志、volume | 不要删除数据卷；修复配置后重启 app |
| URL challenge 失败 | HTTPS、域名、Verification Token、系统时间 | 从公网验证 URL；确认 NTP 同步 |
| 未收到群消息 | 机器人是否在群、事件订阅、用户授权 | 查看群覆盖；机器人入群前的消息不会补录 |
| 看到了其他员工记录 | Base 高级权限 | 立即停止试点扩容，按 [高级权限](bitable-setup.md#2-高级权限) 修复并重新验收 |
| Base 没更新但服务健康 | outbox、Base 限流、回调凭证 | 保留数据库，outbox 会重试；不要手工复制正文 |
| OAuth 失败/过期 | callback、scope、加密密钥 | 修正飞书配置后重新授权；不要随意替换 Token 加密密钥 |
| Docker 拉镜像失败 | 网络/公司镜像策略 | 在 `.env` 配置组织批准的 `PYTHON_IMAGE`/`POSTGRES_IMAGE` 后重建 |

## 12. 上线检查清单

上线前：

- [ ] `.env` 只存在于部署主机或密钥管理系统，不含模板占位符；
- [ ] HTTPS、NTP、内网管理入口、日志脱敏均已验证；
- [ ] 飞书事件、OAuth、多维表格自动化均连接测试资源并验证成功；
- [ ] 管理员、员工 A、员工 B 的行级隔离全部通过；
- [ ] 已完成至少一次加密备份和隔离恢复验证；
- [ ] 试点用户的待加机器人群均已处理或明确标为无法覆盖；
- [ ] 已记录当前版本和回滚路径。

上线后：

- [ ] 每日检查健康状态、容器重启、磁盘、失败事件与 outbox；
- [ ] 每日确认备份成功，按计划进行恢复演练；
- [ ] 每周检查 OAuth 失效、群覆盖率和待加机器人群；
- [ ] 每次扩大用户范围前，抽样重新验证多维表格行级隔离；
- [ ] 公开问题报告只使用合成数据和脱敏日志。
