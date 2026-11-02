import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.ticket_flow import TRANSITIONS, Actor, TransitionError, allowed_targets, check_transition
from tests.conftest import PASSWORD, Member, set_tenant

ALL_STATUSES = ["open", "in_progress", "waiting", "resolved", "closed", "cancelled"]
ASSIGNEE = Actor(is_assignee=True, can_work=True, can_manage=False)
BYSTANDER = Actor(is_assignee=False, can_work=True, can_manage=False)
MANAGER = Actor(is_assignee=False, can_work=True, can_manage=True)


async def _add_member(admin_client, admin: Member, email: str, role: str) -> Member:
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
    members = {"admin": (admin, admin_client)}
    for role in ("supervisor", "agent", "warehouse"):
        member = await _add_member(admin_client, admin, f"{role}@alpha.example.com", role)
        members[role] = (member, await login(member))
    other_agent = await _add_member(admin_client, admin, "agent2@alpha.example.com", "agent")
    members["agent2"] = (other_agent, await login(other_agent))
    return members


async def _create(client, **fields):
    response = await client.post(
        "/api/tickets", json={"type": "damaged", "title": "外箱破损", **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _transition(client, ticket, to, **fields):
    return await client.post(
        f"/api/tickets/{ticket['id']}/transition",
        json={"to": to, "version": ticket["version"], **fields},
    )


async def _assign(client, ticket, assignee_id):
    return await client.post(
        f"/api/tickets/{ticket['id']}/assign",
        json={
            "assignee_id": str(assignee_id) if assignee_id else None,
            "version": ticket["version"],
        },
    )


# --- 状态机规则（纯函数）


@pytest.mark.parametrize("source", ALL_STATUSES)
@pytest.mark.parametrize("target", ALL_STATUSES)
def test_every_undefined_transition_is_rejected(source, target):
    if (source, target) in TRANSITIONS:
        return
    with pytest.raises(TransitionError) as error:
        check_transition(source, target, actor=MANAGER, has_assignee=True, note="x", resolution="x")
    assert error.value.status_code == 409


@pytest.mark.parametrize("terminal", ["closed", "cancelled"])
def test_terminal_statuses_have_no_exit(terminal):
    assert allowed_targets(terminal, actor=MANAGER, has_assignee=True) == []


def test_allowed_targets_agrees_with_check_transition():
    # 前端显示的按钮和后端实际放行的操作不能不一致
    for actor in (ASSIGNEE, BYSTANDER, MANAGER):
        for has_assignee in (True, False):
            for source in ALL_STATUSES:
                allowed = set(allowed_targets(source, actor=actor, has_assignee=has_assignee))
                for target in ALL_STATUSES:
                    try:
                        check_transition(
                            source,
                            target,
                            actor=actor,
                            has_assignee=has_assignee,
                            note="x",
                            resolution="x",
                        )
                        passed = True
                    except TransitionError:
                        passed = False
                    assert passed == (target in allowed), (actor, source, target)


# --- 完整流程


async def test_full_damaged_ticket_lifecycle(team):
    agent, agent_client = team["agent"]
    _, warehouse_client = team["warehouse"]
    _, supervisor_client = team["supervisor"]

    ticket = await _create(warehouse_client, tracking_number="SF001")
    assert ticket["status"] == "open" and ticket["number"] == 1 and ticket["version"] == 1

    ticket = (await _assign(agent_client, ticket, agent.user_id)).json()
    assert ticket["assignee_name"] == "agent"

    ticket = (await _transition(agent_client, ticket, "in_progress")).json()
    assert ticket["status"] == "in_progress"

    ticket = (await _transition(agent_client, ticket, "waiting", note="等承运商回复")).json()
    assert ticket["status"] == "waiting"

    ticket = (await _transition(agent_client, ticket, "in_progress")).json()

    comment = await warehouse_client.post(
        f"/api/tickets/{ticket['id']}/comments", json={"body": "已拍照留存"}
    )
    assert comment.status_code == 201

    ticket = (
        await _transition(agent_client, ticket, "resolved", resolution="承运商赔付 200 元")
    ).json()
    assert ticket["status"] == "resolved"
    assert ticket["resolution"] == "承运商赔付 200 元"

    ticket = (await _transition(supervisor_client, ticket, "closed")).json()
    assert ticket["status"] == "closed"
    assert ticket["allowed_transitions"] == []

    kinds = [
        (e["kind"], e["from_status"], e["to_status"], e["actor_name"]) for e in ticket["events"]
    ]
    assert kinds == [
        ("created", None, "open", "warehouse"),
        ("assigned", None, None, "agent"),
        ("status_changed", "open", "in_progress", "agent"),
        ("status_changed", "in_progress", "waiting", "agent"),
        ("status_changed", "waiting", "in_progress", "agent"),
        ("commented", None, None, "warehouse"),
        ("status_changed", "in_progress", "resolved", "agent"),
        ("status_changed", "resolved", "closed", "supervisor"),
    ]
    assert ticket["events"][3]["body"] == "等承运商回复"
    assert ticket["events"][1]["data"]["assignee_name"] == "agent"


async def test_required_fields(team):
    agent, client = team["agent"]
    _, supervisor = team["supervisor"]
    ticket = await _create(client)
    # 没有处理人：客服不是处理人，所以是 403；主管有权限，但要先指派
    assert (await _transition(client, ticket, "in_progress")).status_code == 403
    assert (await _transition(supervisor, ticket, "in_progress")).status_code == 422

    ticket = (await _assign(client, ticket, agent.user_id)).json()
    ticket = (await _transition(client, ticket, "in_progress")).json()
    assert (await _transition(client, ticket, "waiting")).status_code == 422
    assert (await _transition(client, ticket, "resolved", resolution="   ")).status_code == 422


# --- 权限


async def test_only_assignee_or_manager_can_work(team):
    agent, agent_client = team["agent"]
    _, other_client = team["agent2"]
    _, warehouse_client = team["warehouse"]
    _, supervisor_client = team["supervisor"]

    ticket = await _create(agent_client)
    ticket = (await _assign(agent_client, ticket, agent.user_id)).json()

    assert (await _transition(other_client, ticket, "in_progress")).status_code == 403
    assert (await _transition(warehouse_client, ticket, "in_progress")).status_code == 403
    assert (await _transition(supervisor_client, ticket, "in_progress")).status_code == 200


async def test_assignee_cannot_close_or_cancel_own_ticket(team):
    agent, client = team["agent"]
    ticket = await _create(client)
    ticket = (await _assign(client, ticket, agent.user_id)).json()
    ticket = (await _transition(client, ticket, "in_progress")).json()

    assert (await _transition(client, ticket, "cancelled", note="不想做")).status_code == 403
    ticket = (await _transition(client, ticket, "resolved", resolution="已处理")).json()
    assert (await _transition(client, ticket, "closed")).status_code == 403
    assert (await _transition(client, ticket, "in_progress", note="再看看")).status_code == 403


async def test_assignment_rules(team):
    agent, agent_client = team["agent"]
    other, other_client = team["agent2"]
    warehouse, warehouse_client = team["warehouse"]
    _, supervisor_client = team["supervisor"]

    ticket = await _create(agent_client)
    # 客服不能指派别人，仓管不能领取
    assert (await _assign(agent_client, ticket, other.user_id)).status_code == 403
    assert (await _assign(warehouse_client, ticket, warehouse.user_id)).status_code == 403
    # 客服可以领取无人处理的工单
    claimed = await _assign(agent_client, ticket, agent.user_id)
    assert claimed.status_code == 200
    ticket = claimed.json()
    # 已有处理人时，别的客服不能抢
    assert (await _assign(other_client, ticket, other.user_id)).status_code == 403
    # 主管可以改派，包括指派给仓管
    reassigned = await _assign(supervisor_client, ticket, warehouse.user_id)
    assert reassigned.status_code == 200
    ticket = reassigned.json()
    assert ticket["events"][-1]["data"]["previous_assignee_id"] == str(agent.user_id)


async def test_non_manager_cannot_set_assignee_on_create(team):
    other, _ = team["agent2"]
    agent, client = team["agent"]
    denied = await client.post(
        "/api/tickets",
        json={"type": "other", "title": "x", "assignee_id": str(other.user_id)},
    )
    assert denied.status_code == 403
    assert (await _create(client, assignee_id=str(agent.user_id)))["assignee_id"] == str(
        agent.user_id
    )


async def test_cannot_unassign_active_ticket_or_assign_finished_one(team):
    agent, _ = team["agent"]
    _, client = team["supervisor"]
    ticket = await _create(client)
    ticket = (await _assign(client, ticket, agent.user_id)).json()
    ticket = (await _transition(client, ticket, "in_progress")).json()
    assert (await _assign(client, ticket, None)).status_code == 422

    ticket = (await _transition(client, ticket, "cancelled", note="重复上报")).json()
    assert (await _assign(client, ticket, agent.user_id)).status_code == 409


async def test_cannot_assign_deactivated_member(team):
    agent, _ = team["agent"]
    _, admin_client = team["admin"]
    await admin_client.patch(f"/api/members/{agent.user_id}", json={"is_active": False})
    ticket = await _create(admin_client)
    assert (await _assign(admin_client, ticket, agent.user_id)).status_code == 422


async def test_allowed_actions_reflect_viewer(team):
    agent, agent_client = team["agent"]
    _, other_client = team["agent2"]
    _, supervisor_client = team["supervisor"]
    ticket = await _create(agent_client)
    url = f"/api/tickets/{ticket['id']}"

    viewed = (await agent_client.get(url)).json()
    assert viewed["can_claim"] is True and viewed["can_assign"] is False

    await _assign(agent_client, ticket, agent.user_id)
    assert (await agent_client.get(url)).json()["allowed_transitions"] == ["in_progress"]
    assert (await other_client.get(url)).json()["allowed_transitions"] == []
    manager_view = (await supervisor_client.get(url)).json()
    assert set(manager_view["allowed_transitions"]) == {"in_progress", "cancelled"}
    assert manager_view["can_assign"] is True


# --- 并发和乐观锁


async def test_stale_version_is_rejected(team):
    agent, client = team["agent"]
    ticket = await _create(client)
    await _assign(client, ticket, agent.user_id)  # 版本号变成 2
    stale = await _assign(client, ticket, agent.user_id)
    assert stale.status_code == 409


async def test_two_agents_claiming_only_one_wins(team):
    agent, agent_client = team["agent"]
    other, other_client = team["agent2"]
    for _ in range(5):
        ticket = await _create(agent_client)
        responses = await asyncio.gather(
            _assign(agent_client, ticket, agent.user_id),
            _assign(other_client, ticket, other.user_id),
        )
        assert sorted(r.status_code for r in responses) == [200, 409]

        final = (await agent_client.get(f"/api/tickets/{ticket['id']}")).json()
        assert final["version"] == 2
        assert [e["kind"] for e in final["events"]] == ["created", "assigned"]


async def test_concurrent_transitions_only_apply_once(team):
    agent, client = team["agent"]
    _, supervisor = team["supervisor"]
    ticket = await _create(client)
    ticket = (await _assign(client, ticket, agent.user_id)).json()
    ticket = (await _transition(client, ticket, "in_progress")).json()

    responses = await asyncio.gather(
        _transition(client, ticket, "resolved", resolution="已处理"),
        _transition(supervisor, ticket, "cancelled", note="重复"),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    final = (await client.get(f"/api/tickets/{ticket['id']}")).json()
    assert len([e for e in final["events"] if e["kind"] == "status_changed"]) == 2


async def test_concurrent_creates_get_unique_consecutive_numbers(team):
    _, client = team["agent"]
    results = await asyncio.gather(*[_create(client, title=f"工单 {i}") for i in range(20)])
    assert sorted(t["number"] for t in results) == list(range(1, 21))


async def test_numbers_are_per_tenant(seed, login):
    alpha_client, beta_client = await login(await seed("alpha")), await login(await seed("beta"))
    assert (await _create(alpha_client))["number"] == 1
    assert (await _create(alpha_client))["number"] == 2
    assert (await _create(beta_client))["number"] == 1


# --- 同一运单同类异常只能有一张未关闭的工单


async def test_duplicate_active_ticket_is_rejected(team):
    _, client = team["supervisor"]
    ticket = await _create(client, tracking_number="SF100")

    duplicate = await client.post(
        "/api/tickets", json={"type": "damaged", "title": "重复", "tracking_number": "SF100"}
    )
    assert duplicate.status_code == 409
    # 不同类型、没有运单号的不受影响
    await _create(client, tracking_number="SF100", type="lost")
    await _create(client)
    await _create(client)
    # 取消后可以重新上报
    await _transition(client, ticket, "cancelled", note="报错了")
    await _create(client, tracking_number="SF100")


async def test_duplicate_race_creates_only_one(team):
    _, client = team["agent"]
    body = {"type": "damaged", "title": "并发上报", "tracking_number": "SF200"}
    responses = await asyncio.gather(*[client.post("/api/tickets", json=body) for _ in range(8)])
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 7


async def test_duplicate_rejection_does_not_consume_number(team):
    _, client = team["agent"]
    await _create(client, tracking_number="SF300")
    await client.post(
        "/api/tickets", json={"type": "damaged", "title": "重复", "tracking_number": "SF300"}
    )
    assert (await _create(client))["number"] == 2


# --- 审计事件


async def test_events_are_append_only_for_app_role(team, app_engine):
    member, client = team["agent"]
    await _create(client)
    for sql in ("UPDATE ticket_events SET body = 'x'", "DELETE FROM ticket_events"):
        async with app_engine.connect() as connection:
            await set_tenant(connection, member.tenant_id)
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(sql))


async def test_new_tables_are_isolated(seed, login, app_engine, owner_engine):
    alpha, beta = await seed("alpha"), await seed("beta")
    await _create(await login(alpha))

    async with app_engine.connect() as connection:
        for table in ("ticket_events", "ticket_counters"):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        await set_tenant(connection, beta.tenant_id)
        for table in ("ticket_events", "ticket_counters"):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    async with app_engine.connect() as connection:
        await set_tenant(connection, alpha.tenant_id)
        for table in ("ticket_events", "ticket_counters"):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    async with owner_engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname IN ('tickets', 'ticket_events', 'ticket_counters', 'memberships')"
            )
        )
        assert all(row[1] and row[2] for row in rows)


