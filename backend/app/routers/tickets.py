import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, not_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app import claim_flow, evidence
from app.csv_import import MAX_BYTES, parse_csv
from app.deps import Context, SessionDep, require
from app.models import (
    Membership,
    Ticket,
    TicketAttachment,
    TicketClaim,
    TicketCounter,
    TicketEvent,
    User,
)
from app.permissions import Permission
from app.schemas import (
    AssignIn,
    AttachmentOut,
    ChecklistItem,
    ClaimOut,
    CommentIn,
    ImportResult,
    ImportSkipped,
    TicketDetail,
    TicketEventOut,
    TicketIn,
    TicketOut,
    TicketPage,
    TicketStatus,
    TicketType,
    TransitionIn,
)
from app.ticket_flow import (
    NEEDS_ASSIGNEE_STATUSES,
    TERMINAL_STATUSES,
    Actor,
    TransitionError,
    allowed_targets,
    check_transition,
)

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

Reader = Annotated[Context, Depends(require(Permission.TICKETS_READ))]
Creator = Annotated[Context, Depends(require(Permission.TICKETS_CREATE))]
Commenter = Annotated[Context, Depends(require(Permission.TICKETS_COMMENT))]
Importer = Annotated[Context, Depends(require(Permission.TICKETS_IMPORT))]

_Creator = aliased(User)
_Assignee = aliased(User)

_DUPLICATE = "该运单已有同类型的未关闭工单"
_STALE = "工单已被其他人更新，请确认最新内容后重试"


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


def _fields(ticket: Ticket, creator_name: str, assignee_name: str | None) -> dict:
    return {
        "id": ticket.id,
        "number": ticket.number,
        "version": ticket.version,
        "type": ticket.type,
        "title": ticket.title,
        "tracking_number": ticket.tracking_number,
        "description": ticket.description,
        "status": ticket.status,
        "resolution": ticket.resolution,
        "created_by": ticket.created_by,
        "creator_name": creator_name,
        "assignee_id": ticket.assignee_id,
        "assignee_name": assignee_name,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
    }


def _actor(ctx: Context, ticket: Ticket) -> Actor:
    return Actor(
        is_assignee=ticket.assignee_id == ctx.user_id,
        can_work=ctx.can(Permission.TICKETS_WORK),
        can_manage=ctx.can(Permission.TICKETS_MANAGE),
    )


def claim_out(claim: TicketClaim) -> dict:
    return {name: getattr(claim, name) for name in ClaimOut.model_fields}


async def evidence_counts(session: AsyncSession, ticket_id: uuid.UUID) -> dict[str, int]:
    rows = await session.execute(
        select(TicketAttachment.category, func.count())
        .where(TicketAttachment.ticket_id == ticket_id, TicketAttachment.voided_at.is_(None))
        .group_by(TicketAttachment.category)
    )
    return {category: count for category, count in rows}


async def lock_open_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    """锁住工单行。和关闭工单互斥，避免关闭的同时又开了索赔或传了证据。"""
    ticket = await session.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    if ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "工单已结束，不能再修改证据或索赔")
    return ticket


