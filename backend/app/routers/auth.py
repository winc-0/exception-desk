from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import ContextDep, SessionDep, set_tenant
from app.models import Membership, Tenant, User
from app.permissions import ROLE_PERMISSIONS
from app.schemas import ChangePasswordIn, LoginIn, MeOut, TenantOut
from app.security import (
    COOKIE_NAME,
    SessionClaims,
    create_session_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_LOGIN_FAILED = HTTPException(
    status.HTTP_401_UNAUTHORIZED, "公司代码、邮箱或密码错误，或账号已被临时锁定"
)


def _set_cookie(response: Response, claims: SessionClaims, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(claims, settings),
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


async def _record_failure(session: AsyncSession, user: User, settings: Settings) -> None:
    # 在 SQL 里原子地累加，并发猜密码时计数不会丢
    reached_limit = User.failed_login_count + 1 >= settings.login_max_failures
    await session.execute(
        update(User)
        .where(User.id == user.id)
        .values(
            failed_login_count=case((reached_limit, 0), else_=User.failed_login_count + 1),
            locked_until=case(
                (
                    reached_limit,
                    datetime.now(UTC) + timedelta(minutes=settings.login_lock_minutes),
                ),
                else_=User.locked_until,
            ),
        )
    )
    await session.commit()


async def _me(session: AsyncSession, user_id, tenant_id, role: str) -> MeOut:
    user = await session.get(User, user_id)
    tenant = await session.get(Tenant, tenant_id)
    return MeOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=role,
        permissions=sorted(ROLE_PERMISSIONS[role]),
        tenant=TenantOut(slug=tenant.slug, name=tenant.name),
    )


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, session: SessionDep) -> MeOut:
    settings: Settings = request.app.state.settings
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == body.tenant_slug.lower()))
    user = await session.scalar(select(User).where(User.email == body.email.strip().lower()))

    locked = user is not None and user.locked_until and user.locked_until > datetime.now(UTC)
    usable = tenant is not None and user is not None and user.is_active and not locked
    if not await verify_password(user.password_hash if usable else None, body.password):
        if usable:
            await _record_failure(session, user, settings)
        raise _LOGIN_FAILED

    await set_tenant(session, tenant.id)
    membership = await session.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.tenant_id == tenant.id,
            Membership.is_active,
        )
    )
    if membership is None:
        raise _LOGIN_FAILED

    user.failed_login_count = 0
    user.locked_until = None
    me = await _me(session, user.id, tenant.id, membership.role)
    version = user.session_version
    await session.commit()
    _set_cookie(response, SessionClaims(me.user_id, tenant.id, version), settings)
    return me


@router.get("/me")
async def me(ctx: ContextDep, session: SessionDep) -> MeOut:
    return await _me(session, ctx.user_id, ctx.tenant_id, ctx.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(ctx: ContextDep, response: Response, session: SessionDep) -> None:
    await session.execute(
        update(User).where(User.id == ctx.user_id).values(session_version=User.session_version + 1)
    )
    await session.commit()
    response.delete_cookie(COOKIE_NAME, path="/")


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordIn,
    ctx: ContextDep,
    request: Request,
    response: Response,
    session: SessionDep,
) -> None:
    user = await session.get(User, ctx.user_id)
    if not await verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前密码不正确")
    user.password_hash = await hash_password(body.new_password)
    # 改密后其他设备全部下线，当前设备换发新令牌
    user.session_version += 1
    version = user.session_version
    await session.commit()
    _set_cookie(
        response, SessionClaims(ctx.user_id, ctx.tenant_id, version), request.app.state.settings
    )
