import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import file_store
from tests.conftest import PASSWORD, Member, set_tenant

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body"
PDF = b"%PDF-1.7\nbody"


async def _member(admin_client, admin: Member, role: str) -> Member:
    email = f"{role}@{admin.tenant_slug}.example.com"
    response = await admin_client.post(
        "/api/members",
        json={"email": email, "display_name": role, "role": role, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return Member(admin.tenant_id, response.json()["user_id"], admin.tenant_slug, email)


@pytest.fixture
async def team(seed, login):
    admin = await seed("alpha")
    admin_client = await login(admin)
    clients = {"admin": admin_client}
    for role in ("warehouse", "agent"):
        clients[role] = await login(await _member(admin_client, admin, role))
    return clients


async def _ticket(client, **fields):
    response = await client.post(
        "/api/tickets", json={"type": "damaged", "title": "外箱破损", **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload(client, ticket_id, content=PNG, category="package_photo", filename="箱子.png"):
    return await client.post(
        f"/api/tickets/{ticket_id}/attachments",
        params={"category": category, "filename": filename},
        content=content,
    )


def _content_url(ticket_id, attachment_id):
    return f"/api/tickets/{ticket_id}/attachments/{attachment_id}/content"


async def _finish(client, ticket):
    assignee = (await client.get("/api/auth/me")).json()["user_id"]
    current = (
        await client.post(
            f"/api/tickets/{ticket['id']}/assign",
            json={"assignee_id": assignee, "version": ticket["version"]},
        )
    ).json()
    for to, fields in (("in_progress", {}), ("resolved", {"resolution": "已处理"})):
        current = (
            await client.post(
                f"/api/tickets/{ticket['id']}/transition",
                json={"to": to, "version": current["version"], **fields},
            )
        ).json()
    return current


async def test_upload_and_download_keep_content_and_record_audit(team):
    client = team["warehouse"]
    ticket = await _ticket(client)
    response = await _upload(client, ticket["id"])
    assert response.status_code == 201, response.text
    detail = response.json()

    attachment = detail["attachments"][0]
    assert attachment["filename"] == "箱子.png"
    assert attachment["content_type"] == "image/png"
    assert attachment["size_bytes"] == len(PNG)
    assert attachment["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert attachment["uploader_name"] == "warehouse"
    event = detail["events"][-1]
    assert event["kind"] == "attachment_added"
    assert event["data"]["sha256"] == attachment["sha256"]

    download = await client.get(_content_url(ticket["id"], attachment["id"]))
    assert download.status_code == 200
    assert download.content == PNG
    assert download.headers["content-type"] == "image/png"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-security-policy"] == "sandbox"
    assert download.headers["content-disposition"].startswith("inline")


async def test_checklist_counts_uploads_for_ticket_type(team):
    client = team["warehouse"]
    ticket = await _ticket(client)
    levels = {item["category"]: item for item in ticket["evidence_checklist"]}
    assert levels["damage_photo"]["level"] == "required"
    assert levels["packing_video"]["level"] == "recommended"
    assert levels["tracking_screenshot"]["level"] == "optional"

    detail = (await _upload(client, ticket["id"], category="damage_photo")).json()
    counts = {item["category"]: item["count"] for item in detail["evidence_checklist"]}
    assert counts["damage_photo"] == 1
    assert counts["package_photo"] == 0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (PDF, "application/pdf"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00", "video/mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00", "video/quicktime"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "image/heic"),
    ],
)
async def test_type_comes_from_content_not_from_client(team, content, expected):
    client = team["warehouse"]
    ticket = await _ticket(client)
    response = await client.post(
        f"/api/tickets/{ticket['id']}/attachments",
        params={"category": "other", "filename": "whatever.txt"},
        content=content,
        headers={"Content-Type": "text/html"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["attachments"][0]["content_type"] == expected


@pytest.mark.parametrize(
    "content",
    [b"<html><script>alert(1)</script></html>", b"<svg xmlns='x'/>", b"MZ\x90\x00"],
)
async def test_unsupported_content_is_rejected_even_with_image_name(team, settings, content):
    client = team["warehouse"]
    ticket = await _ticket(client)
    response = await _upload(client, ticket["id"], content=content, filename="photo.png")
    assert response.status_code == 415
    assert (await client.get(f"/api/tickets/{ticket['id']}")).json()["attachments"] == []
    assert _stored_files(settings) == []


def _stored_files(settings) -> list[Path]:
    return [path for path in Path(settings.attachment_dir).rglob("*") if path.is_file()]


async def test_empty_and_oversized_uploads_leave_no_files(team, settings, monkeypatch):
    client = team["warehouse"]
    ticket = await _ticket(client)
    assert (await _upload(client, ticket["id"], content=b"")).status_code == 422

    monkeypatch.setattr(file_store, "MAX_BYTES", 16)
    response = await _upload(client, ticket["id"], content=PNG + b"x" * 64)
    assert response.status_code == 413
    assert _stored_files(settings) == []


async def test_same_file_is_not_stored_twice(team, settings):
    client = team["warehouse"]
    ticket = await _ticket(client)
    assert (await _upload(client, ticket["id"])).status_code == 201
    assert (await _upload(client, ticket["id"], filename="改名.png")).status_code == 409
    assert len(_stored_files(settings)) == 1

    # 另一张工单可以用同一个文件
    other = await _ticket(client, tracking_number="SF2")
    assert (await _upload(client, other["id"])).status_code == 201


async def test_concurrent_identical_uploads_keep_one(team, settings):
    client = team["warehouse"]
    ticket = await _ticket(client)
    responses = await asyncio.gather(*(_upload(client, ticket["id"]) for _ in range(5)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]
    assert len(_stored_files(settings)) == 1


async def test_filename_cannot_affect_storage_path(team, settings):
    client = team["warehouse"]
    ticket = await _ticket(client)
    response = await _upload(client, ticket["id"], filename="../../../etc/pass\nwd.png")
    assert response.status_code == 201
    attachment = response.json()["attachments"][0]
    assert attachment["filename"] == "passwd.png"

    files = _stored_files(settings)
    assert [path.name for path in files] == [attachment["id"]]
    assert files[0].is_relative_to(settings.attachment_dir)


async def test_upload_limit_per_ticket(team, monkeypatch):
    from app.routers import attachments

    monkeypatch.setattr(attachments, "MAX_PER_TICKET", 1)
    client = team["warehouse"]
    ticket = await _ticket(client)
    assert (await _upload(client, ticket["id"])).status_code == 201
    assert (await _upload(client, ticket["id"], content=JPEG)).status_code == 409


async def test_upload_does_not_hold_a_database_connection(app, team):
    """慢速上传期间连接要还给连接池，否则几个手机就能占满连接池。"""
    client = team["warehouse"]
    ticket = await _ticket(client)
    pool = app.state.engine.pool
    release = asyncio.Event()
    started = asyncio.Event()
    checked_out_during_upload = []

    async def slow_body():
        yield PNG[:4]
        started.set()
        await release.wait()
        yield PNG[4:]

    upload = asyncio.create_task(
        client.post(
            f"/api/tickets/{ticket['id']}/attachments",
            params={"category": "other", "filename": "a.png"},
            content=slow_body(),
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)
    checked_out_during_upload.append(pool.checkedout())
    release.set()
    response = await upload
    assert response.status_code == 201, response.text
    assert checked_out_during_upload == [0]


async def test_cannot_upload_or_void_on_finished_ticket(team):
    client = team["admin"]
    ticket = await _ticket(client)
    attachment = (await _upload(client, ticket["id"])).json()["attachments"][0]
    resolved = await _finish(client, ticket)
    closed = await client.post(
        f"/api/tickets/{ticket['id']}/transition",
        json={"to": "closed", "version": resolved["version"]},
    )
    assert closed.status_code == 200
    assert closed.json()["can_upload"] is False
    assert closed.json()["attachments"][0]["can_void"] is False

    assert (await _upload(client, ticket["id"], content=JPEG)).status_code == 409
    voided = await client.post(
        f"/api/tickets/{ticket['id']}/attachments/{attachment['id']}/void",
        json={"reason": "传错了"},
    )
    assert voided.status_code == 409
    # 已结束的工单证据仍可查看
    download = await client.get(_content_url(ticket["id"], attachment["id"]))
    assert download.status_code == 200


async def test_void_keeps_record_and_file_and_allows_reupload(team):
    client = team["warehouse"]
    ticket = await _ticket(client)
    attachment = (await _upload(client, ticket["id"], category="damage_photo")).json()[
        "attachments"
    ][0]
    assert attachment["can_void"] is True

    response = await client.post(
        f"/api/tickets/{ticket['id']}/attachments/{attachment['id']}/void",
        json={"reason": "拍错工单了"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["attachments"][0]["voided_at"] is not None
    assert detail["attachments"][0]["void_reason"] == "拍错工单了"
    assert detail["attachments"][0]["can_void"] is False
    assert detail["events"][-1]["kind"] == "attachment_voided"
    counts = {item["category"]: item["count"] for item in detail["evidence_checklist"]}
    assert counts["damage_photo"] == 0
    assert (await client.get(_content_url(ticket["id"], attachment["id"]))).content == PNG

    assert (await _upload(client, ticket["id"], category="damage_photo")).status_code == 201


async def test_only_uploader_or_manager_can_void(team):
    ticket = await _ticket(team["warehouse"])
    attachment = (await _upload(team["warehouse"], ticket["id"])).json()["attachments"][0]
    url = f"/api/tickets/{ticket['id']}/attachments/{attachment['id']}/void"

    detail = (await team["agent"].get(f"/api/tickets/{ticket['id']}")).json()
    assert detail["attachments"][0]["can_void"] is False
    assert (await team["agent"].post(url, json={"reason": "x"})).status_code == 403
    assert (await team["admin"].post(url, json={"reason": "  "})).status_code == 422
    assert (await team["admin"].post(url, json={"reason": "主管作废"})).status_code == 200
    assert (await team["admin"].post(url, json={"reason": "再来"})).status_code == 409


async def test_other_tenant_cannot_touch_attachments(seed, login, settings):
    alpha, beta = await seed("alpha"), await seed("beta")
    alpha_client, beta_client = await login(alpha), await login(beta)
    ticket = await _ticket(alpha_client)
    attachment = (await _upload(alpha_client, ticket["id"])).json()["attachments"][0]

    assert (await _upload(beta_client, ticket["id"], content=JPEG)).status_code == 404
    assert (await beta_client.get(_content_url(ticket["id"], attachment["id"]))).status_code == 404
    void = await beta_client.post(
        f"/api/tickets/{ticket['id']}/attachments/{attachment['id']}/void", json={"reason": "x"}
    )
    assert void.status_code == 404
    # 被拒绝的上传不留文件
    assert len(_stored_files(settings)) == 1

    beta_ticket = await _ticket(beta_client)
    crossed = await beta_client.get(_content_url(beta_ticket["id"], attachment["id"]))
    assert crossed.status_code == 404


async def test_attachment_must_belong_to_ticket_in_url(team):
    client = team["warehouse"]
    ticket = await _ticket(client)
    other = await _ticket(client, tracking_number="SF2")
    attachment = (await _upload(client, ticket["id"])).json()["attachments"][0]
    assert (await client.get(_content_url(other["id"], attachment["id"]))).status_code == 404


async def test_requires_login(team, client_factory):
    ticket = await _ticket(team["warehouse"])
    attachment = (await _upload(team["warehouse"], ticket["id"])).json()["attachments"][0]
    anonymous = client_factory()
    assert (await _upload(anonymous, ticket["id"], content=JPEG)).status_code == 401
    assert (await anonymous.get(_content_url(ticket["id"], attachment["id"]))).status_code == 401


async def test_missing_file_reports_not_found(team, settings):
    client = team["warehouse"]
    ticket = await _ticket(client)
    attachment = (await _upload(client, ticket["id"])).json()["attachments"][0]
    for path in _stored_files(settings):
        os.remove(path)
    response = await client.get(_content_url(ticket["id"], attachment["id"]))
    assert response.status_code == 404


async def test_db_keeps_attachment_content_immutable(seed, login, app_engine):
    alpha = await seed("alpha")
    client = await login(alpha)
    ticket = await _ticket(client)
    await _upload(client, ticket["id"])

    for sql in (
        "DELETE FROM ticket_attachments",
        "UPDATE ticket_attachments SET sha256 = 'x'",
        "UPDATE ticket_attachments SET filename = 'x'",
    ):
        async with app_engine.connect() as connection:
            await set_tenant(connection, alpha.tenant_id)
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(sql))


async def test_db_isolates_attachments_by_tenant(seed, login, app_engine, owner_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    client = await login(alpha)
    await _upload(client, (await _ticket(client))["id"])

    async with app_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM ticket_attachments")) == 0
        await set_tenant(connection, beta.tenant_id)
        assert await connection.scalar(text("SELECT count(*) FROM ticket_attachments")) == 0
    async with app_engine.connect() as connection:
        await set_tenant(connection, alpha.tenant_id)
        assert await connection.scalar(text("SELECT count(*) FROM ticket_attachments")) == 1
    async with owner_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM ticket_attachments")) == 0