async def ticket_detail(session: AsyncSession, ctx: Context, ticket_id: uuid.UUID) -> TicketDetail:
    # 其他公司的工单被 RLS 过滤，返回 404，不暴露它是否存在
    row = (
        await session.execute(
            _select_tickets()
            .where(Ticket.id == ticket_id)
            # 刚用 UPDATE 语句改过，要刷新会话里缓存的旧对象
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    ticket = row[0]

    claims_enabled = ctx.can(Permission.CLAIMS_MANAGE)
    events_query = (
        select(TicketEvent, User.display_name)
        .join(User, User.id == TicketEvent.actor_id)
        .where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.id)
    )
    if not claims_enabled:
        # 索赔事件里有金额，没有索赔权限的人看不到
        events_query = events_query.where(not_(TicketEvent.kind.startswith("claim_")))
    events = await session.execute(events_query)

    attachments = await session.execute(
        select(TicketAttachment, User.display_name)
        .join(User, User.id == TicketAttachment.uploaded_by)
        .where(TicketAttachment.ticket_id == ticket_id)
        .order_by(TicketAttachment.created_at, TicketAttachment.id)
        .execution_options(populate_existing=True)
    )
    claim = await session.scalar(
        select(TicketClaim)
        .where(TicketClaim.ticket_id == ticket_id)
        .execution_options(populate_existing=True)
    )
    terminal = ticket.status in TERMINAL_STATUSES
    claim_locked = claim is not None and claim.status in claim_flow.LOCKS_EVIDENCE_STATUSES
    can_manage = ctx.can(Permission.TICKETS_MANAGE)
    return TicketDetail(
        **_fields(*row),
        events=[
            TicketEventOut(
                id=event.id,
                kind=event.kind,
                actor_name=name,
                from_status=event.from_status,
                to_status=event.to_status,
                body=event.body,
                data=event.data,
                created_at=event.created_at,
            )
            for event, name in events
        ],
        allowed_transitions=allowed_targets(
            ticket.status,
            actor=_actor(ctx, ticket),
            has_assignee=ticket.assignee_id is not None,
        ),
        can_claim=(
            ctx.can(Permission.TICKETS_CLAIM)
            and ticket.assignee_id is None
            and ticket.status not in TERMINAL_STATUSES
        ),
        can_assign=(ctx.can(Permission.TICKETS_MANAGE) and ticket.status not in TERMINAL_STATUSES),
        can_comment=ctx.can(Permission.TICKETS_COMMENT),
        attachments=[
            AttachmentOut(
                id=attachment.id,
                category=attachment.category,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size_bytes,
                sha256=attachment.sha256,
                uploader_name=name,
                created_at=attachment.created_at,
                voided_at=attachment.voided_at,
                void_reason=attachment.void_reason,
                can_void=(
                    attachment.voided_at is None
                    and not terminal
                    and not claim_locked
                    and (can_manage or attachment.uploaded_by == ctx.user_id)
                ),
            )
            for attachment, name in attachments
        ],
        evidence_checklist=[
            ChecklistItem(**item)
            for item in evidence.checklist(ticket.type, await evidence_counts(session, ticket_id))
        ],
        can_upload=ctx.can(Permission.TICKETS_COMMENT) and not terminal,
        claim=ClaimOut(**claim_out(claim)) if claim is not None and claims_enabled else None,
        claim_enabled=(
            claims_enabled
            and ticket.type in evidence.CLAIMABLE_TYPES
            and (claim is not None or not terminal)
        ),
        claim_transitions=(
            claim_flow.allowed_targets(claim.status, can_manage=can_manage)
            if claim is not None and claims_enabled and not terminal
            else []
        ),
        claim_blocks_closing=claim is not None and claim.status in claim_flow.OPEN_STATUSES,
    )


async def _allocate_numbers(session: AsyncSession, tenant_id: uuid.UUID, count: int = 1) -> int:
    """原子地分配工单号，返回本次分配的最大号码。

    这一行的锁会持有到事务结束，所以同一家公司的创建是串行的，号码不会重复；
    回滚时计数器一起回滚。不同公司互不影响。
    """
    statement = insert(TicketCounter).values(tenant_id=tenant_id, last_number=count)
    return await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[TicketCounter.tenant_id],
            set_={"last_number": TicketCounter.last_number + count},
        ).returning(TicketCounter.last_number)
    )


async def _active_member_name(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    name = await session.scalar(
        select(User.display_name)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == user_id,
            Membership.is_active,
            User.is_active,
        )
    )
    if name is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "处理人不是本公司的启用成员")
    return name


async def _get_for_update(session: AsyncSession, ticket_id: uuid.UUID, version: int) -> Ticket:
    ticket = await session.scalar(select(Ticket).where(Ticket.id == ticket_id))
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    if ticket.version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
    return ticket


async def _apply_update(session: AsyncSession, ticket: Ticket, version: int, **values) -> None:
    # 版本号写进 WHERE，读取之后被别人改过时影响 0 行，不会覆盖别人的修改
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.version == version)
        .values(version=version + 1, updated_at=func.now(), **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _STALE)


