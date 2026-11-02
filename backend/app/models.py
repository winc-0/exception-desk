import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """全局表，无 RLS：登录时要先按公司代码找到租户。应用角色只读。"""

    __tablename__ = "tenants"
    __table_args__ = (CheckConstraint("slug ~ '^[a-z0-9-]{2,40}$'", name="tenants_slug_format"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """全局表，无 RLS：同一个人可以属于多个租户。接口只能通过 memberships 关联读取。"""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="users_email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # 令牌里带这个版本号，自增后该用户的所有旧会话立即失效
    session_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    failed_login_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    """租户表，受 RLS 保护。"""

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'supervisor', 'agent', 'warehouse')", name="memberships_role_valid"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ticket(Base):
    """租户表，受 RLS 保护。复合外键保证创建人和处理人一定属于同一租户。"""

    __tablename__ = "tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="tickets_creator_in_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assignee_id"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="tickets_assignee_in_tenant",
        ),
        CheckConstraint(
            "type IN ('damaged', 'lost', 'delayed', 'address_change', 'other')",
            name="tickets_type_valid",
        ),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting', 'resolved', 'closed', 'cancelled')",
            name="tickets_status_valid",
        ),
        UniqueConstraint("tenant_id", "number", name="tickets_number_key"),
        UniqueConstraint("tenant_id", "id", name="tickets_tenant_id_key"),
        # 同一运单的同类异常只能有一张未关闭的工单
        Index(
            "tickets_one_active_per_shipment",
            "tenant_id",
            "tracking_number",
            "type",
            unique=True,
            postgresql_where=text(
                "tracking_number IS NOT NULL AND status NOT IN ('closed', 'cancelled')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    number: Mapped[int] = mapped_column(Integer)
    # 乐观锁：每次修改都要带上读到的版本号，防止后提交的人覆盖别人刚做的操作
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    tracking_number: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolution: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TicketCounter(Base):
    """每家公司独立的工单号计数器，租户表，受 RLS 保护。"""

    __tablename__ = "ticket_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    last_number: Mapped[int] = mapped_column(Integer)


class TicketEvent(Base):
    """工单审计事件，只能追加。租户表，受 RLS 保护。"""

    __tablename__ = "ticket_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            name="ticket_events_ticket_in_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="ticket_events_actor_in_tenant",
        ),
        CheckConstraint(
            "kind IN ('created', 'status_changed', 'assigned', 'commented', "
            "'attachment_added', 'attachment_voided', "
            "'claim_opened', 'claim_updated', 'claim_status_changed', 'ai_drafted')",
            name="ticket_events_kind_valid",
        ),
        Index("ticket_events_ticket", "tenant_id", "ticket_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, server_default=text("''"))
    data: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TicketAttachment(Base):
    """证据附件，内容不可修改，只能作废。租户表，受 RLS 保护。"""

    __tablename__ = "ticket_attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            name="ticket_attachments_ticket_in_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "uploaded_by"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="ticket_attachments_uploader_in_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "voided_by"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="ticket_attachments_voider_in_tenant",
        ),
        Index("ticket_attachments_ticket", "tenant_id", "ticket_id", "created_at"),
        Index(
            "ticket_attachments_one_per_content",
            "tenant_id",
            "ticket_id",
            "sha256",
            unique=True,
            postgresql_where=text("voided_at IS NULL"),
        ),
    )

    # 由应用生成：写文件和写数据库都要用到
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    category: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    void_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))


class TicketClaim(Base):
    """向承运商的索赔，每张工单最多一笔。租户表，受 RLS 保护。金额单位是分，重量单位是克。"""

    __tablename__ = "ticket_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            name="ticket_claims_ticket_in_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="ticket_claims_creator_in_tenant",
        ),
        Index("ticket_claims_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'preparing'"))
    carrier: Mapped[str] = mapped_column(Text)
    claim_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    approved_cents: Mapped[int | None] = mapped_column(BigInteger)
    paid_cents: Mapped[int | None] = mapped_column(BigInteger)
    insured: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pickup_weight_g: Mapped[int | None] = mapped_column(Integer)
    billed_weight_g: Mapped[int | None] = mapped_column(Integer)
    external_ref: Mapped[str] = mapped_column(Text, server_default=text("''"))
    rejection_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    rejection_code: Mapped[str | None] = mapped_column(Text)
    goods_quantity: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketAiDraft(Base):
    """AI 回复草稿的记录，用于额度控制、成本统计和效果分析。租户表，受 RLS 保护。"""

    __tablename__ = "ticket_ai_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            name="ticket_ai_drafts_ticket_in_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "requested_by"],
            ["memberships.tenant_id", "memberships.user_id"],
            name="ticket_ai_drafts_requester_in_tenant",
        ),
        Index("ticket_ai_drafts_quota", "tenant_id", "requested_by", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, server_default=text("'generating'"))
    source: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, server_default=text("''"))
    prompt_version: Mapped[str] = mapped_column(Text)
    draft: Mapped[str] = mapped_column(Text, server_default=text("''"))
    fallback_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    feedback: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
