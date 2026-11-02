"""工单状态机。纯函数，不依赖数据库，所有流转规则都集中在这张表里。"""

from dataclasses import dataclass
from typing import Literal

TERMINAL_STATUSES = frozenset({"closed", "cancelled"})
# 这些状态下工单有人在跟进，不能没有处理人
NEEDS_ASSIGNEE_STATUSES = frozenset({"in_progress", "waiting"})


@dataclass(frozen=True)
class Rule:
    managers_only: bool = False
    needs_assignee: bool = False
    requires: Literal["note", "resolution"] | None = None


TRANSITIONS: dict[tuple[str, str], Rule] = {
    ("open", "in_progress"): Rule(needs_assignee=True),
    ("in_progress", "waiting"): Rule(requires="note"),
    ("waiting", "in_progress"): Rule(),
    ("in_progress", "resolved"): Rule(requires="resolution"),
    ("resolved", "closed"): Rule(managers_only=True),
    ("resolved", "in_progress"): Rule(managers_only=True, requires="note"),
    ("open", "cancelled"): Rule(managers_only=True, requires="note"),
    ("in_progress", "cancelled"): Rule(managers_only=True, requires="note"),
    ("waiting", "cancelled"): Rule(managers_only=True, requires="note"),
}

_REQUIRED_LABELS = {"note": "请填写原因", "resolution": "请填写处理结果"}


class TransitionError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class Actor:
    is_assignee: bool
    can_work: bool
    can_manage: bool


def _permitted(rule: Rule, actor: Actor) -> bool:
    if actor.can_manage:
        return True
    return not rule.managers_only and actor.can_work and actor.is_assignee


def allowed_targets(status: str, *, actor: Actor, has_assignee: bool) -> list[str]:
    """当前用户能把工单改成哪些状态，前端据此显示按钮，规则不用在前端再写一遍。"""
    return [
        target
        for (source, target), rule in TRANSITIONS.items()
        if source == status
        and _permitted(rule, actor)
        and (has_assignee or not rule.needs_assignee)
    ]


def check_transition(
    status: str,
    target: str,
    *,
    actor: Actor,
    has_assignee: bool,
    note: str = "",
    resolution: str = "",
) -> None:
    rule = TRANSITIONS.get((status, target))
    if rule is None:
        raise TransitionError(409, f"工单当前状态不能改为 {target}")
    if not _permitted(rule, actor):
        raise TransitionError(403, "没有执行此操作的权限")
    if rule.needs_assignee and not has_assignee:
        raise TransitionError(422, "请先指派处理人")
    provided = {"note": note, "resolution": resolution}
    if rule.requires and not provided[rule.requires].strip():
        raise TransitionError(422, _REQUIRED_LABELS[rule.requires])
