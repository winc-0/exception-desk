import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.language_models import BaseChatModel
from sqlalchemy import func, select, update

from app.ai.facts import Facts, build_facts, risks, template_reply
from app.ai.graph import PROMPT_VERSION, build_graph, initial_state
from app.deps import Context, SessionDep, require, set_tenant
from app.models import TicketAiDraft, TicketEvent
from app.permissions import Permission
from app.routers.tickets import ticket_detail
from app.schemas import AiFeedbackIn
from app.ticket_flow import TERMINAL_STATUSES

router = APIRouter(prefix="/api/tickets", tags=["ai"])

Drafter = Annotated[Context, Depends(require(Permission.AI_DRAFT))]

_TOTAL_TIMEOUT_SECONDS = 60


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/{ticket_id}/ai/reply-draft")
async def create_reply_draft(
    ticket_id: uuid.UUID, request: Request, ctx: Drafter, session: SessionDep
) -> StreamingResponse:
    """流式起草给客户的回复。系统不会自动发送，由客服确认后自己发出。"""
    settings = request.app.state.settings
    detail = await ticket_detail(session, ctx, ticket_id)
    if detail.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "工单已结束，不需要再起草回复")

    since = datetime.now(UTC) - timedelta(hours=1)
    used = await session.scalar(
        select(func.count())
        .select_from(TicketAiDraft)
        .where(TicketAiDraft.requested_by == ctx.user_id, TicketAiDraft.created_at >= since)
    )
    if used >= settings.ai_hourly_limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "起草次数已达上限，请稍后再试")

    model: BaseChatModel | None = request.app.state.chat_model
    record = TicketAiDraft(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        requested_by=ctx.user_id,
        model=settings.llm_model if model is not None else "",
        prompt_version=PROMPT_VERSION,
    )
    session.add(record)
    draft_id = record.id
    # 先落库再调模型：断开连接的请求也计入额度。模型可能要跑几十秒，这段时间不占数据库连接
    await session.commit()

    facts = build_facts(detail)
    return StreamingResponse(
        _stream(request, ctx, ticket_id, draft_id, facts, model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream(
    request: Request,
    ctx: Context,
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    facts: Facts,
    model: BaseChatModel | None,
) -> AsyncIterator[str]:
    started = time.perf_counter()
    yield _sse("facts", {"missing_required": facts.missing_required, "risks": risks(facts)})

    draft, source, reason, attempts = "", "template", "未配置模型", 0
    input_tokens = output_tokens = 0
    if model is not None:
        state: dict = {}
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                graph = build_graph(model, facts)
                async for mode, chunk in graph.astream(
                    initial_state(), stream_mode=["messages", "updates"]
                ):
                    if mode == "messages":
                        message, metadata = chunk
                        if metadata.get("langgraph_node") != "agent":
                            continue
                        if isinstance(message.content, str) and message.content:
                            yield _sse("delta", {"text": message.content})
                        usage = getattr(message, "usage_metadata", None)
                        if usage:
                            input_tokens += usage.get("input_tokens", 0)
                            output_tokens += usage.get("output_tokens", 0)
                        continue
                    for node, update_values in chunk.items():
                        state.update(update_values or {})
                        if node == "tools":
                            names = [m.name for m in update_values["messages"]]
                            yield _sse("tool", {"names": names})
                        elif node == "check" and update_values["problems"]:
                            # 不合格的内容已经显示出来了，通知前端清掉
                            yield _sse("reset", {})
            attempts = state.get("attempts", 0)
            if not state.get("used_fallback") and state.get("draft"):
                draft, source, reason = state["draft"], "model", ""
            else:
                reason = "；".join(state.get("problems") or []) or "模型多次未给出结果"
        except TimeoutError:
            reason = "模型响应超时"
        except Exception as error:  # 模型服务的异常种类很多，都不能影响客服继续工作
            reason = f"模型调用失败：{type(error).__name__}"

    if source == "template":
        draft = template_reply(facts)

    latency_ms = int((time.perf_counter() - started) * 1000)
    async with request.app.state.sessionmaker() as session:
        await set_tenant(session, ctx.tenant_id)
        await session.execute(
            update(TicketAiDraft)
            .where(TicketAiDraft.id == draft_id)
            .values(
                status="done",
                source=source,
                draft=draft,
                fallback_reason=reason[:500],
                attempts=attempts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                finished_at=func.now(),
            )
        )
        session.add(
            TicketEvent(
                tenant_id=ctx.tenant_id,
                ticket_id=ticket_id,
                actor_id=ctx.user_id,
                kind="ai_drafted",
                data={"draft_id": str(draft_id), "source": source},
            )
        )
        await session.commit()

    yield _sse(
        "done",
        {"draft_id": str(draft_id), "draft": draft, "source": source, "reason": reason},
    )


@router.post("/{ticket_id}/ai/reply-draft/{draft_id}/feedback", status_code=204)
async def give_feedback(
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    body: AiFeedbackIn,
    ctx: Drafter,
    session: SessionDep,
) -> None:
    """记录客服最后怎么用这份草稿，用来判断 AI 建议到底有没有帮上忙。"""
    result = await session.execute(
        update(TicketAiDraft)
        .where(TicketAiDraft.id == draft_id, TicketAiDraft.ticket_id == ticket_id)
        .values(feedback=body.verdict)
    )
    if result.rowcount != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "草稿不存在")
    await session.commit()
