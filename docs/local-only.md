# Windows 仅本机运行手册

本模式适合先由一名用户验证、再扩展到几名用户的本机试点：Windows 原生 Python + SQLite，不需要 Docker、WSL、公网服务器、域名或内网穿透。原来的服务器 Webhook 部署方式仍保留。

## 1. 边界先说清楚

- API 只绑定 `127.0.0.1:8090`，局域网其他电脑不能直接访问；不开放防火墙入站规则，不映射路由器端口，不使用隧道。
- 消息使用飞书官方 Python SDK 的出站长连接；机器人仍必须在目标内部群中。
- 多维表格通过出站 HTTPS 写入，每 30 秒轮询处理状态、备注、个人设置，不配置入站 HTTP 自动化。
- 本机模式的启用/停用名单由本机管理员 API 管理；“启用用户管理”表仅作为投影，不接受表内的名单变更。它不是服务端 Webhook 模式的完全等价替代。
- 首次 OAuth 必须在运行服务的同一台电脑的浏览器中完成。手机扫码登录可以用于浏览器登录确认，但最终重定向必须回到这台电脑。不能把 `127.0.0.1` 链接发送给其他电脑使用。
- **本机运行不等于所有数据仅在本机**：原消息在飞书，收件箱展示会同步至飞书多维表格；其管理员仍可查看全量记录。
- 回环监听不隔离这台电脑上的其他账户、恶意程序或管理员。需要 Windows 账户密码、磁盘加密、可信软件和目录权限；不要把数据库/密钥目录放入共享盘或云盘同步目录。
- 电脑关机、休眠、断网期间不保证收集；本项目不承诺补录缺口期间的消息。

## 2. 安装和初始化

安装 Python 3.11+，推荐 3.12。在项目根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[local]"
.\.venv\Scripts\python.exe -m app.local --init
```

初始化会新建 `.env.local`，生成三个互不相同的随机密钥，不会覆盖已有文件，也不会读取或改写服务器用的 `.env`。默认数据库为 `data/mentions-local.db`，与旧版测试数据库分开。

初始化默认启用 `LOCAL_SINGLE_USER_PILOT=true`：只允许数据库中唯一的已初始化身份重新授权，不需要 `BITABLE_USER_ROLE_ID`，也不会调用角色成员或文档协作者授权接口。尝试添加第二名用户会被拒绝。这个开关只解决首位用户的安全联调，不等于多用户行级隔离已经验收。

`.env.local` 和 `data/` 已由 Git 忽略。Windows 上创建文件继承父目录 ACL；请通过文件属性 → 安全，确认没有其他普通账户、共享组的读取权限。不要把密钥粘贴到聊天、截图或提交至 GitHub。

## 3. 先做无需飞书凭证的本机冒烟测试

```powershell
.\.venv\Scripts\python.exe -m app.local --offline
```

打开 <http://127.0.0.1:8090/docs>；健康检查是 <http://127.0.0.1:8090/healthz>。

预期 `local_only=true`、`database=true`，同时 `background_workers=false`、`receiver_process_running=false`。这是离线测试，不会收集飞书消息或同步多维表格。

另开一个 PowerShell 检查监听地址：

```powershell
netstat -ano | findstr :8090
```

监听地址必须是 `127.0.0.1:8090`，不能是 `0.0.0.0:8090` 或 `[::]:8090`。不要为修复访问问题而放开防火墙。浏览器必须使用 `127.0.0.1`，不是 `localhost` 或局域网 IP。

按 `Ctrl+C` 停止服务。正常退出会同时停止长连接子进程。

## 4. 配置飞书应用和多维表格

1. 使用独立的企业自建应用，添加机器人能力。不要复用已有 OpenClaw 机器人的应用，否则事件订阅与连接可能互相影响。
2. 按 [飞书配置手册](feishu-setup.md) 申请所需通讯录、群列表、群消息和多维表格权限。群消息权限与用户 OAuth 是不同的权限层，登录飞书客户端不能代替应用授权。
3. 在“事件与回调”中选择“使用长连接接收事件”，订阅：
   - `im.message.receive_v1`
   - `im.message.recalled_v1`
   - `im.chat.member.bot.added_v1`
   - `im.chat.member.bot.deleted_v1`
   - `im.chat.disbanded_v1`
4. 在安全设置登记回调 `http://127.0.0.1:8090/auth/feishu/callback`，逐字符保持一致。是否接受该回环地址及租户安全策略，必须在真实应用上验证；若控制台拒绝，停止这一授权路径，不以开放公网端口绕过本机边界。
5. 按 [多维表格配置手册](bitable-setup.md) 创建四张表和字段，**跳过所有 HTTP 回调自动化**。保留收件箱的“版本”和“表格修改时间”字段；后者仅追踪状态、备注，不追踪服务写入的字段。
6. 个人设置中“包含@所有人”必须是复选框。普通用户仅能编辑自己的复选框；其用户标识和权限字段只读。
7. 将应用身份加入多维表格的所需权限范围。单用户试点不授予新成员权限，首位用户须已能访问该 Base；不要把 Base 分享给其他员工。扩展到多人前，普通用户角色必须完成行级隔离验证。默认 `BITABLE_GRANT_DOCUMENT_ACCESS=false` 不主动扩大文档共享范围。
8. 在 `.env.local` 填入应用 ID、App Secret、租户 key、多维表格 app token 和四张表 ID。单用户试点把普通用户角色 ID 留空；多人模式必须填写。勿改动自动生成的加密密钥。
9. 保存/发布应用的权限和订阅配置；具体审批由企业策略决定。部分配置需要长连接先启动才允许保存，可在凭证齐全后启动并返回控制台保存。
10. 群主把新机器人加入测试内部群。机器人入群前的消息不承诺补录。

