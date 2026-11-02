import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app import claim_flow, file_store
from app.deps import Context, SessionDep, require, set_tenant
from app.evidence import CATEGORY_LABELS
from app.models import Ticket, TicketAttachment, TicketClaim, TicketEvent
from app.permissions import Permission
from app.routers.tickets import lock_open_ticket, ticket_detail
from app.schemas import EvidenceCategory, TicketDetail, VoidIn
from app.ticket_flow import TERMINAL_STATUSES

router = APIRouter(prefix="/api/tickets", tags=["attachments"])

Reader = Annotated[Context, Depends(require(Permission.TICKETS_READ))]
Uploader = Annotated[Context, Depends(require(Permission.TICKETS_COMMENT))]

MAX_PER_TICKET = 50


@router.post("/{ticket_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    ticket_id: uuid.UUID,
    request: Request,
    ctx: Uploader,
    session: SessionDep,
    category: EvidenceCategory,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> TicketDetail:
    """上传一份证据，请求体是文件内容本身。"""
    ticket = await session.scalar(select(Ticket).where(Ticket.id == ticket_id))
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    if ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "工单已结束，不能再修改证据或索赔")

    # 手机网络上传一个视频可能要几分钟，不能一直占着数据库连接。
    # 结束事务后 RLS 设置失效，收完文件要重新设置。
    await session.rollback()
    try:
        received = await file_store.receive(
            request.stream(), request.app.state.settings.attachment_dir
        )
    except file_store.FileTooLarge:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "文件不能超过 50 MB") from None

    final_path = None
    try:
        if received.size == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "文件是空的")
        content_type = file_store.sniff_content_type(received.head)
        if content_type is None:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "只支持 JPG、PNG、WebP、HEIC、PDF、MP4 和 MOV",
            )

        await set_tenant(session, ctx.tenant_id)
        await lock_open_ticket(session, ticket_id)
        existing = await session.scalar(
            select(func.count())
            .select_from(TicketAttachment)
            .where(TicketAttachment.ticket_id == ticket_id, TicketAttachment.voided_at.is_(None))
        )
        if existing >= MAX_PER_TICKET:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"每张工单最多保留 {MAX_PER_TICKET} 份证据"
            )

        attachment_id = uuid.uuid4()
        name = file_store.clean_filename(filename)
        session.add(
            TicketAttachment(
                id=attachment_id,
                tenant_id=ctx.tenant_id,
                ticket_id=ticket_id,
                uploaded_by=ctx.user_id,
                category=category,
                filename=name,
                content_type=content_type,
                size_bytes=received.size,
                sha256=received.sha256,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "这份文件已经上传过了") from None
        session.add(
            TicketEvent(
                tenant_id=ctx.tenant_id,
                ticket_id=ticket_id,
                actor_id=ctx.user_id,
                kind="attachment_added",
                data={
                    "attachment_id": str(attachment_id),
                    "category": category,
                    "category_label": CATEGORY_LABELS[category],
                    "filename": name,
                    "sha256": received.sha256,
                },
            )
        )
        await session.flush()
        out = await ticket_detail(session, ctx, ticket_id)

        # 先放文件再提交：提交失败时删掉文件；进程中途崩溃最多留下无人引用的文件，
        # 不会出现数据库里有记录、文件却不存在的情况
        final_path = file_store.attachment_path(
            request.app.state.settings.attachment_dir, ctx.tenant_id, attachment_id
        )
        await file_store.publish(received.temp_path, final_path)
        await session.commit()
        return out
    except BaseException:
        if final_path is not None:
            await file_store.discard(final_path)
        raise
    finally:
        await file_store.discard(received.temp_path)


@router.get("/{ticket_id}/attachments/{attachment_id}/content")
async def download_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    ctx: Reader,
    session: SessionDep,
) -> FileResponse:
    # 其他公司的附件被 RLS 过滤，返回 404
    attachment = await session.scalar(
        select(TicketAttachment).where(
            TicketAttachment.id == attachment_id, TicketAttachment.ticket_id == ticket_id
        )
    )
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "附件不存在")
    # 传输大文件时不占数据库连接。用 commit 而不是 rollback：commit 不会让已读到的对象过期
    await session.commit()

    path = file_store.attachment_path(
        request.app.state.settings.attachment_dir, ctx.tenant_id, attachment_id
    )
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已丢失，请联系管理员")
    inline = attachment.content_type in file_store.INLINE_TYPES
    return FileResponse(
        path,
        media_type=attachment.content_type,
        filename=attachment.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            # 文件来自用户，即使被直接打开也不能在本站域名下运行脚本
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-cache",
        },
    )


@router.post("/{ticket_id}/attachments/{attachment_id}/void")
async def void_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    body: VoidIn,
    ctx: Uploader,
    session: SessionDep,
) -> TicketDetail:
    """传错的证据可以作废。原文件和记录都保留，只是不再计入证据清单。"""
    await lock_open_ticket(session, ticket_id)
    attachment = await session.scalar(
        select(TicketAttachment).where(
            TicketAttachment.id == attachment_id, TicketAttachment.ticket_id == ticket_id
        )
    )
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "附件不存在")
    if attachment.uploaded_by != ctx.user_id and not ctx.can(Permission.TICKETS_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有上传人和主管可以作废证据")
    if attachment.voided_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "这份证据已经作废")
    claim_status = await session.scalar(
        select(TicketClaim.status).where(TicketClaim.ticket_id == ticket_id)
    )
    if claim_status in claim_flow.LOCKS_EVIDENCE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "索赔材料已提交，证据不能再作废")

    await session.execute(
        update(TicketAttachment)
        .where(TicketAttachment.id == attachment_id)
        .values(voided_at=func.now(), voided_by=ctx.user_id, void_reason=body.reason)
        .execution_options(synchronize_session=False)
    )
    session.add(
        TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket_id,
            actor_id=ctx.user_id,
            kind="attachment_voided",
            body=body.reason,
            data={
                "attachment_id": str(attachment_id),
                "category_label": CATEGORY_LABELS[attachment.category],
                "filename": attachment.filename,
            },
        )
    )
    await session.flush()
    out = await ticket_detail(session, ctx, ticket_id)
    await session.commit()
    return out
