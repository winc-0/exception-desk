import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints

Role = Literal["admin", "supervisor", "agent", "warehouse"]
TicketType = Literal["damaged", "lost", "delayed", "address_change", "other"]
TicketStatus = Literal["open", "in_progress", "waiting", "resolved", "closed", "cancelled"]
ClaimStatus = Literal["preparing", "submitted", "approved", "rejected", "paid", "withdrawn"]
EvidenceCategory = Literal[
    "label_photo",
    "package_photo",
    "damage_photo",
    "packing_video",
    "weight_record",
    "tracking_screenshot",
    "value_proof",
    "customer_proof",
    "other",
]

Password = Annotated[str, StringConstraints(min_length=10, max_length=128)]
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
# 上限是一百亿元和一千吨，主要拦截手误
Cents = Annotated[int, Field(gt=0, le=1_000_000_000_000)]
Grams = Annotated[int, Field(gt=0, le=1_000_000_000)]
Quantity = Annotated[int, Field(gt=0, le=1_000_000)]
RejectionCode = Literal[
    "insufficient_materials", "damage_not_evident", "packaging_intact", "uninsured", "other"
]


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


class AttachmentOut(BaseModel):
    id: uuid.UUID
    category: EvidenceCategory
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploader_name: str
    created_at: datetime
    voided_at: datetime | None
    void_reason: str
    can_void: bool


class ChecklistItem(BaseModel):
    category: EvidenceCategory
    label: str
    level: Literal["required", "recommended", "optional"]
    count: int


class VoidIn(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


Carrier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class ClaimIn(BaseModel):
    carrier: Carrier
    goods_quantity: Quantity | None = None
    claim_amount_cents: Cents | None = None
    insured: bool = False
    pickup_weight_g: Grams | None = None
    billed_weight_g: Grams | None = None


class ClaimPatch(BaseModel):
    version: int
    carrier: Carrier | None = None
    goods_quantity: Quantity | None = None
    claim_amount_cents: Cents | None = None
    insured: bool | None = None
    pickup_weight_g: Grams | None = None
    billed_weight_g: Grams | None = None


class ClaimTransitionIn(BaseModel):
    to: ClaimStatus
    version: int
    note: str = Field(default="", max_length=2000)
    external_ref: str | None = Field(default=None, max_length=100)
    approved_cents: Cents | None = None
    paid_cents: Cents | None = None
    rejection_code: RejectionCode | None = None


class ClaimOut(BaseModel):
    version: int
    status: ClaimStatus
    carrier: str
    claim_amount_cents: int | None
    approved_cents: int | None
    paid_cents: int | None
    insured: bool
    pickup_weight_g: int | None
    billed_weight_g: int | None
    external_ref: str
    rejection_reason: str
    rejection_code: RejectionCode | None
    goods_quantity: int | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    paid_at: datetime | None


class ClaimListItem(ClaimOut):
    ticket_id: uuid.UUID
    ticket_number: int
    ticket_title: str
    ticket_type: TicketType
    tracking_number: str | None


class ClaimPage(BaseModel):
    items: list[ClaimListItem]
    next_before: int | None


class CarrierSummary(BaseModel):
    carrier: str
    total: int
    open: int
    rejected: int
    paid: int
    claimed_cents: int
    # 已有结果（到账或被拒）的索赔金额，到账率的分母
    settled_claimed_cents: int
    paid_cents: int


class RejectionCount(BaseModel):
    code: RejectionCode
    count: int


class ClaimSummary(BaseModel):
    total: CarrierSummary
    carriers: list[CarrierSummary]
    rejections: list[RejectionCount]


class AiFeedbackIn(BaseModel):
    verdict: Literal["used", "edited", "rejected"]


class TicketDetail(TicketOut):
    events: list[TicketEventOut]
    attachments: list[AttachmentOut]
    evidence_checklist: list[ChecklistItem]
    can_upload: bool
    can_draft_reply: bool
    # 没有索赔权限时 claim 是 None，claim_enabled 是 false
    claim: ClaimOut | None
    claim_enabled: bool
    claim_transitions: list[ClaimStatus]
    claim_blocks_closing: bool
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
