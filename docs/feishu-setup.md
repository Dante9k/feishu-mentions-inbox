# 飞书应用配置

[返回 README](../README.md) · [多维表格配置](bitable-setup.md) · [部署指南](deployment.md)

## 1. 创建和发布企业自建应用

启用机器人与网页应用能力，把应用可用范围先限制为 3 个测试账号。申请以下最小能力（开放平台后台显示名称可能随版本调整，以对应接口页面的“权限要求”为准）：

- 接收群消息、获取群组中所有消息、读取群信息和读取用户所在群列表。
- 获取用户 `user_id`、`open_id` 和基础通讯录信息；通讯录数据范围覆盖启用名单。
- 以应用身份向员工发送消息。
- 查看、编辑和管理目标多维表格。
- 管理云文档协作者，用于开通和撤销多维表格访问。
- 用户 OAuth：读取本人所在群和离线访问（默认 scope 为 `im:chat:readonly offline_access`）。

权限、事件、网页应用回调或数据范围发生变化后，需要创建新版本并完成管理员审核/发布。

## 2. 配置事件

请求地址：

```text
https://mentions.example.com/integrations/feishu/events
```

订阅事件：

- `im.message.receive_v1`
- `im.message.recalled_v1`
- `im.chat.member.bot.added_v1`
- `im.chat.member.bot.deleted_v1`
- `im.chat.disbanded_v1`

启用事件加密，将 Verification Token 和 Encrypt Key 分别写入 `.env`。生产环境保持 `FEISHU_REQUIRE_SIGNATURE=true`。服务支持 URL challenge、签名校验、5 分钟防重放窗口、AES-CBC 解密和事件 ID 幂等。服务器时间必须保持 NTP 同步。

## 3. 配置 OAuth

网页应用重定向地址必须与下列地址完全一致：

```text
https://mentions.example.com/auth/feishu/callback
```

管理员开通员工后，服务发送 `/auth/feishu/start`。回调阶段按以下顺序执行：

1. 校验签名 state 并交换 access/refresh token。
2. 用租户内稳定 `user_id` 校验管理员启用名单。
3. 授予多维表格文档访问及普通员工高级权限角色。
4. 加密保存 token，标记用户已授权。
5. 获取用户当前内部群，建立覆盖基线；只收集此后事件。

如果角色授权失败，回调返回错误且不会激活用户；员工可在问题修复后重新打开授权入口。

## 4. 群覆盖

机器人必须实际加入目标群。服务每小时比较“全部已启用且已授权用户的内部群并集”和“机器人所在群”，并显示：已覆盖、待加机器人、无法覆盖、授权失效。

机器人加入或退出、群解散会实时更新数据库和管理表。外部/跨租户群以及禁止机器人加入的群标记为“无法覆盖”，不计入首版承诺。机器人加入前的消息不会补录。

## 5. 反向代理安全

- 公网仅放行飞书事件、OAuth 回调、多维表格自动化回调和 `/healthz` 所需路径。
- `/admin/*` 在代理层限制公司内网或企业管理员认证，应用层还要求 `ADMIN_API_TOKEN`。
- TLS 在公司网关终止；不要在日志或错误页输出 App Secret、OAuth token、回调 token 或完整正文。
- 建议网关限制请求体大小、速率和来源；飞书事件仍以签名和 Verification Token 为最终信任依据。

## 官方参考

- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [机器人加入群聊说明](https://www.feishu.cn/content/389799179937)

飞书后台的权限名称和审核流程可能调整；配置时以对应开放平台接口页面显示的“权限要求”为准。
