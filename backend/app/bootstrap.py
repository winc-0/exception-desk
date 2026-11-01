import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models import Membership, Tenant, User
from app.security import hash_password


async def create_tenant_with_admin(
    connection: AsyncConnection,
    *,
    slug: str,
    name: str,
    admin_email: str,
    admin_name: str,
    admin_password: str,
    role: str = "admin",
) -> tuple[uuid.UUID, uuid.UUID]:
    """创建租户和第一位成员。应用角色没有 tenants 的写权限，所以只能用迁移账号调用。"""
    tenant_id = await connection.scalar(
        Tenant.__table__.insert().values(slug=slug, name=name).returning(Tenant.id)
    )
    email = admin_email.strip().lower()
    user_id = await connection.scalar(select(User.id).where(User.email == email))
    if user_id is None:
        user_id = await connection.scalar(
            User.__table__.insert()
            .values(
                email=email,
                display_name=admin_name,
                password_hash=await hash_password(admin_password),
            )
            .returning(User.id)
        )
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
    await connection.execute(
        Membership.__table__.insert().values(tenant_id=tenant_id, user_id=user_id, role=role)
    )
    return tenant_id, user_id