长连接依据：[飞书官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)。本项目将阻塞的 SDK 放入无窗口子进程，父进程先将消息存入数据库再确认接收；数据库失败不返回成功确认，重复投递由数据库去重。

### 4.1 可重复使用的初始化命令

在项目根目录运行以下命令，可以完成凭证录入、建表和配置检查。它们**不会代替飞书后台发布、用户授权和权限隔离验收**。

```powershell
.\.venv\Scripts\python.exe -m app.setup credentials
```

打开命令输出的临时本机网址，填写新应用的 App ID 和 App Secret。网址只绑定 `127.0.0.1:8091`，一次保存成功后关闭，未使用时最多开放 15 分钟。不要分享这个临时网址。相同应用重置密钥后可重跑；已有数据库加密密钥保持不变，不允许静默替换成另一个应用。

```powershell
.\.venv\Scripts\python.exe -m app.setup doctor --online
.\.venv\Scripts\python.exe -m app.provision
```

`doctor --online` 向飞书验证应用凭证，只报告结果和缺失配置项，不输出密钥。`credentials_valid=true` 仅表示凭证有效；`configuration_ready=true` 也不代表发布、连接、用户授权或权限隔离验收完成。

`provision` 在当前应用下创建独立多维表格和四张业务表，把标识保存到 `.env.local`。重跑会复用已保存的表并校验字段名称和类型，不会自动覆盖不一致的字段，也不会授予员工访问权限。若建表响应中断，先重跑以检查已创建的同名表；若提示 `previous_base_create_uncertain_do_not_duplicate`，必须先确认飞书端是否已创建 Base，不要清空配置后反复创建。

建表后仍须在多维表格中完成：

- 开启高级权限，配置当前访问者的行级筛选及可编辑字段，保存角色 ID。
- 将“表格修改时间”的追踪范围限定为“处理状态”和“处理备注”；初始化命令只创建系统字段，不配置这一追踪范围。
- 配置所需基础访问权限并完成三个真实账号的隔离验收。字段创建成功不等于权限安全。

### 4.2 首次订阅时的长连接探测

只有在独立新应用尚未开始真实采集、控制台要求先建立连接时，才运行：

```powershell
.\.venv\Scripts\python.exe -m app.connection_probe
```

看到 `websocket_connected=true` 后，在飞书控制台选择长连接并保存事件订阅。这个命令最多运行 10 分钟，**不保存业务事件，也不返回业务事件的成功确认**；不要在这一步把机器人加入业务群，不承诺补录探测期间的消息。它不能与正式接收器同时运行。配置完成后按 `Ctrl+C` 停止探测，再按下一节启动正式服务。

若应用后台页面无法操作，保留 `.env.local` 中的进度并停止重复点击，不要新建第二个应用、再次重置有效密钥或重建已有表。还未通过的发布、授权、字段追踪及权限隔离门禁不能跳过。

## 5. 正式启动本机试点

先停止冒烟测试进程，再运行：

```powershell
.\.venv\Scripts\python.exe -m app.local
```