@router.get("")
async def list_tickets(
    ctx: Reader,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[int | None, Query(ge=1)] = None,
    ticket_status: Annotated[TicketStatus | None, Query(alias="status")] = None,
    type: TicketType | None = None,
    assignee: Literal["me", "unassigned"] | uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> TicketPage:
    query = _select_tickets()
    # 按工单号做游标分页：翻页时不会因为有新工单插入而重复或漏掉数据，深翻页也不变慢
    if before is not None:
        query = query.where(Ticket.number < before)
    if ticket_status is not None:
        query = query.where(Ticket.status == ticket_status)
    if type is not None:
        query = query.where(Ticket.type == type)
    if assignee == "me":
        query = query.where(Ticket.assignee_id == ctx.user_id)
    elif assignee == "unassigned":
        query = query.where(Ticket.assignee_id.is_(None))
    elif assignee is not None:
        query = query.where(Ticket.assignee_id == assignee)
    if q and q.strip():
        keyword = q.strip()
        query = query.where(
            or_(
                Ticket.title.icontains(keyword, autoescape=True),
                Ticket.tracking_number.icontains(keyword, autoescape=True),
            )
        )

    rows = (await session.execute(query.order_by(Ticket.number.desc()).limit(limit + 1))).all()
    items = [TicketOut(**_fields(*row)) for row in rows[:limit]]
    return TicketPage(items=items, next_before=items[-1].number if len(rows) > limit else None)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(body: TicketIn, ctx: Creator, session: SessionDep) -> TicketDetail:
    if body.assignee_id is not None:
        claiming_self = body.assignee_id == ctx.user_id and ctx.can(Permission.TICKETS_CLAIM)
        if not (ctx.can(Permission.TICKETS_MANAGE) or claiming_self):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "没有指派处理人的权限")
        await _active_member_name(session, ctx.tenant_id, body.assignee_id)

    number = await _allocate_numbers(session, ctx.tenant_id)
    ticket = Ticket(
        tenant_id=ctx.tenant_id,
        number=number,
        type=body.type,
        title=body.title,
        tracking_number=body.tracking_number,
        description=body.description,
        created_by=ctx.user_id,
        assignee_id=body.assignee_id,
    )
    session.add(ticket)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE) from None
    session.add(
        TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket.id,
            actor_id=ctx.user_id,
            kind="created",
            to_status=ticket.status,
        )
    )
    await session.flush()
    out = await ticket_detail(session, ctx, ticket.id)
    await session.commit()
    return out


