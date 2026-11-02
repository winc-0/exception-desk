import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints

Role = Literal["admin", "supervisor", "agent", "warehouse"]
TicketType = Literal["damaged", "lost", "delayed", "address_change", "other"]
TicketStatus = Literal["open", "in_progress", "waiting", "resolved", "closed", "cancelled"]

Password = Annotated[str, StringConstraints(min_length=10, max_length=128)]
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class LoginIn(BaseModel):
    tenant_slug: str = Field(max_length=40)
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: Password


class TenantOut(BaseModel):
    slug: str
    name: str


class MeOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    permissions: list[str]
    tenant: TenantOut


class MemberIn(BaseModel):
    email: NormalizedEmail
    display_name: Name
    role: Role
    password: Password


class MemberPatch(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    is_active: bool


class MemberCreated(MemberOut):
    # false 表示此邮箱已有账号，只是加入了本公司，传入的初始密码没有生效
    password_set: bool


class TicketIn(BaseModel):
    type: TicketType
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    tracking_number: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None
    ) = None
    description: str = Field(default="", max_length=5000)
    assignee_id: uuid.UUID | None = None


class TransitionIn(BaseModel):
    to: TicketStatus
    version: int
    note: str = Field(default="", max_length=2000)
    resolution: str = Field(default="", max_length=5000)


class AssignIn(BaseModel):
    assignee_id: uuid.UUID | None
    version: int


class CommentIn(BaseModel):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class TicketOut(BaseModel):
    id: uuid.UUID
    number: int
    version: int
    type: TicketType
    title: str
    tracking_number: str | None
    description: str
    status: TicketStatus
    resolution: str
    created_by: uuid.UUID
    creator_name: str
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    created_at: datetime
    updated_at: datetime


class TicketPage(BaseModel):
    items: list[TicketOut]
    # 传给下一次请求的 before 参数，None 表示没有更多了
    next_before: int | None


class TicketEventOut(BaseModel):
    id: int
    kind: str
    actor_name: str
    from_status: TicketStatus | None
    to_status: TicketStatus | None
    body: str
    data: dict
    created_at: datetime


class TicketDetail(TicketOut):
    events: list[TicketEventOut]
    allowed_transitions: list[TicketStatus]
    can_claim: bool
    can_assign: bool
    can_comment: bool


class ImportSkipped(BaseModel):
    line: int
    tracking_number: str | None
    message: str


class ImportResult(BaseModel):
    created: int
    skipped: list[ImportSkipped]
