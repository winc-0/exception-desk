"""租户隔离分两层验证：绕过应用直接查库（证明数据库兜底），再走 API。"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import set_tenant


async def _ticket(client, title="货物破损", **extra):
    response = await client.post("/api/tickets", json={"type": "damaged", "title": title, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def test_app_role_is_restricted(app_engine, owner_engine):
    async with app_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert row == (False, False)


async def test_no_tenant_set_returns_no_rows(seed, login, app_engine):
    member = await seed("alpha")
    await _ticket(await login(member))

    async with app_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM tickets")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM memberships")) == 0


async def test_db_only_returns_current_tenant_rows(seed, login, app_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    await _ticket(await login(alpha), "A 的工单")
    await _ticket(await login(beta), "B 的工单")

    async with app_engine.connect() as connection:
        await set_tenant(connection, alpha.tenant_id)
        titles = (await connection.scalars(text("SELECT title FROM tickets"))).all()
    assert titles == ["A 的工单"]


async def test_tenant_setting_does_not_leak_to_next_transaction(seed, login, app_engine):
    alpha = await seed("alpha")
    await _ticket(await login(alpha))

    async with app_engine.connect() as connection:
        async with connection.begin():
            await set_tenant(connection, alpha.tenant_id)
            assert await connection.scalar(text("SELECT count(*) FROM tickets")) == 1
        # 同一条连接开始新事务，相当于连接池把它分给了下一个请求
        async with connection.begin():
            assert await connection.scalar(text("SELECT count(*) FROM tickets")) == 0


async def test_db_rejects_insert_into_other_tenant(seed, app_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    async with app_engine.connect() as connection:
        await set_tenant(connection, alpha.tenant_id)
        with pytest.raises(DBAPIError, match="row-level security"):
            await connection.execute(
                text(
                    "INSERT INTO tickets (tenant_id, number, type, title, created_by) "
                    "VALUES (:t, 1, 'other', 'x', :u)"
                ),
                {"t": beta.tenant_id, "u": beta.user_id},
            )


async def test_db_rejects_moving_row_to_other_tenant(seed, login, app_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    await _ticket(await login(alpha))
    async with app_engine.connect() as connection:
        await set_tenant(connection, alpha.tenant_id)
        with pytest.raises(DBAPIError, match="row-level security"):
            await connection.execute(
                text("UPDATE tickets SET tenant_id = :t"), {"t": beta.tenant_id}
            )


async def test_owner_is_also_subject_to_rls(seed, login, owner_engine):
    alpha = await seed("alpha")
    await _ticket(await login(alpha))
    async with owner_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM tickets")) == 0


async def test_db_rejects_cross_tenant_assignee(seed, app_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    async with app_engine.connect() as connection:
        await set_tenant(connection, alpha.tenant_id)
        with pytest.raises(IntegrityError, match="tickets_assignee_in_tenant"):
            await connection.execute(
                text(
                    "INSERT INTO tickets (tenant_id, number, type, title, created_by, assignee_id) "
                    "VALUES (:t, 1, 'other', 'x', :u, :a)"
                ),
                {"t": alpha.tenant_id, "u": alpha.user_id, "a": beta.user_id},
            )


async def test_app_role_cannot_delete_or_change_tenants(seed, app_engine):
    alpha = await seed("alpha")
    for sql in ("DELETE FROM tickets", "DELETE FROM users", "UPDATE tenants SET name = 'x'"):
        async with app_engine.connect() as connection:
            await set_tenant(connection, alpha.tenant_id)
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(sql))


async def test_api_cannot_read_other_tenant_ticket(seed, login):
    alpha, beta = await seed("alpha"), await seed("beta")
    alpha_client, beta_client = await login(alpha), await login(beta)
    ticket = await _ticket(alpha_client, "A 的工单")

    assert (await alpha_client.get(f"/api/tickets/{ticket['id']}")).status_code == 200
    assert (await beta_client.get(f"/api/tickets/{ticket['id']}")).status_code == 404
    assert (await beta_client.get("/api/tickets")).json()["items"] == []


async def test_api_rejects_other_tenant_or_unknown_assignee(seed, login):
    alpha, beta = await seed("alpha"), await seed("beta")
    client = await login(alpha)
    for assignee in (beta.user_id, uuid.uuid4()):
        response = await client.post(
            "/api/tickets",
            json={"type": "other", "title": "x", "assignee_id": str(assignee)},
        )
        assert response.status_code == 422


async def test_api_cannot_see_or_change_other_tenant_member(seed, login):
    alpha, beta = await seed("alpha"), await seed("beta")
    client = await login(alpha)
    emails = [m["email"] for m in (await client.get("/api/members")).json()]
    assert emails == [alpha.email]

    response = await client.patch(f"/api/members/{beta.user_id}", json={"is_active": False})
    assert response.status_code == 404


async def test_same_user_in_two_tenants_only_sees_logged_in_tenant(seed, login):
    alpha = await seed("alpha", email="shared@example.com")
    beta = await seed("beta", email="shared@example.com")
    alpha_client, beta_client = await login(alpha), await login(beta)
    await _ticket(alpha_client, "A 的工单")
    await _ticket(beta_client, "B 的工单")

    assert [t["title"] for t in (await alpha_client.get("/api/tickets")).json()["items"]] == [
        "A 的工单"
    ]
    assert [t["title"] for t in (await beta_client.get("/api/tickets")).json()["items"]] == [
        "B 的工单"
    ]
    assert (await beta_client.get("/api/auth/me")).json()["tenant"]["slug"] == "beta"