@router.post("/import")
async def import_tickets(request: Request, ctx: Importer, session: SessionDep) -> ImportResult:
    """导入 CSV，请求体是文件内容本身。

    有任何一行不合法就整批不导入，让用户改好后重新上传，避免一半成功一半失败。
    已有同类未关闭工单的行会跳过并返回明细，所以同一个文件重复上传不会产生重复工单。
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "文件不能超过 1 MB")
        chunks.append(chunk)

    try:
        rows, errors = parse_csv(b"".join(chunks))
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "message": f"有 {len(errors)} 行不符合要求，没有导入任何数据",
                "errors": [{"line": e.line, "message": e.message} for e in errors],
            },
        )

    # 被跳过的行也占用了号码，所以导入有重复时工单号会跳号。号码只要求唯一递增，不要求连续。
    last = await _allocate_numbers(session, ctx.tenant_id, len(rows))
    numbered = {last - len(rows) + 1 + index: row for index, row in enumerate(rows)}
    inserted = await session.execute(
        insert(Ticket)
        .values(
            [
                {
                    "tenant_id": ctx.tenant_id,
                    "number": number,
                    "type": row.type,
                    "title": row.title,
                    "tracking_number": row.tracking_number,
                    "description": row.description,
                    "created_by": ctx.user_id,
                }
                for number, row in numbered.items()
            ]
        )
        .on_conflict_do_nothing()
        .returning(Ticket.id, Ticket.number)
    )
    created = inserted.all()
    if created:
        await session.execute(
            insert(TicketEvent).values(
                [
                    {
                        "tenant_id": ctx.tenant_id,
                        "ticket_id": ticket_id,
                        "actor_id": ctx.user_id,
                        "kind": "created",
                        "to_status": "open",
                        "data": {"source": "csv"},
                    }
                    for ticket_id, _ in created
                ]
            )
        )
    await session.commit()

    created_numbers = {number for _, number in created}
    return ImportResult(
        created=len(created),
        skipped=[
            ImportSkipped(line=row.line, tracking_number=row.tracking_number, message=_DUPLICATE)
            for number, row in numbered.items()
            if number not in created_numbers
        ],
    )


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: uuid.UUID, ctx: Reader, session: SessionDep) -> TicketDetail:
    return await ticket_detail(session, ctx, ticket_id)


@router.post("/{ticket_id}/transition")
async def transition_ticket(
    ticket_id: uuid.UUID, body: TransitionIn, ctx: Reader, session: SessionDep
) -> TicketDetail:
    ticket = await _get_for_update(session, ticket_id, body.version)
    source = ticket.status
    try:
        check_transition(
            source,
            body.to,
            actor=_actor(ctx, ticket),
            has_assignee=ticket.assignee_id is not None,
            note=body.note,
            resolution=body.resolution,
        )
    except TransitionError as error:
        raise HTTPException(error.status_code, error.message) from None

    values = {"status": body.to}
    if body.to == "resolved":
        values["resolution"] = body.resolution.strip()
    await _apply_update(session, ticket, body.version, **values)
    if body.to in TERMINAL_STATUSES:
        # 上面的 UPDATE 已经锁住工单行，和开启索赔互斥，这里读到的是最新状态
        claim_status = await session.scalar(
            select(TicketClaim.status).where(TicketClaim.ticket_id == ticket_id)
        )
        if claim_status in claim_flow.OPEN_STATUSES:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "索赔还没有结束，不能关闭或取消工单")
    session.add(
        TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket.id,
            actor_id=ctx.user_id,
            kind="status_changed",
            from_status=source,
            to_status=body.to,
            body=(body.resolution if body.to == "resolved" else body.note).strip(),
        )
    )
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out


@router.post("/{ticket_id}/assign")
async def assign_ticket(
    ticket_id: uuid.UUID, body: AssignIn, ctx: Reader, session: SessionDep
) -> TicketDetail:
    ticket = await _get_for_update(session, ticket_id, body.version)
    if ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "工单已结束，不能再指派")

    claiming = (
        body.assignee_id == ctx.user_id
        and ticket.assignee_id is None
        and ctx.can(Permission.TICKETS_CLAIM)
    )
    if not (ctx.can(Permission.TICKETS_MANAGE) or claiming):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有指派处理人的权限")
    if body.assignee_id is None and ticket.status in NEEDS_ASSIGNEE_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "处理中的工单不能没有处理人")
    if body.assignee_id == ticket.assignee_id:
        return await ticket_detail(session, ctx, ticket_id)

    assignee_name = None
    if body.assignee_id is not None:
        assignee_name = await _active_member_name(session, ctx.tenant_id, body.assignee_id)
    previous = ticket.assignee_id
    await _apply_update(session, ticket, body.version, assignee_id=body.assignee_id)
    session.add(
        TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket.id,
            actor_id=ctx.user_id,
            kind="assigned",
            # 记下当时的姓名：以后成员改名，历史记录也不会变
            data={
                "assignee_id": str(body.assignee_id) if body.assignee_id else None,
                "assignee_name": assignee_name,
                "previous_assignee_id": str(previous) if previous else None,
            },
        )
    )
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out


@router.post("/{ticket_id}/comments", status_code=status.HTTP_201_CREATED)
async def comment_ticket(
    ticket_id: uuid.UUID, body: CommentIn, ctx: Commenter, session: SessionDep
) -> TicketDetail:
    # 评论只是追加事件，不改工单本身，所以不需要版本号，也不会和别人的操作冲突
    exists = await session.scalar(select(Ticket.id).where(Ticket.id == ticket_id))
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    session.add(
        TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket_id,
            actor_id=ctx.user_id,
            kind="commented",
            body=body.body,
        )
    )
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out