缺少凭证时命令会拒绝启动，只列出缺少的配置项名称，不打印凭证值。默认正式模式不开放 `/docs`。请保留终端，不要让电脑休眠。

`/healthz` 的 `receiver_process_running=true` **只代表 SDK 子进程存活，不代表已经连接/订阅成功**。连接验收必须在控制台确认并发送一条真实测试 @消息。SDK 可能在后台重连，不能拿健康检查代替消息链路验收。

本机管理员需要携带 `.env.local` 中 `ADMIN_API_TOKEN` 调用 `/admin/users` 等管理接口。接口结构见 [API 参考](api.md)。例如，在项目根目录通过 Python 读取配置，不把凭证放在命令行参数中：

```powershell
.\.venv\Scripts\python.exe -c 'from dotenv import dotenv_values; import httpx; c=dotenv_values(".env.local"); r=httpx.post(c["PUBLIC_BASE_URL"]+"/admin/users", headers={"Authorization":"Bearer "+c["ADMIN_API_TOKEN"]}, json={"user_id":"REPLACE_WITH_EMPLOYEE_USER_ID","send_activation_message":False}); print(r.status_code)'
```

将示例 `user_id` 替换为通讯录中的真实稳定用户 ID。命令仅打印状态码，200 表示成功；不能用群 ID 或 `open_id` 代替它。

启用后，在本机浏览器访问 <http://127.0.0.1:8090/auth/feishu/start>，由该用户完成授权。服务仍校验启用名单，不会因为应用配置成功而自动激活所有员工。本机模式不会向员工发送无法在其他电脑使用的本机授权链接。默认单用户模式只接受已初始化的唯一身份，并跳过所有 Base 成员授权调用。

首次本机试点尚不知道租户 key 和首位用户 ID 时，使用一次性初始化模式：

```powershell
.\.venv\Scripts\python.exe -m app.local --offline --bootstrap-first-user
```

随后在同一台电脑的浏览器访问 <http://127.0.0.1:8090/auth/feishu/start>。只有本地 SQLite 中尚无任何用户时，首个完成 OAuth 的身份才会被初始化；租户 key 会写入私有 `.env.local`。该模式不启动消息接收或同步任务，也不授予多维表格访问。身份初始化成功后停止进程；单用户试点可在应用发布后直接正常启动并重新授权。不要在共享电脑上使用这一模式。

要扩展到第二名用户，必须先在 Base 中创建并验证当前用户行级隔离角色，填写 `BITABLE_USER_ROLE_ID`，完成至少三个真实账号的隔离测试，然后把 `LOCAL_SINGLE_USER_PILOT` 改为 `false`。不要仅关闭开关就扩大可用范围。

停用用户用 `DELETE /admin/users/{user_id}`，具体以 [API 参考](api.md) 为准。停用后同时检查多维表格访问权限移除是否成功；失败需按接口提示重试。

## 6. 验收与维护

- 在已入机器人的内部群直接 @已启用且已授权用户，确认个人待办出现；多用户提及应各有一条。
- 在多维表格修改状态、备注，等待一个轮询周期并检查主数据库状态；修改个人 `@所有人` 开关，再发新消息验证。
- 本机回调入口 `/integrations/*` 应返回 404；无凭证的管理员请求应返回 401；伪造 Host 或非本机来源应返回 403。
- 未授权、停用、未入群、外部群、普通引用不得误采集。至少三账号验证行级权限后才可扩大使用范围。
- 30 秒轮询会消耗飞书 API 配额；使用人数、数据量增加后应观察限流，调整 `RECONCILIATION_INTERVAL_SECONDS`。不能将 60 秒出现目标当作断网/限流时的保证。
- 备份：正常停止服务后，备份 `data/mentions-local.db` 和 `.env.local` 到受控加密位置。恢复必须配套原加密密钥，不能重新初始化密钥后直接复用已有令牌数据。
- 更新：停止服务，按 [升级指南](deployment.md) 更新代码和依赖，运行测试后启动；不要使用两个实例同时连接同一飞书应用来试验，平台可能把事件分发给其中一个连接。
- 强制结束主进程可能留下 SDK 子进程。重启前检查进程命令行，仅结束本项目的 `python -m app.long_connection`，不要结束其他 Python 或 OpenClaw 进程。

本机模式是受约束的试点部署，不是整机安全认证；只有完成真实授权、订阅、三账号隔离和端到端消息验证后，才能宣称真实业务已可用。
