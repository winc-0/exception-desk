from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import text

from app.security import COOKIE_NAME
from tests.conftest import ORIGIN, PASSWORD


def _body(member, password=PASSWORD):
    return {"tenant_slug": member.tenant_slug, "email": member.email, "password": password}


async def test_login_sets_protected_cookie(seed, client_factory):
    member = await seed("alpha")
    client = client_factory()
    response = await client.post("/api/auth/login", json=_body(member))

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "password" not in response.text
    me = (await client.get("/api/auth/me")).json()
    assert me["role"] == "admin" and "members:manage" in me["permissions"]


async def test_cookie_is_secure_by_default(seed, client_factory, app):
    app.state.settings = app.state.settings.model_copy(update={"cookie_secure": True})
    member = await seed("alpha")
    response = await client_factory().post("/api/auth/login", json=_body(member))
    assert "secure" in response.headers["set-cookie"].lower()


async def test_login_failures_look_identical(seed, client_factory):
    member = await seed("alpha")
    await seed("beta")
    client = client_factory()
    cases = [
        _body(member, "wrong-password"),
        {**_body(member), "email": "nobody@example.com"},
        {**_body(member), "tenant_slug": "no-such-company"},
        {**_body(member), "tenant_slug": "beta"},  # 密码正确，但不是这家公司的成员
    ]
    responses = [await client.post("/api/auth/login", json=body) for body in cases]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1


async def test_email_is_case_insensitive(seed, client_factory):
    member = await seed("alpha")
    body = {**_body(member), "email": member.email.upper()}
    assert (await client_factory().post("/api/auth/login", json=body)).status_code == 200


async def test_account_locks_after_repeated_failures(seed, client_factory, owner_engine):
    member = await seed("alpha")
    client = client_factory()
    for _ in range(5):
        response = await client.post("/api/auth/login", json=_body(member, "wrong-password"))
        assert response.status_code == 401
    # 已锁定，正确密码也进不去
    assert (await client.post("/api/auth/login", json=_body(member))).status_code == 401

    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET locked_until = :t"),
            {"t": datetime.now(UTC) - timedelta(seconds=1)},
        )
    assert (await client.post("/api/auth/login", json=_body(member))).status_code == 200


async def test_requests_without_valid_session_are_rejected(seed, client_factory, settings):
    member = await seed("alpha")
    secret = settings.jwt_secret.get_secret_value()
    good = {
        "sub": str(member.user_id),
        "tid": str(member.tenant_id),
        "ver": 1,
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    bad_tokens = {
        "garbage": "not-a-token",
        "wrong key": jwt.encode(good, "k" * 40, algorithm="HS256"),
        "alg none": jwt.encode(good, "", algorithm="none"),
        "expired": jwt.encode(
            {**good, "exp": datetime.now(UTC) - timedelta(seconds=1)}, secret, algorithm="HS256"
        ),
        "no expiry": jwt.encode(
            {k: v for k, v in good.items() if k != "exp"}, secret, algorithm="HS256"
        ),
        "old version": jwt.encode({**good, "ver": 0}, secret, algorithm="HS256"),
    }

    assert (await client_factory().get("/api/tickets")).status_code == 401
    for name, token in bad_tokens.items():
        client = client_factory()
        client.cookies.set(COOKIE_NAME, token)
        assert (await client.get("/api/tickets")).status_code == 401, name

    client = client_factory()
    client.cookies.set(COOKIE_NAME, jwt.encode(good, secret, algorithm="HS256"))
    assert (await client.get("/api/tickets")).status_code == 200


async def test_forged_tenant_claim_needs_membership(seed, client_factory, settings):
    alpha, beta = await seed("alpha"), await seed("beta")
    token = jwt.encode(
        {
            "sub": str(alpha.user_id),
            "tid": str(beta.tenant_id),
            "ver": 1,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    client = client_factory()
    client.cookies.set(COOKIE_NAME, token)
    assert (await client.get("/api/tickets")).status_code == 401


async def test_logout_invalidates_copied_cookie(seed, login, client_factory):
    client = await login(await seed("alpha"))
    copied = client.cookies[COOKIE_NAME]

    assert (await client.post("/api/auth/logout")).status_code == 204
    other = client_factory()
    other.cookies.set(COOKIE_NAME, copied)
    assert (await other.get("/api/auth/me")).status_code == 401


async def test_change_password(seed, login, client_factory):
    member = await seed("alpha")
    client = await login(member)
    other_device = await login(member)
    new_password = "a-brand-new-password"

    wrong = await client.post(
        "/api/auth/password", json={"current_password": "nope", "new_password": new_password}
    )
    assert wrong.status_code == 400
    short = await client.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
    )
    assert short.status_code == 422

    done = await client.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": new_password}
    )
    assert done.status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 200
    assert (await other_device.get("/api/auth/me")).status_code == 401
    fresh = client_factory()
    assert (await fresh.post("/api/auth/login", json=_body(member))).status_code == 401
    relogin = await fresh.post("/api/auth/login", json=_body(member, new_password))
    assert relogin.status_code == 200


async def test_cross_origin_writes_are_blocked(seed, login):
    client = await login(await seed("alpha"))
    body = {"type": "other", "title": "x"}

    evil = await client.post("/api/tickets", json=body, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    same = await client.post("/api/tickets", json=body, headers={"Origin": ORIGIN})
    assert same.status_code == 201
    # 读请求不受影响
    read = await client.get("/api/tickets", headers={"Origin": "http://evil.example"})
    assert read.status_code == 200


async def test_health(client_factory, app):
    assert (await client_factory().get("/health")).json() == {"status": "ok"}
