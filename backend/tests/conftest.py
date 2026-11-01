"""测试跑在真实的 PostgreSQL 上：RLS 是数据库的行为，用 SQLite 或 mock 证明不了。

默认启动内置 PostgreSQL；设置 DESK_TEST_ADMIN_URL 时（CI）改用外部实例。
"""

import asyncio
import os
import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.bootstrap import create_tenant_with_admin
from app.config import Settings
from app.main import create_app

PASSWORD = "correct-horse-battery"
ORIGIN = "http://app.test"
DB_NAME = "desk_test"


async def _prepare_database(admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        for role in ("desk_owner", "desk_app"):
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            )
            if not exists:
                # 故意不给超级用户：验证 owner 也受 FORCE RLS 约束
                await connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'test'"))
        await connection.execute(text(f"DROP DATABASE IF EXISTS {DB_NAME}"))
        await connection.execute(text(f"CREATE DATABASE {DB_NAME} OWNER desk_owner"))
    await engine.dispose()


@pytest.fixture(scope="session")
def db_urls(tmp_path_factory):
    server = None
    admin_url = os.environ.get("DESK_TEST_ADMIN_URL")
    if admin_url is None:
        import pgserver

        server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
        admin_url = server.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    asyncio.run(_prepare_database(admin_url))

    host_port = admin_url.split("@", 1)[1].split("/", 1)[0]
    owner = f"postgresql+asyncpg://desk_owner:test@{host_port}/{DB_NAME}"
    app = f"postgresql+asyncpg://desk_app:test@{host_port}/{DB_NAME}"

    os.environ["DESK_MIGRATION_DATABASE_URL"] = owner
    command.upgrade(Config("alembic.ini"), "head")
    yield {"owner": owner, "app": app}
    if server is not None:
        server.cleanup()


@pytest.fixture
def settings(db_urls) -> Settings:
    return Settings(
        _env_file=None,
        database_url=db_urls["app"],
        jwt_secret="test-secret-" + "x" * 32,
        cookie_secure=False,
        allowed_origins=ORIGIN,
    )


@pytest_asyncio.fixture
async def owner_engine(db_urls):
    engine = create_async_engine(db_urls["owner"])
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE tickets, memberships, users, tenants CASCADE"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(db_urls):
    engine = create_async_engine(db_urls["app"])
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app(settings, owner_engine):
    application = create_app(settings)
    yield application
    await application.state.engine.dispose()


@dataclass
class Member:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    tenant_slug: str
    email: str


@pytest_asyncio.fixture
async def seed(owner_engine):
    async def _seed(slug: str, email: str | None = None, role: str = "admin") -> Member:
        email = email or f"admin@{slug}.example.com"
        async with owner_engine.begin() as connection:
            tenant_id, user_id = await create_tenant_with_admin(
                connection,
                slug=slug,
                name=f"{slug} 公司",
                admin_email=email,
                admin_name=f"{slug} 管理员",
                admin_password=PASSWORD,
                role=role,
            )
        return Member(tenant_id, user_id, slug, email)

    return _seed


@pytest_asyncio.fixture
async def client_factory(app):
    clients = []

    def _client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        )
        clients.append(client)
        return client

    yield _client
    for client in clients:
        await client.aclose()


@pytest_asyncio.fixture
async def login(client_factory):
    async def _login(member: Member, password: str = PASSWORD) -> httpx.AsyncClient:
        client = client_factory()
        response = await client.post(
            "/api/auth/login",
            json={"tenant_slug": member.tenant_slug, "email": member.email, "password": password},
        )
        assert response.status_code == 200, response.text
        return client

    return _login


async def set_tenant(connection: AsyncConnection, tenant_id: uuid.UUID) -> None:
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )
