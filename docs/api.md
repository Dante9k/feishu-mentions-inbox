# API 参考

生产环境默认关闭 OpenAPI 页面。开发环境设置 `APP_ENV=development` 后可访问 `/docs` 或 `/redoc` 查看由代码生成的完整模型。

## 认证方式

| 接口组 | 认证 |
|---|---|
| `/integrations/feishu/events` | 飞书请求签名、时间戳、Verification Token 和事件加密 |
| `/integrations/bitable/*` | `Authorization: Bearer <BITABLE_CALLBACK_TOKEN>` |
| `/admin/*` | `Authorization: Bearer <ADMIN_API_TOKEN>`，并要求网关限制来源 |
| `/auth/feishu/*` | 短期 HMAC 签名 OAuth state 和飞书授权码 |
| `/healthz` | 无认证，只返回服务与数据库状态 |

## 飞书事件

### `POST /integrations/feishu/events`

接收飞书 URL challenge、消息、撤回和群生命周期事件。请求体上限 2 MiB；签名时间戳与服务器时间相差超过 5 分钟时拒绝。

成功响应：

```json
{"code": 0}
```

事件先幂等写入配置的 SQLite 或 PostgreSQL 主数据库，再由后台 worker 处理。HTTP 200 只表示事件已接收或已去重，不表示 Base 已同步完成。

## OAuth

### `GET /auth/feishu/start`

生成 10 分钟有效的签名 state 并 302 跳转到飞书授权页。

### `GET /auth/feishu/callback?code=...&state=...`

只有管理员启用名单中的用户可以激活。成功后授予 Base 访问、保存加密 Token、同步当前群列表并返回授权成功页面。

## Base 自动化回调

### `POST /integrations/bitable/status`

```json
{
  "item_id": "00000000-0000-0000-0000-000000000000",
  "record_id": "rec_example",
  "status": "已处理",
  "note": "已回复",
  "version": 1,
  "changed_at": 1787673600000
}
```

`status` 只能为“待处理”“处理中”“已处理”“忽略”。后端确认 record 映射属于该待办，并用版本与修改时间解决重复和乱序。

### `POST /integrations/bitable/settings`

```json
{
  "user_id": "example-user-id",
  "record_id": "rec_example",
  "include_at_all": true
}
```

### `POST /integrations/bitable/users`

```json
{
  "enabled": true,
  "record_id": "rec_example",
  "open_id": "ou_example",
  "name": "测试用户",
  "send_activation_message": true
}
```

管理员表可传稳定 `user_id`，也可传 `open_id` 由后端通过通讯录解析。

## 管理接口

### 启用用户

```bash
curl --request POST https://mentions.example.com/admin/users \
  --header "Authorization: Bearer $ADMIN_API_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"user_id":"example-user-id","send_activation_message":true}'
```

### 停用用户

```bash
curl --request DELETE \
  --header "Authorization: Bearer $ADMIN_API_TOKEN" \
  https://mentions.example.com/admin/users/{user_id}
```

### 查询用户与覆盖

- `GET /admin/users`
- `GET /admin/coverage`
- `POST /admin/coverage/run`

### 标记覆盖例外

```bash
curl --request POST https://mentions.example.com/admin/chats/oc_example/coverage-exception \
  --header "Authorization: Bearer $ADMIN_API_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"unsupported":true,"reason":"该群禁止机器人加入"}'
```

取消例外时传 `{"unsupported":false}`。

## 健康检查

`GET /healthz`

- HTTP 200：`{"status":"ok","database":true}`
- HTTP 503：`{"status":"degraded","database":false}`

该接口不检查飞书和 Base 的实时可用性；外部 API 状态应通过队列重试、失败率和最老任务时间监控。
