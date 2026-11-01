import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.deps import Context, SessionDep, require
from app.models import Membership, Ticket, User
from app.permissions import Permission
from app.schemas import TicketIn, TicketOut

# 这里只有验证租户隔离所需的创建和查询。状态流转、指派、审计日志在下一阶段实现。
router = APIRouter(prefix="/api/tickets", tags=["tickets"])

Reader = Annotated[Context, Depends(require(Permission.TICKETS_READ))]
Creator = Annotated[Context, Depends(require(Permission.TICKETS_CREATE))]

_Creator = aliased(User)
_Assignee = aliased(User)


def _select_tickets():
    return (
        select(
            Ticket,
            _Creator.display_name.label("creator_name"),
            _Assignee.display_name.label("assignee_name"),
        )
        .join(_Creator, _Creator.id == Ticket.created_by)
        .outerjoin(_Assignee, _Assignee.id == Ticket.assignee_id)
    )


def _out(ticket: Ticket, creator_name: str, assignee_name: str | None) -> TicketOut:
    return TicketOut(
        id=ticket.id,
        type=ticket.type,
        title=ticket.title,
        tracking_number=ticket.tracking_number,
        description=ticket.description,
        status=ticket.status,
        created_by=ticket.created_by,
        creator_name=creator_name,
        assignee_id=ticket.assignee_id,
        assignee_name=assignee_name,
        created_at=ticket.created_at,
    )


async def _load(session: AsyncSession, ticket_id: uuid.UUID) -> TicketOut | None:
    row = (await session.execute(_select_tickets().where(Ticket.id == ticket_id))).first()
    return _out(*row) if row else None


@router.get("")
async def list_tickets(
    ctx: Reader,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[TicketOut]:
    rows = await session.execute(
        _select_tickets().order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit)
    )
    return [_out(*row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(body: TicketIn, ctx: Creator, session: SessionDep) -> TicketOut:
    if body.assignee_id is not None:
        assignee_ok = await session.scalar(
            select(Membership.user_id).where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == body.assignee_id,
                Membership.is_active,
            )
        )
        if assignee_ok is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "处理人不是本公司的启用成员")

    ticket = Ticket(
        tenant_id=ctx.tenant_id,
        type=body.type,
        title=body.title,
        tracking_number=body.tracking_number,
        description=body.description,
        created_by=ctx.user_id,
        assignee_id=body.assignee_id,
    )
    session.add(ticket)
    await session.flush()
    out = await _load(session, ticket.id)
    await session.commit()
    return out


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: uuid.UUID, ctx: Reader, session: SessionDep) -> TicketOut:
    # 其他公司的工单被 RLS 过滤，返回 404，不暴露它是否存在
    out = await _load(session, ticket_id)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    return out
