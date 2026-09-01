# SQLite 轻量试点与功能验证

这份清单用于几名用户的首轮验证。目标是先证明部署、飞书事件、多人拆分、个人隔离、Base 回写和重启恢复都正确，再考虑扩大名单。

## 1. 准备隔离环境

建议使用独立的飞书测试应用、独立多维表格和至少三个测试账号，不要直接连接全员正式群。

```bash
cp .env.example .env
```

确认以下设置：

```dotenv
DATABASE_URL=sqlite:////data/mentions.db
RUN_BACKGROUND_WORKERS=true
FEISHU_REQUIRE_SIGNATURE=true
```

替换 `.env` 中所有域名、应用 ID、表 ID、角色 ID、`replace-me` 和随机密钥。SQLite 文件由 Docker volume 持久化，不需要配置 PostgreSQL。

## 2. 启动基础服务

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app
```

检查：

```bash
curl --fail https://你的域名/healthz
```

必须返回 HTTP 200 和：

```json
{"status":"ok","database":true}
```

如果容器反复重启，先检查 `.env` 中的必填配置和 HTTPS 回调域名，不要关闭签名校验绕过问题。

## 3. 验证飞书事件链路

1. 在飞书开放平台保存事件订阅地址；
2. 确认 URL challenge 验证成功；
3. 订阅消息接收、消息撤回、机器人进退群和群解散事件；
4. 把机器人加入一个专用内部测试群；
5. 确认日志中没有完整正文、Token 或 App Secret。

## 4. 启用测试用户

先只启用三个账号。可以通过“启用用户管理”表，也可以调用管理员接口：

```bash
curl --request POST https://你的域名/admin/users \
  --header "Authorization: Bearer $ADMIN_API_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"user_id":"测试用户的稳定 user_id"}'
```

每个账号分别打开授权入口并完成 OAuth。随后检查：

- “个人设置”出现三行；
- 授权状态为有效；
- 测试群显示“已覆盖”；
- 每个普通账号只能看到自己的设置行。

## 5. 验证多人直接 @

在测试群发送一条同时 @三个测试账号的新消息。60 秒内应满足：

- “个人 @收件箱”生成三条记录；
- 三条记录拥有不同内部待办 ID；
- 目标用户分别对应三人；
- 提及类型均为“直接@我”；
- 群名、发送人、正文摘要和时间正确。

让用户 A 把状态改为“已处理”并填写备注。用户 B、C 必须仍为“待处理”，且不能看见或修改 A 的记录。

## 6. 验证 `@所有人`

1. 仅让用户 A 开启“包含 @所有人”；
2. 用户 B、C 保持关闭；
3. 在测试群发送 `@所有人`；
4. 只应为 A 创建新待办；
5. 再直接 @A 并同时 `@所有人`，A 只能收到一条，类型为“直接@我”。

## 7. 验证撤回

发送一条 @A 和 @B 的消息，先让 A 标记“已处理”，然后撤回源消息：

- 两条记录的正文应被清除；
- 源状态应变为“已撤回”；
- A 保持“已处理”；
- B 自动变为“忽略”。

## 8. 验证持久化和恢复

记录当前收件箱数量，然后重启应用：

```bash
docker compose restart app
docker compose ps
```

重启后检查：

- `/healthz` 恢复为 200；
- 已有用户、设置、消息状态和 Base 映射没有丢失；
- 重启前后重复投递同一事件不会新增记录；
- 新 @消息仍能正常进入收件箱。

`docker compose down` 会保留 volume；不要在验证期间执行 `docker compose down -v`。

## 9. 验证权限隔离

分别登录三个普通账号，逐项检查：

- A 不能查看、搜索或导出 B、C 的收件箱记录；
- A 只能修改自己的状态和备注；
- 目标用户、正文、源状态、内部 ID 和版本均不可编辑；
- A 只能查看和修改自己的个人设置；
- 普通账号看不到“群覆盖管理”和“启用用户管理”。

任意一项失败都必须先修复 Base 高级权限，不能继续扩大试点。

## 10. 完成试点

验证结果记录在 [验收清单](acceptance-checklist.md)。建议连续观察几天，再从测试群扩展到真实内部群。扩大范围前完成一次 SQLite 加密备份和恢复演练，并确认所有目标群的机器人覆盖状态。
