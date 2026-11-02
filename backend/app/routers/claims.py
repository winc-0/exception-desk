import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import claim_flow, evidence
from app.deps import Context, SessionDep, require
from app.models import Ticket, TicketClaim, TicketEvent
from app.permissions import Permission
from app.routers.tickets import claim_out, evidence_counts, lock_open_ticket, ticket_detail
from app.schemas import (
    CarrierSummary,
    ClaimIn,
    ClaimListItem,
    ClaimPage,
    ClaimPatch,
    ClaimStatus,
    ClaimSummary,
    ClaimTransitionIn,
    RejectionCount,
    TicketDetail,
)
from app.ticket_flow import TransitionError

router = APIRouter(tags=["claims"])

ClaimWorker = Annotated[Context, Depends(require(Permission.CLAIMS_MANAGE))]

_STALE = "索赔已被其他人更新，请确认最新内容后重试"
_EDITABLE_FIELDS = (
    "carrier",
    "goods_quantity",
    "claim_amount_cents",
    "insured",
    "pickup_weight_g",
    "billed_weight_g",
)


async def _load_claim(session: AsyncSession, ticket_id: uuid.UUID, version: int) -> TicketClaim:
    claim = await session.scalar(
        select(TicketClaim)
        .where(TicketClaim.ticket_id == ticket_id)
        .execution_options(populate_existing=True)
    )
    if claim is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这张工单还没有索赔")
    if claim.version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
    return claim


async def _apply(session: AsyncSession, claim: TicketClaim, **values) -> None:
    # 版本号写进 WHERE，读取之后被别人改过时影响 0 行，不会覆盖别人的修改
    result = await session.execute(
        update(TicketClaim)
        .where(TicketClaim.ticket_id == claim.ticket_id, TicketClaim.version == claim.version)
        .values(version=claim.version + 1, updated_at=func.now(), **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _STALE)


def _event(ctx: Context, ticket_id: uuid.UUID, kind: str, body: str = "", **data) -> TicketEvent:
    return TicketEvent(
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        actor_id=ctx.user_id,
        kind=kind,
        body=body,
        data=data,
    )


@router.post("/api/tickets/{ticket_id}/claim", status_code=status.HTTP_201_CREATED)
async def open_claim(
    ticket_id: uuid.UUID, body: ClaimIn, ctx: ClaimWorker, session: SessionDep
) -> TicketDetail:
    ticket = await lock_open_ticket(session, ticket_id)
    if ticket.type not in evidence.CLAIMABLE_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "这类工单不涉及索赔")
    # 工单行已锁，同一张工单不会同时开出两笔，不需要处理主键冲突
    exists = await session.scalar(
        select(TicketClaim.ticket_id).where(TicketClaim.ticket_id == ticket_id)
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "这张工单已经有索赔了")

    session.add(
        TicketClaim(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket_id,
            created_by=ctx.user_id,
            **body.model_dump(),
        )
    )
    await session.flush()
    session.add(_event(ctx, ticket_id, "claim_opened", carrier=body.carrier))
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out


@router.patch("/api/tickets/{ticket_id}/claim")
async def update_claim(
    ticket_id: uuid.UUID, body: ClaimPatch, ctx: ClaimWorker, session: SessionDep
) -> TicketDetail:
    await lock_open_ticket(session, ticket_id)
    claim = await _load_claim(session, ticket_id, body.version)
    if claim.status not in claim_flow.EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "索赔当前状态不能修改，被拒后才能调整")

    provided = body.model_dump(include=set(_EDITABLE_FIELDS), exclude_unset=True)
    for field in ("carrier", "insured"):
        if field in provided and provided[field] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "填写的内容不符合要求")
    changes = {name: value for name, value in provided.items() if getattr(claim, name) != value}
    if not changes:
        return await ticket_detail(session, ctx, ticket_id)

    await _apply(session, claim, **changes)
    session.add(_event(ctx, ticket_id, "claim_updated", changes=changes))
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out


