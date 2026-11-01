from enum import StrEnum


class Permission(StrEnum):
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    TICKETS_READ = "tickets:read"
    TICKETS_CREATE = "tickets:create"


_EVERYONE = {Permission.TICKETS_READ, Permission.TICKETS_CREATE}

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "admin": frozenset(_EVERYONE | {Permission.MEMBERS_READ, Permission.MEMBERS_MANAGE}),
    "supervisor": frozenset(_EVERYONE | {Permission.MEMBERS_READ}),
    "agent": frozenset(_EVERYONE),
    "warehouse": frozenset(_EVERYONE),
}
