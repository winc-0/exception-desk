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
            "kind IN ('created', 'status_changed', 'assigned', 'commented')",
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
