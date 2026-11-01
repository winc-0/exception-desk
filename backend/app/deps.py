import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, User
from app.permissions import ROLE_PERMISSIONS, Permission
from app.security import COOKIE_NAME, decode_session_token


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def set_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """设置当前事务的租户。

    第三个参数 true 表示只在当前事务内生效，连接归还连接池后不会把租户带给下一个请求。
    未设置时 RLS 策略匹配不到任何行，所以漏调用的结果是查不到数据，而不是看到全部数据。
    路由必须在响应前显式 commit，commit 之后这个设置就失效了，不要再访问租户表。
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@dataclass(frozen=True)
class Context:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    session_version: int

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]


_UNAUTHENTICATED = HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或会话已失效")


async def get_context(request: Request, session: SessionDep) -> Context:
    """每个请求都查库确认状态，所以停用账号、调整角色会立即生效，不用等令牌过期。"""
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_session_token(token, request.app.state.settings) if token else None
    if claims is None:
        raise _UNAUTHENTICATED

    await set_tenant(session, claims.tenant_id)
    row = (
        await session.execute(
            select(Membership.role, User.session_version)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.user_id == claims.user_id,
                Membership.tenant_id == claims.tenant_id,
                Membership.is_active,
                User.is_active,
            )
        )
    ).first()
    if row is None or row.session_version != claims.session_version:
        raise _UNAUTHENTICATED
    return Context(claims.user_id, claims.tenant_id, row.role, row.session_version)


ContextDep = Annotated[Context, Depends(get_context)]


def require(permission: Permission) -> Callable[[Context], Context]:
    def checker(ctx: ContextDep) -> Context:
        if not ctx.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "没有执行此操作的权限")
        return ctx

    return checker
