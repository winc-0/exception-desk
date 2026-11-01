import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.models import Base
from tests.conftest import PASSWORD, Member


async def _add(client, email, role="agent", password=PASSWORD, name="成员"):
    return await client.post(
        "/api/members",
        json={"email": email, "display_name": name, "role": role, "password": password},
    )


def _member(admin: Member, response) -> Member:
    data = response.json()
    return Member(admin.tenant_id, data["user_id"], admin.tenant_slug, data["email"])


async def test_role_permissions(seed, login):
    admin = await seed("alpha")
    admin_client = await login(admin)
    clients = {}
    for role in ("supervisor", "agent", "warehouse"):
        response = await _add(admin_client, f"{role}@alpha.example.com", role)
        assert response.status_code == 201
        clients[role] = await login(_member(admin, response))

    assert (await clients["supervisor"].get("/api/members")).status_code == 200
    assert (await clients["agent"].get("/api/members")).status_code == 403
    assert (await clients["warehouse"].get("/api/members")).status_code == 403
    for client in clients.values():
        assert (await _add(client, "x@alpha.example.com")).status_code == 403
        created = await client.post("/api/tickets", json={"type": "lost", "title": "丢件"})
        assert created.status_code == 201


async def test_role_change_applies_to_existing_session(seed, login):
    admin = await seed("alpha")
    admin_client = await login(admin)
    member = _member(admin, await _add(admin_client, "s@alpha.example.com", "supervisor"))
    client = await login(member)
    assert (await client.get("/api/members")).status_code == 200

    await admin_client.patch(f"/api/members/{member.user_id}", json={"role": "agent"})
    assert (await client.get("/api/members")).status_code == 403


async def test_deactivation_applies_to_existing_session(seed, login, client_factory):
    admin = await seed("alpha")
    admin_client = await login(admin)
    member = _member(admin, await _add(admin_client, "a@alpha.example.com"))
    client = await login(member)
    assert (await client.get("/api/tickets")).status_code == 200

    await admin_client.patch(f"/api/members/{member.user_id}", json={"is_active": False})
    assert (await client.get("/api/tickets")).status_code == 401
    login_again = await client_factory().post(
        "/api/auth/login",
        json={"tenant_slug": "alpha", "email": member.email, "password": PASSWORD},
    )
    assert login_again.status_code == 401


async def test_last_admin_is_protected(seed, login):
    admin = await seed("alpha")
    client = await login(admin)
    url = f"/api/members/{admin.user_id}"

    assert (await client.patch(url, json={"role": "agent"})).status_code == 409
    assert (await client.patch(url, json={"is_active": False})).status_code == 409

    await _add(client, "second@alpha.example.com", "admin")
    assert (await client.patch(url, json={"role": "agent"})).status_code == 200


async def test_two_admins_cannot_demote_each_other_concurrently(seed, login):
    first = await seed("alpha")
    first_client = await login(first)
    second = _member(first, await _add(first_client, "second@alpha.example.com", "admin"))
    second_client = await login(second)

    for _ in range(10):
        responses = await asyncio.gather(
            first_client.patch(f"/api/members/{second.user_id}", json={"role": "agent"}),
            second_client.patch(f"/api/members/{first.user_id}", json={"role": "agent"}),
        )
        # 一个成功、一个因“保留最后一名管理员”被拒，不会死锁成 500
        assert sorted(r.status_code for r in responses) in ([200, 403], [200, 409])

        roles = [
            (await c.get("/api/auth/me")).json()["role"] for c in (first_client, second_client)
        ]
        assert roles.count("admin") == 1

        # 把被降级的人恢复成管理员，再来一轮
        admin_client, demoted = (
            (first_client, second) if roles[0] == "admin" else (second_client, first)
        )
        restored = await admin_client.patch(
            f"/api/members/{demoted.user_id}", json={"role": "admin"}
        )
        assert restored.status_code == 200


async def test_duplicate_member_conflicts(seed, login):
    client = await login(await seed("alpha"))
    assert (await _add(client, "a@alpha.example.com")).status_code == 201
    assert (await _add(client, "A@alpha.example.com")).status_code == 409


async def test_existing_account_joins_without_password_change(seed, login, client_factory):
    alpha, beta = await seed("alpha"), await seed("beta")
    client = await login(beta)

    response = await _add(client, alpha.email, password="attacker-chosen-password")
    assert response.status_code == 201
    assert response.json()["password_set"] is False

    # 其他公司的管理员不能通过“添加成员”改掉别人的密码
    fresh = client_factory()
    for slug, password, expected in (
        ("alpha", PASSWORD, 200),
        ("alpha", "attacker-chosen-password", 401),
    ):
        result = await fresh.post(
            "/api/auth/login",
            json={"tenant_slug": slug, "email": alpha.email, "password": password},
        )
        assert result.status_code == expected


async def test_models_match_migrations(owner_engine):
    def diff(connection):
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)

    async with owner_engine.connect() as connection:
        assert await connection.run_sync(diff) == []
