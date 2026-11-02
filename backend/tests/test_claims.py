import asyncio

import pytest
from sqlalchemy import text

from app.claim_flow import TRANSITIONS, allowed_targets, check_transition
from app.ticket_flow import TransitionError
from tests.conftest import PASSWORD, Member, set_tenant

PNGS = [b"\x89PNG\r\n\x1a\n" + bytes([i]) for i in range(10)]


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
    for role in ("supervisor", "agent", "warehouse"):
        clients[role] = await login(await _member(admin_client, admin, role))
    return clients


async def _ticket(client, type="damaged", tracking_number="SF1"):
    response = await client.post(
        "/api/tickets",
        json={"type": type, "title": "外箱破损", "tracking_number": tracking_number},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_evidence(client, ticket_id, categories=None):
    categories = categories or ("label_photo", "package_photo", "damage_photo", "value_proof")
    for index, category in enumerate(categories):
        response = await client.post(
            f"/api/tickets/{ticket_id}/attachments",
            params={"category": category, "filename": f"{category}.png"},
            content=PNGS[index],
        )
        assert response.status_code == 201, response.text
    return response.json()


async def _open(client, ticket_id, **fields):
    body = {"carrier": "顺丰", "claim_amount_cents": 50000, **fields}
    return await client.post(f"/api/tickets/{ticket_id}/claim", json=body)


async def _move(client, ticket_id, version, to, **fields):
    return await client.post(
        f"/api/tickets/{ticket_id}/claim/transition", json={"to": to, "version": version, **fields}
    )


async def _submitted(client, tracking_number="SF1", amount=50000):
    ticket = await _ticket(client, tracking_number=tracking_number)
    await _add_evidence(client, ticket["id"])
    claim = (await _open(client, ticket["id"], claim_amount_cents=amount)).json()["claim"]
    response = await _move(client, ticket["id"], claim["version"], "submitted")
    assert response.status_code == 200, response.text
    return ticket["id"], response.json()["claim"]


async def _resolve(client, ticket_id):
    me = (await client.get("/api/auth/me")).json()["user_id"]
    current = (await client.get(f"/api/tickets/{ticket_id}")).json()
    current = (
        await client.post(
            f"/api/tickets/{ticket_id}/assign",
            json={"assignee_id": me, "version": current["version"]},
        )
    ).json()
    for to, fields in (("in_progress", {}), ("resolved", {"resolution": "已处理"})):
        current = (
            await client.post(
                f"/api/tickets/{ticket_id}/transition",
                json={"to": to, "version": current["version"], **fields},
            )
        ).json()
    return current


def test_transition_table_covers_every_path():
    assert set(TRANSITIONS) == {
        ("preparing", "submitted"),
        ("submitted", "approved"),
        ("submitted", "rejected"),
        ("rejected", "submitted"),
        ("approved", "paid"),
        ("preparing", "withdrawn"),
        ("submitted", "withdrawn"),
        ("rejected", "withdrawn"),
        ("withdrawn", "preparing"),
    }


def test_only_managers_are_offered_paid():
    assert allowed_targets("approved", can_manage=False) == []
    assert allowed_targets("approved", can_manage=True) == ["paid"]
    assert allowed_targets("paid", can_manage=True) == []


@pytest.mark.parametrize(
    ("source", "target", "fields", "status_code"),
    [
        ("paid", "submitted", {}, 409),
        ("preparing", "approved", {"approved_cents": 1}, 409),
        ("preparing", "submitted", {}, 422),
        ("preparing", "submitted", {"claim_amount_cents": 1, "missing_evidence": ["面单"]}, 422),
        ("submitted", "approved", {}, 422),
        ("submitted", "approved", {"claim_amount_cents": 100, "approved_cents": 101}, 422),
        ("submitted", "rejected", {}, 422),
        ("submitted", "rejected", {"rejection_code": "other", "note": "  "}, 422),
        ("approved", "paid", {"can_manage": False, "paid_cents": 1}, 403),
        ("approved", "paid", {"current_approved_cents": 100, "paid_cents": 101}, 422),
    ],
)
def test_check_transition_rejects(source, target, fields, status_code):
    with pytest.raises(TransitionError) as error:
        check_transition(source, target, **{"can_manage": True, **fields})
    assert error.value.status_code == status_code


async def test_full_claim_lifecycle_records_amounts_and_audit(team):
    client = team["agent"]
    ticket_id, claim = await _submitted(client)
    assert claim["status"] == "submitted"
    assert claim["submitted_at"] is not None

    response = await _move(client, ticket_id, claim["version"], "approved", approved_cents=40000)
    assert response.status_code == 200, response.text
    claim = response.json()["claim"]
    assert claim["approved_cents"] == 40000
    assert response.json()["claim_transitions"] == []

    # 到账由主管确认
    paid = await _move(client, ticket_id, claim["version"], "paid", paid_cents=40000)
    assert paid.status_code == 403
    paid = await _move(team["supervisor"], ticket_id, claim["version"], "paid", paid_cents=40000)
    assert paid.status_code == 200, paid.text
    detail = paid.json()
    assert detail["claim"]["paid_cents"] == 40000
    assert detail["claim"]["paid_at"] is not None
    assert detail["claim_blocks_closing"] is False

    kinds = [e["kind"] for e in detail["events"]]
    assert kinds.count("claim_opened") == 1
    assert kinds.count("claim_status_changed") == 3
    assert detail["events"][-1]["data"] == {"from": "approved", "to": "paid", "paid_cents": 40000}


async def test_submit_requires_required_evidence_and_amount(team):
    client = team["agent"]
    ticket = await _ticket(client)
    claim = (await _open(client, ticket["id"], claim_amount_cents=None)).json()["claim"]

    response = await _move(client, ticket["id"], claim["version"], "submitted")
    assert response.status_code == 422
    assert "索赔金额" in response.json()["detail"]

    patched = await client.patch(
        f"/api/tickets/{ticket['id']}/claim",
        json={"version": claim["version"], "claim_amount_cents": 100},
    )
    claim = patched.json()["claim"]
    await _add_evidence(client, ticket["id"], ["label_photo"])
    response = await _move(client, ticket["id"], claim["version"], "submitted")
    assert response.status_code == 422
    assert "外包装照片" in response.json()["detail"]
    assert "内件破损照片" in response.json()["detail"]
    assert "面单照片" not in response.json()["detail"]


async def test_voided_evidence_does_not_count_for_submission(team):
    client = team["agent"]
    ticket = await _ticket(client)
    detail = await _add_evidence(client, ticket["id"])
    target = detail["attachments"][0]
    await client.post(
        f"/api/tickets/{ticket['id']}/attachments/{target['id']}/void", json={"reason": "拍错了"}
    )
    claim = (await _open(client, ticket["id"])).json()["claim"]
    response = await _move(client, ticket["id"], claim["version"], "submitted")
    assert response.status_code == 422
    assert "面单照片" in response.json()["detail"]


async def test_submitted_claim_locks_evidence_until_rejected(team):
    client = team["agent"]
    ticket_id, claim = await _submitted(client)
    detail = (await client.get(f"/api/tickets/{ticket_id}")).json()
    attachment = detail["attachments"][0]
    assert attachment["can_void"] is False
    url = f"/api/tickets/{ticket_id}/attachments/{attachment['id']}/void"
    assert (await client.post(url, json={"reason": "x"})).status_code == 409

    # 被拒后可以调整材料，并且可以补充上传
    rejected = await _move(
        client, ticket_id, claim["version"], "rejected", rejection_code="packaging_intact"
    )
    assert rejected.json()["claim"]["rejection_code"] == "packaging_intact"
    assert (await client.post(url, json={"reason": "重新拍"})).status_code == 200


async def test_rejected_claim_can_be_resubmitted_with_reason(team):
    client = team["agent"]
    ticket_id, claim = await _submitted(client)
    claim = (
        await _move(client, ticket_id, claim["version"], "rejected", rejection_code="uninsured")
    ).json()["claim"]

    assert (await _move(client, ticket_id, claim["version"], "submitted")).status_code == 422
    response = await _move(
        client, ticket_id, claim["version"], "submitted", note="补充货值凭证", external_ref=" LP9 "
    )
    assert response.status_code == 200, response.text
    assert response.json()["claim"]["rejection_reason"] == ""
    assert response.json()["claim"]["external_ref"] == "LP9"


async def test_withdrawn_claim_can_be_reopened(team):
    client = team["agent"]
    ticket = await _ticket(client)
    claim = (await _open(client, ticket["id"])).json()["claim"]
    assert (await _move(client, ticket["id"], claim["version"], "withdrawn")).status_code == 422
    claim = (
        await _move(client, ticket["id"], claim["version"], "withdrawn", note="货主决定不赔了")
    ).json()["claim"]
    assert (await _open(client, ticket["id"])).status_code == 409
    response = await _move(client, ticket["id"], claim["version"], "preparing")
    assert response.json()["claim"]["status"] == "preparing"


async def test_edit_only_while_preparing_or_rejected(team):
    client = team["agent"]
    ticket_id, claim = await _submitted(client)
    url = f"/api/tickets/{ticket_id}/claim"
    frozen = await client.patch(url, json={"version": claim["version"], "carrier": "中通"})
    assert frozen.status_code == 409

    claim = (
        await _move(
            client, ticket_id, claim["version"], "rejected", rejection_code="other", note="x"
        )
    ).json()["claim"]
    response = await client.patch(
        url,
        json={
            "version": claim["version"],
            "carrier": "中通",
            "pickup_weight_g": 1200,
            "billed_weight_g": 800,
            "insured": True,
        },
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["claim"]["carrier"] == "中通"
    assert detail["claim"]["pickup_weight_g"] == 1200
    assert detail["claim"]["insured"] is True
    assert detail["events"][-1]["kind"] == "claim_updated"
    assert detail["events"][-1]["data"]["changes"]["carrier"] == "中通"

    # 没有变化时不产生版本号和审计记录
    version = detail["claim"]["version"]
    same = await client.patch(url, json={"version": version, "carrier": "中通"})
    assert same.json()["claim"]["version"] == version
    assert len(same.json()["events"]) == len(detail["events"])


async def test_stale_version_is_rejected(team):
    client = team["agent"]
    ticket_id, claim = await _submitted(client)
    first = await _move(
        client, ticket_id, claim["version"], "rejected", rejection_code="other", note="x"
    )
    assert first.status_code == 200
    stale = await _move(client, ticket_id, claim["version"], "withdrawn", note="y")
    assert stale.status_code == 409
    assert "其他人" in stale.json()["detail"]


async def test_concurrent_transitions_only_one_succeeds(team):
    ticket_id, claim = await _submitted(team["agent"])
    responses = await asyncio.gather(
        _move(
            team["agent"],
            ticket_id,
            claim["version"],
            "rejected",
            rejection_code="other",
            note="拒",
        ),
        _move(team["supervisor"], ticket_id, claim["version"], "approved", approved_cents=100),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]


async def test_concurrent_open_creates_one_claim(team):
    ticket = await _ticket(team["agent"])
    responses = await asyncio.gather(*(_open(team["agent"], ticket["id"]) for _ in range(4)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]


async def test_open_claim_blocks_closing_and_cancelling(team):
    client = team["supervisor"]
    ticket = await _ticket(client)
    await _resolve(client, ticket["id"])
    assert (await _open(client, ticket["id"])).status_code == 201

    detail = (await client.get(f"/api/tickets/{ticket['id']}")).json()
    assert detail["claim_blocks_closing"] is True
    blocked = await client.post(
        f"/api/tickets/{ticket['id']}/transition",
        json={"to": "closed", "version": detail["version"]},
    )
    assert blocked.status_code == 409
    assert "索赔" in blocked.json()["detail"]
    after = (await client.get(f"/api/tickets/{ticket['id']}")).json()
    assert after["status"] == "resolved"
    assert after["version"] == detail["version"]

    claim = (
        await _move(client, ticket["id"], detail["claim"]["version"], "withdrawn", note="不赔了")
    ).json()["claim"]
    assert claim["status"] == "withdrawn"
    closed = await client.post(
        f"/api/tickets/{ticket['id']}/transition",
        json={"to": "closed", "version": detail["version"]},
    )
    assert closed.status_code == 200


async def test_closing_and_opening_claim_race_never_leaves_open_claim_on_closed_ticket(team):
    client = team["supervisor"]
    for index in range(5):
        ticket = await _ticket(client, tracking_number=f"SF-race-{index}")
        current = await _resolve(client, ticket["id"])
        close, opened = await asyncio.gather(
            client.post(
                f"/api/tickets/{ticket['id']}/transition",
                json={"to": "closed", "version": current["version"]},
            ),
            _open(client, ticket["id"]),
        )
        detail = (await client.get(f"/api/tickets/{ticket['id']}")).json()
        assert sorted([close.status_code, opened.status_code]) in ([200, 409], [201, 409])
        assert not (detail["status"] == "closed" and detail["claim"] is not None)


async def test_finished_ticket_freezes_claim(team):
    client = team["supervisor"]
    ticket_id, claim = await _submitted(client)
    claim = (
        await _move(
            client, ticket_id, claim["version"], "rejected", rejection_code="other", note="拒"
        )
    ).json()["claim"]
    current = await _resolve(client, ticket_id)
    closed = await client.post(
        f"/api/tickets/{ticket_id}/transition", json={"to": "closed", "version": current["version"]}
    )
    assert closed.status_code == 200
    assert closed.json()["claim_transitions"] == []
    response = await _move(client, ticket_id, claim["version"], "submitted", note="再试")
    assert response.status_code == 409


async def test_address_change_ticket_has_no_claim(team):
    client = team["agent"]
    ticket = await _ticket(client, type="address_change")
    assert ticket["claim_enabled"] is False
    assert (await _open(client, ticket["id"])).status_code == 422


async def test_warehouse_cannot_see_or_change_claims(team):
    agent, warehouse = team["agent"], team["warehouse"]
    ticket_id, claim = await _submitted(agent)

    detail = (await warehouse.get(f"/api/tickets/{ticket_id}")).json()
    assert detail["claim"] is None
    assert detail["claim_enabled"] is False
    assert detail["claim_transitions"] == []
    assert not [e for e in detail["events"] if e["kind"].startswith("claim_")]
    # 仓管仍要能上传证据
    assert detail["can_upload"] is True

    assert (await warehouse.get("/api/claims")).status_code == 403
    assert (await warehouse.get("/api/claims/summary")).status_code == 403
    assert (await _open(warehouse, ticket_id)).status_code == 403
    moved = await _move(
        warehouse, ticket_id, claim["version"], "rejected", rejection_code="other", note="x"
    )
    assert moved.status_code == 403
    patched = await warehouse.patch(
        f"/api/tickets/{ticket_id}/claim", json={"version": claim["version"], "carrier": "x"}
    )
    assert patched.status_code == 403


async def test_invalid_amounts_are_rejected(team):
    client = team["agent"]
    ticket = await _ticket(client)
    for amount in (0, -1, 1.5, 10**13):
        assert (await _open(client, ticket["id"], claim_amount_cents=amount)).status_code == 422
    assert (await _open(client, ticket["id"], carrier="   ")).status_code == 422


async def test_list_filters_and_pages_claims(team):
    client = team["agent"]
    numbers = []
    for index in range(3):
        ticket = await _ticket(client, tracking_number=f"SF{index}")
        numbers.append(ticket["number"])
        await _open(client, ticket["id"])
    submitted_id, _ = await _submitted(client, tracking_number="SF9")

    page = (await client.get("/api/claims", params={"limit": 2})).json()
    assert len(page["items"]) == 2
    assert page["items"][0]["tracking_number"] == "SF9"
    rest = (
        await client.get("/api/claims", params={"limit": 2, "before": page["next_before"]})
    ).json()
    assert [item["ticket_number"] for item in rest["items"]] == numbers[:2][::-1]
    assert rest["next_before"] is None

    only = (await client.get("/api/claims", params={"status": "submitted"})).json()
    assert [item["ticket_id"] for item in only["items"]] == [submitted_id]


async def test_summary_reports_recovery_by_carrier(team):
    agent, supervisor = team["agent"], team["supervisor"]

    # 顺丰：索赔 500 元到账 400 元；再一笔 100 元被拒；再一笔 300 元进行中
    ticket_id, claim = await _submitted(agent, "SF1", 50000)
    claim = (
        await _move(agent, ticket_id, claim["version"], "approved", approved_cents=40000)
    ).json()["claim"]
    await _move(supervisor, ticket_id, claim["version"], "paid", paid_cents=40000)
    ticket_id, claim = await _submitted(agent, "SF2", 10000)
    await _move(agent, ticket_id, claim["version"], "rejected", rejection_code="uninsured")
    await _submitted(agent, "SF3", 30000)
    # 中通：撤回的不算
    other = await _ticket(agent, tracking_number="ZT1")
    claim = (await _open(agent, other["id"], carrier="中通")).json()["claim"]
    await _move(agent, other["id"], claim["version"], "withdrawn", note="不赔")
    # 韵达：没填金额
    third = await _ticket(agent, tracking_number="YD1")
    await _open(agent, third["id"], carrier="韵达", claim_amount_cents=None)

    summary = (await agent.get("/api/claims/summary")).json()
    assert [c["carrier"] for c in summary["carriers"]] == ["顺丰", "韵达"]
    assert summary["carriers"][0] == {
        "carrier": "顺丰",
        "total": 3,
        "open": 1,
        "rejected": 1,
        "paid": 1,
        "claimed_cents": 90000,
        "settled_claimed_cents": 60000,
        "paid_cents": 40000,
    }
    assert summary["total"]["total"] == 4
    assert summary["total"]["open"] == 2
    assert summary["total"]["paid_cents"] == 40000


async def test_other_tenant_cannot_see_claims(seed, login, app_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    alpha_client, beta_client = await login(alpha), await login(beta)
    ticket = await _ticket(alpha_client)
    claim = (await _open(alpha_client, ticket["id"])).json()["claim"]

    assert (await beta_client.get("/api/claims")).json()["items"] == []
    assert (await beta_client.get("/api/claims/summary")).json()["carriers"] == []
    assert (await _open(beta_client, ticket["id"])).status_code == 404
    moved = await _move(beta_client, ticket["id"], claim["version"], "withdrawn", note="x")
    assert moved.status_code == 404

    async with app_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM ticket_claims")) == 0
        await set_tenant(connection, beta.tenant_id)
        assert await connection.scalar(text("SELECT count(*) FROM ticket_claims")) == 0