@router.post("/api/tickets/{ticket_id}/claim/transition")
async def transition_claim(
    ticket_id: uuid.UUID, body: ClaimTransitionIn, ctx: ClaimWorker, session: SessionDep
) -> TicketDetail:
    ticket = await lock_open_ticket(session, ticket_id)
    claim = await _load_claim(session, ticket_id, body.version)
    source = claim.status
    missing = evidence.missing_required(ticket.type, await evidence_counts(session, ticket_id))
    try:
        claim_flow.check_transition(
            source,
            body.to,
            can_manage=ctx.can(Permission.TICKETS_MANAGE),
            note=body.note,
            claim_amount_cents=claim.claim_amount_cents,
            approved_cents=body.approved_cents,
            paid_cents=body.paid_cents,
            missing_evidence=missing,
            current_approved_cents=claim.approved_cents,
            rejection_code=body.rejection_code,
        )
    except TransitionError as error:
        raise HTTPException(error.status_code, error.message) from None

    values: dict = {"status": body.to}
    note = body.note.strip()
    if body.to == "submitted":
        values["submitted_at"] = func.now()
        values["rejection_reason"] = ""
        values["rejection_code"] = None
        if body.external_ref is not None:
            values["external_ref"] = body.external_ref.strip()
    elif body.to == "approved":
        values["approved_cents"] = body.approved_cents
    elif body.to == "rejected":
        values["rejection_reason"] = note
        values["rejection_code"] = body.rejection_code
    elif body.to == "paid":
        values["paid_cents"] = body.paid_cents
        values["paid_at"] = func.now()
    await _apply(session, claim, **values)

    data = {"from": source, "to": body.to}
    if body.to == "rejected":
        data["rejection_code"] = body.rejection_code
    if body.to == "approved":
        data["approved_cents"] = body.approved_cents
    elif body.to == "paid":
        data["paid_cents"] = body.paid_cents
    session.add(_event(ctx, ticket_id, "claim_status_changed", note, **data))
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out


@router.get("/api/claims")
async def list_claims(
    ctx: ClaimWorker,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[int | None, Query(ge=1)] = None,
    claim_status: Annotated[ClaimStatus | None, Query(alias="status")] = None,
) -> ClaimPage:
    query = select(TicketClaim, Ticket).join(Ticket, Ticket.id == TicketClaim.ticket_id)
    if before is not None:
        query = query.where(Ticket.number < before)
    if claim_status is not None:
        query = query.where(TicketClaim.status == claim_status)
    rows = (await session.execute(query.order_by(Ticket.number.desc()).limit(limit + 1))).all()
    items = [
        ClaimListItem(
            **claim_out(claim),
            ticket_id=ticket.id,
            ticket_number=ticket.number,
            ticket_title=ticket.title,
            ticket_type=ticket.type,
            tracking_number=ticket.tracking_number,
        )
        for claim, ticket in rows[:limit]
    ]
    return ClaimPage(
        items=items, next_before=items[-1].ticket_number if len(rows) > limit else None
    )


@router.get("/api/claims/summary")
async def claim_summary(ctx: ClaimWorker, session: SessionDep) -> ClaimSummary:
    """按承运商汇总。撤回的索赔不算在内。"""
    amount = func.coalesce(TicketClaim.claim_amount_cents, 0)
    rows = await session.execute(
        select(
            TicketClaim.carrier,
            func.count().label("total"),
            func.count().filter(TicketClaim.status.in_(claim_flow.OPEN_STATUSES)).label("open"),
            func.count().filter(TicketClaim.status == "rejected").label("rejected"),
            func.count().filter(TicketClaim.status == "paid").label("paid"),
            func.coalesce(func.sum(amount), 0).label("claimed_cents"),
            func.coalesce(
                func.sum(case((TicketClaim.status.in_(("paid", "rejected")), amount), else_=0)), 0
            ).label("settled_claimed_cents"),
            func.coalesce(func.sum(TicketClaim.paid_cents), 0).label("paid_cents"),
        )
        .where(TicketClaim.status != "withdrawn")
        .group_by(TicketClaim.carrier)
        .order_by(func.sum(amount).desc(), TicketClaim.carrier)
    )
    carriers = [CarrierSummary(**row._mapping) for row in rows]
    numeric = set(CarrierSummary.model_fields) - {"carrier"}
    total = CarrierSummary(
        carrier="全部",
        **{name: sum(getattr(carrier, name) for carrier in carriers) for name in numeric},
    )
    # 被拒后再次提交的不算，这里看的是当前卡在哪类拒赔理由上
    rejection_rows = await session.execute(
        select(TicketClaim.rejection_code, func.count())
        .where(TicketClaim.status == "rejected", TicketClaim.rejection_code.is_not(None))
        .group_by(TicketClaim.rejection_code)
        .order_by(func.count().desc(), TicketClaim.rejection_code)
    )
    return ClaimSummary(
        total=total,
        carriers=carriers,
        rejections=[RejectionCount(code=code, count=count) for code, count in rejection_rows],
    )
