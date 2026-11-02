"""索赔状态机。和工单状态机一样是纯函数，规则集中在这张表里。"""

from dataclasses import dataclass
from typing import Literal

from app.ticket_flow import TransitionError

# 索赔还在进行中，这时不能关闭或取消工单
OPEN_STATUSES = frozenset({"preparing", "submitted", "approved"})
EDITABLE_STATUSES = frozenset({"preparing", "rejected"})
# 材料已经交给承运商，这时作废证据会和提交的内容对不上
LOCKS_EVIDENCE_STATUSES = frozenset({"submitted", "approved", "paid"})


@dataclass(frozen=True)
class Rule:
    managers_only: bool = False
    requires_note: bool = False
    requires: Literal["submission", "approved", "paid"] | None = None


TRANSITIONS: dict[tuple[str, str], Rule] = {
    ("preparing", "submitted"): Rule(requires="submission"),
    ("submitted", "approved"): Rule(requires="approved"),
    ("submitted", "rejected"): Rule(requires_note=True),
    # 被拒后补充材料再次提交
    ("rejected", "submitted"): Rule(requires_note=True, requires="submission"),
    # 到账涉及钱，由主管确认
    ("approved", "paid"): Rule(managers_only=True, requires="paid"),
    ("preparing", "withdrawn"): Rule(requires_note=True),
    ("submitted", "withdrawn"): Rule(requires_note=True),
    ("rejected", "withdrawn"): Rule(requires_note=True),
    ("withdrawn", "preparing"): Rule(),
}


def allowed_targets(status: str, *, can_manage: bool) -> list[str]:
    return [
        target
        for (source, target), rule in TRANSITIONS.items()
        if source == status and (can_manage or not rule.managers_only)
    ]


def check_transition(
    status: str,
    target: str,
    *,
    can_manage: bool,
    note: str = "",
    claim_amount_cents: int | None = None,
    approved_cents: int | None = None,
    paid_cents: int | None = None,
    missing_evidence: list[str] | None = None,
    current_approved_cents: int | None = None,
) -> None:
    rule = TRANSITIONS.get((status, target))
    if rule is None:
        raise TransitionError(409, "索赔当前状态不能执行这个操作")
    if rule.managers_only and not can_manage:
        raise TransitionError(403, "没有执行此操作的权限")
    if rule.requires_note and not note.strip():
        raise TransitionError(422, "请填写原因")

    if rule.requires == "submission":
        if claim_amount_cents is None:
            raise TransitionError(422, "请先填写索赔金额")
        if missing_evidence:
            raise TransitionError(422, f"还缺必备证据：{'、'.join(missing_evidence)}")
    elif rule.requires == "approved":
        if approved_cents is None:
            raise TransitionError(422, "请填写核定金额")
        if claim_amount_cents is not None and approved_cents > claim_amount_cents:
            raise TransitionError(422, "核定金额不能超过索赔金额")
    elif rule.requires == "paid":
        if paid_cents is None:
            raise TransitionError(422, "请填写到账金额")
        if current_approved_cents is not None and paid_cents > current_approved_cents:
            raise TransitionError(422, "到账金额不能超过核定金额")
