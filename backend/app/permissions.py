from enum import StrEnum


class Permission(StrEnum):
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    TICKETS_READ = "tickets:read"
    TICKETS_CREATE = "tickets:create"
    TICKETS_COMMENT = "tickets:comment"
    # 只能处理指派给自己的工单
    TICKETS_WORK = "tickets:work"
    # 领取无人处理的工单
    TICKETS_CLAIM = "tickets:claim"
    TICKETS_IMPORT = "tickets:import"
    # 指派任何人，处理任何工单，以及关闭、取消、重新打开
    TICKETS_MANAGE = "tickets:manage"
    # 查看和跟进承运商索赔。仓管看不到金额
    CLAIMS_MANAGE = "claims:manage"


_FRONTLINE = {
    Permission.TICKETS_READ,
    Permission.TICKETS_CREATE,
    Permission.TICKETS_COMMENT,
    Permission.TICKETS_WORK,
}
_AGENT = _FRONTLINE | {
    Permission.TICKETS_CLAIM,
    Permission.TICKETS_IMPORT,
    Permission.CLAIMS_MANAGE,
}
_SUPERVISOR = _AGENT | {Permission.TICKETS_MANAGE, Permission.MEMBERS_READ}

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "admin": frozenset(_SUPERVISOR | {Permission.MEMBERS_MANAGE}),
    "supervisor": frozenset(_SUPERVISOR),
    "agent": frozenset(_AGENT),
    "warehouse": frozenset(_FRONTLINE),
}
