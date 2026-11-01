# 异常件协同台

面向第三方仓配公司的异常件协同平台，用来跟进破损、丢件、超时和改址。目前完成了账号、多租户隔离和角色权限，工单只有创建和查询。

技术栈：Vue 3 + TypeScript、FastAPI、SQLAlchemy（异步）、PostgreSQL 16、Alembic。

## 当前进度

| 模块 | 状态 |
| --- | --- |
| 登录、改密、退出、失败锁定 | 已完成 |
| 多租户隔离（PostgreSQL RLS） | 已完成 |
| 管理员、主管、客服、仓管四种角色 | 已完成 |
| 成员管理 | 已完成 |
| 工单创建和查询 | 已完成（仅用于验证隔离） |
| 工单流转、指派、审计日志、SLA | 未开始 |
| AI 建议（接入 Fulfill-Bot） | 未开始 |
| 公网部署、真实用户 | 未开始 |

## 本地运行

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 24。不需要 Docker，开发数据库由 Python 依赖内置。

在 `backend` 目录启动数据库、执行迁移：

```bash
uv sync
```

```bash
uv run python -m scripts.dev_db
```

```bash
uv run alembic upgrade head
```

创建公司和第一位管理员。先在 `backend/.env` 里加一行 `DESK_BOOTSTRAP_PASSWORD=至少10位的密码`，再运行：

```bash
uv run python -m scripts.create_tenant --slug demo-north --name 华北演示物流 --email admin@north.example.com --admin-name 演示管理员
```

启动后端：

```bash
uv run uvicorn app.asgi:app --port 8000
```

在 `frontend` 目录启动前端，然后打开 http://localhost:5173：

```bash
npm install
```

```bash
npm run dev
```

如果 `uv` 访问 PyPI 镜像时报 403，说明全局配置的镜像不可用。本项目的依赖锁定在官方 PyPI，可以临时取消 `UV_DEFAULT_INDEX` 环境变量后重试。

## 测试

在 `backend` 目录运行。测试跑在真实的 PostgreSQL 上，因为 RLS 是数据库行为，mock 证明不了。

```bash
uv run pytest
```

## 多租户隔离的设计

隔离不只靠应用代码里的 `WHERE tenant_id = ...`，而是由数据库兜底：

- **行级安全：** `memberships` 和 `tickets` 开启 `ENABLE` + `FORCE ROW LEVEL SECURITY`，表 owner 也受约束。
- **默认拒绝：** 没有设置当前租户时查询返回 0 行，所以代码漏设置的后果是查不到数据，而不是看到全部数据。
- **事务级设置：** 用 `set_config('app.tenant_id', ..., true)`，只在当前事务生效，连接归还连接池后不会串到下一个请求。
- **复合外键：** 工单的创建人和处理人通过 `(tenant_id, user_id)` 引用成员表，数据库层面就不能把工单指派给其他公司的人。
- **最小权限：** 运行时使用 `desk_app`，没有 `SUPERUSER` 和 `BYPASSRLS`，不能修改租户表，也没有 `DELETE` 权限；迁移用单独的 owner 账号。

`tenants` 和 `users` 是全局表，没有 RLS：登录时需要先按公司代码和邮箱查询，而且同一个人可以属于多家公司。接口只能通过成员关系读取用户信息。

## 登录会话的设计

- 令牌放在 `HttpOnly`、`SameSite=Lax` 的 Cookie 里，前端脚本读不到；生产环境默认带 `Secure`。写请求还会校验 `Origin`。
- 每个请求都查库确认账号、成员状态和角色，所以停用账号、调整角色立即生效，不用等令牌过期。
- 退出和改密时会让 `session_version` 自增，已经复制走的旧令牌也会失效。退出是全设备生效。
- 连续失败 5 次锁定 15 分钟。所有失败返回同样的提示，账号不存在时也会计算一次哈希，避免探测邮箱是否存在。

## 已知限制

- 失败锁定按账号计算，别人可以故意输错让某个账号暂时无法登录。上线前还要在反向代理层按 IP 限流。
- 成员的初始密码由管理员设置和告知，还没有邮件邀请和首次登录强制改密。
- 工单列表最多返回 100 条，还没有分页。
- `docker-compose.yml`、镜像构建和 GitHub Actions 配置还没有实际运行过。
- 容器只提供 HTTP，公网部署时要在前面加 HTTPS 反向代理；否则默认带 `Secure` 的 Cookie 发不出去，无法保持登录。
