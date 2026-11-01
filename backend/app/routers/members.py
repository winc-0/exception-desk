import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import Context, SessionDep, require
from app.models import Membership, User
from app.permissions import Permission
from app.schemas import MemberCreated, MemberIn, MemberOut, MemberPatch
from app.security import hash_password

router = APIRouter(prefix="/api/members", tags=["members"])

Reader = Annotated[Context, Depends(require(Permission.MEMBERS_READ))]
Manager = Annotated[Context, Depends(require(Permission.MEMBERS_MANAGE))]


def _out(membership: Membership, user: User) -> MemberOut:
    return MemberOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        is_active=membership.is_active,
    )


@router.get("")
async def list_members(ctx: Reader, session: SessionDep) -> list[MemberOut]:
    rows = await session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == ctx.tenant_id)
        .order_by(User.display_name)
    )
    return [_out(m, u) for m, u in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_member(body: MemberIn, ctx: Manager, session: SessionDep) -> MemberCreated:
    user = await session.scalar(select(User).where(User.email == body.email))
    password_set = user is None
    if user is None:
        user = User(
            email=body.email,
            display_name=body.display_name,
            password_hash=await hash_password(body.password),
        )
        session.add(user)

    try:
        await session.flush()
        membership = Membership(tenant_id=ctx.tenant_id, user_id=user.id, role=body.role)
        session.add(membership)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已是本公司成员") from None

    out = MemberCreated(**_out(membership, user).model_dump(), password_set=password_set)
    await session.commit()
    return out


@router.patch("/{user_id}")
async def update_member(
    user_id: uuid.UUID, body: MemberPatch, ctx: Manager, session: SessionDep
) -> MemberOut:
    # 先按固定顺序锁住所有管理员，再读目标成员。
    # 这样两个管理员同时互相降级时只会排队，不会死锁，也不会让公司失去最后一名管理员。
    admins = await session.scalars(
        select(Membership.user_id)
        .where(
            Membership.tenant_id == ctx.tenant_id,
            Membership.role == "admin",
            Membership.is_active,
        )
        .order_by(Membership.user_id)
        .with_for_update()
    )
    active_admin_count = len(admins.all())

    row = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id)
            .with_for_update(of=Membership)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    membership, user = row

    loses_admin = (
        membership.role == "admin"
        and membership.is_active
        and ((body.role is not None and body.role != "admin") or body.is_active is False)
    )
    if loses_admin and active_admin_count <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "公司至少要保留一名启用中的管理员")

    if body.role is not None:
        membership.role = body.role
    if body.is_active is not None:
        membership.is_active = body.is_active
    await session.flush()
    out = _out(membership, user)
    await session.commit()
    return out
