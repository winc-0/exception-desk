import asyncio

import pytest

from app.csv_import import MAX_BYTES, MAX_ROWS
from tests.conftest import Member

HEADER = "类型,标题,运单号,描述\n"


async def _import(client, content: bytes | str):
    if isinstance(content, str):
        content = content.encode("utf-8")
    return await client.post(
        "/api/tickets/import", content=content, headers={"Content-Type": "text/csv"}
    )


@pytest.fixture
async def client(seed, login):
    return await login(await seed("alpha"))


async def _all(client):
    return (await client.get("/api/tickets")).json()["items"]


async def test_import_creates_tickets_with_audit_events(client):
    response = await _import(client, HEADER + "破损,外箱压损,SF001,客户拒收\n丢件,包裹丢失,,\n")
    assert response.status_code == 200, response.text
    assert response.json() == {"created": 2, "skipped": []}

    tickets = await _all(client)
    assert [(t["number"], t["type"], t["tracking_number"]) for t in tickets] == [
        (2, "lost", None),
        (1, "damaged", "SF001"),
    ]
    detail = (await client.get(f"/api/tickets/{tickets[1]['id']}")).json()
    assert detail["description"] == "客户拒收"
    assert detail["events"][0]["kind"] == "created"
    assert detail["events"][0]["data"] == {"source": "csv"}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk"])
async def test_import_supports_common_excel_encodings(client, encoding):
    response = await _import(client, (HEADER + "超时,晚点三天,SF9,\n").encode(encoding))
    assert response.status_code == 200, response.text
    assert (await _all(client))[0]["title"] == "晚点三天"


async def test_import_handles_quotes_blank_lines_and_extra_columns(client):
    content = '备注,类型,标题,运单号\n无关内容,破损,"标题,带逗号",SF1\n\n,,,\n'
    response = await _import(client, content)
    assert response.json()["created"] == 1
    assert (await _all(client))[0]["title"] == "标题,带逗号"


async def test_invalid_row_imports_nothing_and_reports_lines(client):
    response = await _import(client, HEADER + "破损,正常行,SF1,\n火灾,类型不对,SF2,\n丢件,,SF3,\n")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [e["line"] for e in detail["errors"]] == [3, 4]
    assert await _all(client) == []
    # 失败的导入不占用工单号
    created = await client.post("/api/tickets", json={"type": "other", "title": "x"})
    assert created.json()["number"] == 1


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "空"),
        (HEADER, "没有数据行"),
        ("标题,运单号\n一个标题,SF1\n", "缺少必需的列"),
        (b"\xff\xfe\x00\x01\x80\x80\xff", "编码"),
    ],
)
async def test_bad_files_are_rejected_with_a_clear_message(client, content, message):
    response = await _import(client, content)
    assert response.status_code == 422
    assert message in response.json()["detail"]


async def test_size_and_row_limits(client):
    too_big = HEADER.encode() + b"x" * (MAX_BYTES + 1)
    assert (await _import(client, too_big)).status_code == 413

    too_many = HEADER + "破损,标题,,\n" * (MAX_ROWS + 1)
    response = await _import(client, too_many)
    assert response.status_code == 422
    assert str(MAX_ROWS) in response.json()["detail"]

    exactly_max = HEADER + "破损,标题,,\n" * MAX_ROWS
    assert (await _import(client, exactly_max)).json()["created"] == MAX_ROWS


async def test_reimporting_same_file_creates_no_duplicates(client):
    content = HEADER + "破损,外箱压损,SF001,\n丢件,包裹丢失,SF002,\n"
    assert (await _import(client, content)).json()["created"] == 2

    again = (await _import(client, content)).json()
    assert again["created"] == 0
    assert [s["line"] for s in again["skipped"]] == [2, 3]
    assert len(await _all(client)) == 2


async def test_duplicates_inside_one_file_are_skipped(client):
    content = HEADER + "破损,第一次,SF001,\n破损,第二次,SF001,\n丢件,不同类型,SF001,\n"
    result = (await _import(client, content)).json()
    assert result["created"] == 2
    assert [s["line"] for s in result["skipped"]] == [3]


async def test_concurrent_imports_do_not_duplicate(client):
    content = HEADER + "".join(f"破损,工单,SF{i},\n" for i in range(20))
    results = await asyncio.gather(*[_import(client, content) for _ in range(4)])
    assert all(r.status_code == 200 for r in results)
    assert sum(r.json()["created"] for r in results) == 20


async def test_import_permissions_and_isolation(seed, login):
    admin = await seed("alpha")
    admin_client = await login(admin)
    beta_client = await login(await seed("beta"))
    await admin_client.post(
        "/api/members",
        json={
            "email": "w@alpha.example.com",
            "display_name": "仓管",
            "role": "warehouse",
            "password": "correct-horse-battery",
        },
    )
    warehouse = await login(Member(admin.tenant_id, None, "alpha", "w@alpha.example.com"))
    assert (await _import(warehouse, HEADER + "破损,x,,\n")).status_code == 403

    await _import(admin_client, HEADER + "破损,x,SF1,\n")
    # 另一家公司导入同一运单不算重复，也看不到对方的数据
    assert (await _import(beta_client, HEADER + "破损,y,SF1,\n")).json()["created"] == 1
    assert [t["title"] for t in await _all(beta_client)] == ["y"]