async def test_other_tenant_cannot_act_on_ticket(seed, login):
    alpha, beta = await seed("alpha"), await seed("beta")
    alpha_client, beta_client = await login(alpha), await login(beta)
    ticket = await _create(alpha_client)
    url = f"/api/tickets/{ticket['id']}"

    assert (await _transition(beta_client, ticket, "cancelled", note="x")).status_code == 404
    assert (await _assign(beta_client, ticket, beta.user_id)).status_code == 404
    comment = await beta_client.post(f"{url}/comments", json={"body": "x"})
    assert comment.status_code == 404
    assert len((await alpha_client.get(url)).json()["events"]) == 1


# --- 列表筛选和分页


async def test_list_filters(team):
    agent, client = team["agent"]
    _, other_client = team["agent2"]
    first = await _create(client, title="外箱破损", tracking_number="SF001")
    await _create(client, title="包裹丢失", type="lost", tracking_number="YT002")
    await _assign(client, first, agent.user_id)

    async def titles(viewer=client, **params):
        response = await viewer.get("/api/tickets", params=params)
        assert response.status_code == 200, response.text
        return [t["title"] for t in response.json()["items"]]

    assert await titles() == ["包裹丢失", "外箱破损"]
    assert await titles(type="lost") == ["包裹丢失"]
    assert await titles(status="closed") == []
    assert await titles(assignee="me") == ["外箱破损"]
    assert await titles(other_client, assignee="me") == []
    assert await titles(assignee="unassigned") == ["包裹丢失"]
    assert await titles(assignee=str(agent.user_id)) == ["外箱破损"]
    assert await titles(q="破损") == ["外箱破损"]
    assert await titles(q="yt00") == ["包裹丢失"]
    # % 和 _ 按普通字符匹配，不当作通配符
    assert await titles(q="%") == []
    assert await titles(q="_") == []


async def test_pagination_is_stable_when_new_tickets_arrive(team):
    _, client = team["agent"]
    for i in range(5):
        await _create(client, title=f"工单 {i}")

    page1 = (await client.get("/api/tickets", params={"limit": 2})).json()
    assert [t["number"] for t in page1["items"]] == [5, 4]
    await _create(client, title="翻页期间新增")

    page2 = (
        await client.get("/api/tickets", params={"limit": 2, "before": page1["next_before"]})
    ).json()
    assert [t["number"] for t in page2["items"]] == [3, 2]
    page3 = (
        await client.get("/api/tickets", params={"limit": 2, "before": page2["next_before"]})
    ).json()
    assert [t["number"] for t in page3["items"]] == [1]
    assert page3["next_before"] is None
