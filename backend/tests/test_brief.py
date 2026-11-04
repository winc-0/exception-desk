import asyncio
import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import text

from app.ai.brief import (
    Brief,
    BriefInput,
    BriefItem,
    accept_for,
    generate_brief,
    parse_brief,
    retrieve,
    rule_brief,
    validate_brief,
)
from app.ai.kb import (
    Bm25,
    Hit,
    Retriever,
    load_all_docs,
    load_index,
    load_taxonomy,
    match_carrier_terms,
    searchable_docs,
    tokenize,
)
from evals.run_brief import case_input, eval_docs, gold, predicted, score
from tests.conftest import set_tenant
from tests.test_ai import ScriptedModel

PNG = b"\x89PNG\r\n\x1a\n"


def inputs(**overrides) -> BriefInput:
    base = BriefInput(
        type="破损",
        title="外箱完好但内件碎裂",
        description="",
        carrier="顺丰",
        insured=False,
        status="被拒赔",
        rejection_code="packaging_intact",
        rejection="外包装完好",
        collected_evidence=frozenset({"E04"}),
        weight_gap_g=None,
    )
    return replace(base, **overrides)


@pytest.fixture
def retriever():
    return Retriever(vectors={})


def test_corpus_labels_are_all_valid():
    taxonomy = load_taxonomy()
    for doc in load_all_docs():
        assert doc.text and doc.source_url.startswith("http"), doc.id
        for code in doc.reasons:
            assert code in taxonomy["reasons"], (doc.id, code)
        for code in doc.counters:
            assert code in taxonomy["counters"], (doc.id, code)
    ids = [doc.id for doc in load_all_docs()]
    assert len(ids) == len(set(ids))


def test_eval_cases_never_enter_search():
    held_out = {doc.id for doc in eval_docs()}
    assert len(held_out) == 10
    assert not held_out & {doc.id for doc in searchable_docs()}
    assert not held_out & set(load_index())


def test_index_is_up_to_date():
    """语料改了要重新生成向量，否则这些文档只能走关键词。"""
    assert set(load_index()) == {doc.id for doc in searchable_docs()}


def test_tokenize_handles_chinese_and_numbers():
    tokens = tokenize("7倍运费 GB/T")
    assert "运费" in tokens
    assert "7" in tokens
    assert "gb" in tokens


def test_keyword_search_finds_exact_terms():
    ranked = Bm25(searchable_docs()).rank("民法典第496条 格式条款 提示说明义务")
    assert ranked[0].id == "L04"


def test_vector_ranking_uses_similarity():
    docs = searchable_docs()[:3]
    vectors = {docs[0].id: [1.0, 0.0], docs[1].id: [0.0, 1.0], docs[2].id: [0.7, 0.7]}
    ranked = Retriever(docs, vectors).rank_vector([0.0, 1.0])
    assert [doc.id for doc in ranked] == [docs[1].id, docs[2].id, docs[0].id]


def test_hybrid_rewards_docs_found_by_both_routes():
    docs = searchable_docs()
    target = next(doc for doc in docs if doc.id == "L04")
    other = next(doc for doc in docs if doc.id == "L01")
    vectors = {target.id: [1.0, 0.0], other.id: [0.0, 1.0]}
    hits = Retriever(docs, vectors).search("格式条款 提示说明义务", query_vector=[1.0, 0.0])
    assert hits[0].doc.id == "L04"
    assert set(hits[0].via) == {"keyword", "vector"}


def test_other_carriers_terms_are_excluded(retriever):
    hits = retrieve(retriever, inputs(carrier="顺丰速运"), None)
    carriers = {h.doc.carrier for h in hits if h.doc.doc_type == "承运商条款"}
    assert carriers == {"顺丰"}


def test_carrier_terms_are_always_included(retriever):
    hits = retrieve(retriever, inputs(carrier="韵达", title="无关的内容"), None, k=3)
    assert "T-YD" in {hit.doc.id for hit in hits}


def test_unknown_carrier_gets_no_carrier_terms(retriever):
    hits = retrieve(retriever, inputs(carrier=None), None)
    assert not [hit for hit in hits if hit.doc.doc_type == "承运商条款"]
    assert accept_for(None)(match_carrier_terms("顺丰")) is False


def _valid(code="R03", why="价值证明不足时承运商常这样主张", cites=("T-SF",)):
    return BriefItem(code=code, why=why, cites=list(cites))


@pytest.fixture
def hits():
    docs = {doc.id: doc for doc in searchable_docs()}
    return [Hit(docs[i], 1.0, ("keyword",)) for i in ("T-SF", "L04", "C01")]


def test_validate_accepts_grounded_brief(hits):
    brief = Brief(
        likely_reasons=[_valid()],
        evidence_gaps=[_valid("E06", "可以证明签收时就有异常")],
        counter_points=[_valid("K01", "可以主张未尽提示义务", ("L04",))],
    )
    assert validate_brief(brief, inputs(), hits) == []


@pytest.mark.parametrize(
    ("brief", "facts", "expected"),
    [
        (Brief(likely_reasons=[_valid("R99")]), {}, "不是"),
        (Brief(likely_reasons=[_valid("K01")]), {}, "不是"),
        (Brief(likely_reasons=[_valid(cites=())]), {}, "没有引用"),
        (Brief(likely_reasons=[_valid(cites=("C99",))]), {}, "不存在"),
        (Brief(likely_reasons=[_valid(why="这种情况一律全额赔")]), {}, "绝对结论"),
        (Brief(likely_reasons=[_valid(why="可以赔 99999 元")]), {}, "不在工单"),
        (Brief(likely_reasons=[_valid("R01")]), {"insured": True}, "已保价"),
        (Brief(likely_reasons=[_valid("R15")]), {}, "不是延误"),
        (Brief(evidence_gaps=[_valid("E04")]), {}, "已经有了"),
        (Brief(likely_reasons=[_valid(why="这正是 R05 的核心依据")]), {}, "分类代码"),
        (Brief(likely_reasons=[_valid(why="可以用 K01 反驳")]), {}, "分类代码"),
        (Brief(likely_reasons=[_valid(why="如 C99 所示")]), {}, "不存在"),
        (Brief(counter_points=[_valid("K06", "可主张保价费", ("L04",))]), {}, "K06"),
        (Brief(likely_reasons=[_valid() for _ in range(5)]), {}, "最多"),
        (Brief(likely_reasons=[_valid("R03", cites=("L04",))]), {}, "不涉及"),
        (Brief(counter_points=[_valid("K01", "可主张未提示", ("T-SF",))]), {}, "不涉及"),
    ],
)
def test_validate_rejects(brief, facts, expected, hits):
    problems = validate_brief(brief, inputs(**facts), hits)
    assert any(expected in problem for problem in problems), problems


def test_validate_rejects_citation_that_does_not_support_the_claim():
    """试用时发现的：K05 引了一篇辟谣说明，资料确实存在，内容却完全无关。"""
    docs = {doc.id: doc for doc in searchable_docs()}
    hits = [Hit(docs[i], 1.0, ("keyword",)) for i in ("L10", "L06")]
    brief = Brief(counter_points=[_valid("K05", "合理期限内提出仍可获支持", ("L10",))])
    assert validate_brief(brief, inputs(), hits) == ["K05 引用的资料不涉及这一项，请删掉这一项"]
    brief = Brief(counter_points=[_valid("K03", "快递企业不适用三倍上限", ("L10",))])
    assert validate_brief(brief, inputs(), hits) == ["K03 引用的资料不涉及这一项，请改为引用 L06"]


def test_validate_needs_only_one_supporting_citation(hits):
    brief = Brief(likely_reasons=[_valid("R01", cites=("L04", "T-SF"))])
    assert validate_brief(brief, inputs(), hits) == []


def test_validate_accepts_numbers_from_cited_source(hits):
    doc = hits[0].doc
    assert "7倍" in doc.text
    brief = Brief(likely_reasons=[_valid("R01", "条款约定 7倍 运费以内赔偿")])
    assert validate_brief(brief, inputs(), hits) == []


def test_validate_accepts_numbers_from_the_ticket(hits):
    brief = Brief(likely_reasons=[_valid(why="交易价 4600 元，明显高于保价额")])
    assert validate_brief(brief, inputs(description="交易价4600元"), hits) == []


def test_validate_rejects_numbers_from_an_uncited_source(hits):
    other = next(hit.doc for hit in hits if hit.doc.id == "T-SF")
    assert "7倍" in other.text
    brief = Brief(likely_reasons=[_valid("R01", "可以按 7倍 运费赔偿", ("L04",))])
    problems = validate_brief(brief, inputs(), hits)
    assert any("7倍" in problem for problem in problems), problems


def test_docs_mentioned_in_why_count_as_citations(hits):
    item = _valid(why="如 C01 所示，T-SF 也有约定")
    assert item.cites == ["T-SF", "C01"]
    assert validate_brief(Brief(likely_reasons=[item]), inputs(), hits) == []


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ('{"likely_reasons": []}', True),
        ('```json\n{"likely_reasons": []}\n```', True),
        ("不是 JSON", False),
        ('{"likely_reasons": [{"code": "R01"}]}', False),
    ],
)
def test_parse_brief(text, ok):
    assert (parse_brief(text) is not None) is ok


@pytest.mark.parametrize(
    "facts",
    [
        inputs(),
        inputs(insured=True, rejection_code=None, rejection=None),
        inputs(carrier=None, insured=None, status=None, rejection_code=None, rejection=None),
        inputs(type="超时", title="超时未送达", carrier="圆通"),
        inputs(collected_evidence=frozenset({"E01", "E02", "E04", "E06", "E07", "E08"})),
    ],
)
def test_rule_brief_always_passes_validation(facts, retriever):
    found = retrieve(retriever, facts, None)
    assert validate_brief(rule_brief(facts, found), facts, found) == []


def test_rule_brief_puts_rejection_reason_first(retriever):
    facts = inputs()
    brief = rule_brief(facts, retrieve(retriever, facts, None))
    assert brief.likely_reasons[0].code == "R05"


def test_rule_brief_respects_insured(retriever):
    facts = inputs(insured=True, rejection_code=None, rejection=None)
    codes = {i.code for i in rule_brief(facts, retrieve(retriever, facts, None)).likely_reasons}
    assert not codes & {"R01", "R02"}


GOOD = json.dumps(
    {
        "likely_reasons": [{"code": "R03", "why": "价值证明不足时常这样主张", "cites": ["T-SF"]}],
        "evidence_gaps": [],
        "counter_points": [{"code": "K01", "why": "可主张未提示", "cites": ["L04"]}],
    },
    ensure_ascii=False,
)


async def test_model_brief_retries_then_passes(hits):
    bad = json.dumps({"likely_reasons": [{"code": "R03", "why": "一律赔", "cites": ["T-SF"]}]})
    model = ScriptedModel(script=[bad, GOOD])
    brief, source, _, attempts = await generate_brief(model, inputs(), hits)
    assert source == "model"
    assert attempts == 2
    assert brief.likely_reasons[0].code == "R03"
    assert "绝对结论" in model.seen[-1][-1].content


async def test_model_brief_falls_back_to_rules(hits):
    model = ScriptedModel(script=["不是 JSON", "还不是 JSON"])
    brief, source, reason, _ = await generate_brief(model, inputs(), hits)
    assert source == "rules"
    assert "JSON" in reason
    assert validate_brief(brief, inputs(), hits) == []


async def test_model_timeout_falls_back_to_rules(hits, monkeypatch):
    monkeypatch.setattr("app.ai.brief.TOTAL_TIMEOUT_SECONDS", 0.01)

    class Slow(ScriptedModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            await asyncio.sleep(1)
            return self._generate(messages, stop, run_manager, **kwargs)

    _, source, reason, _ = await generate_brief(Slow(script=[GOOD]), inputs(), hits)
    assert source == "rules"
    assert reason == "模型响应超时"


async def test_model_error_falls_back_to_rules(hits):
    model = ScriptedModel(script=[RuntimeError("boom")])
    _, source, reason, _ = await generate_brief(model, inputs(), hits)
    assert source == "rules"
    assert reason == "模型调用失败：RuntimeError"


async def test_prompt_contains_sources_but_not_held_out_cases(hits):
    model = ScriptedModel(script=[GOOD])
    await generate_brief(model, inputs(), hits)
    prompt = model.seen[-1][1].content
    assert "[T-SF]" in prompt
    assert "[C05]" not in prompt
    assert "（可引用 T-SF）" in prompt
    assert "R15 " not in prompt
    assert "K05 " not in prompt
    assert "E01 " in prompt


def test_eval_input_does_not_leak_the_answer():
    for doc in eval_docs("all"):
        facts = case_input(doc)
        text = facts.describe() + facts.query()
        assert doc.case.get("holding", "\0") not in text
        assert doc.text not in text
        assert "法院" not in facts.description


def test_replay_cases_are_scorable_and_separate_from_held_out():
    replay = eval_docs("replay")
    assert len(replay) == 19
    assert all(gold(doc)["拒赔理由"] for doc in replay)
    assert not {doc.id for doc in replay} & {doc.id for doc in eval_docs()}
    assert all(case_input(doc).type in ("破损", "丢件", "超时") for doc in replay)


def test_replay_never_retrieves_the_case_itself(retriever):
    for doc in eval_docs("replay"):
        facts = case_input(doc)
        assert doc.id in {hit.doc.id for hit in retrieve(retriever, facts, None, 51)}
        kept = retrieve(retriever, facts, None, 51, exclude=doc.id)
        assert doc.id not in {hit.doc.id for hit in kept}


def test_score_computes_precision_and_recall():
    rows = [
        (
            {"拒赔理由": {"R01", "R02"}, "缺失证据": set(), "反驳": {"K01"}},
            {"拒赔理由": {"R01"}, "缺失证据": {"E01"}, "反驳": {"K01"}},
        )
    ]
    result = score(rows)
    assert result["拒赔理由"] == (0.5, 1.0, 1)
    assert result["缺失证据"] == (0.0, 0.0, 1)
    assert result["反驳"] == (1.0, 1.0, 1)


def test_gold_comes_from_case_labels():
    doc = next(d for d in eval_docs() if d.id == "C05")
    assert gold(doc)["拒赔理由"] == {"R04", "R12"}
    assert predicted(Brief())["反驳"] == set()


async def _ticket(client, **fields):
    response = await client.post(
        "/api/tickets",
        json={"type": "damaged", "title": "外箱完好内件碎裂", "tracking_number": "SF1", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def member(seed):
    return await seed("alpha")


@pytest.fixture
async def client(member, login):
    return await login(member)


async def test_api_returns_rule_brief_with_sources_and_terms(client):
    ticket = await _ticket(client)
    await client.post(
        f"/api/tickets/{ticket['id']}/claim", json={"carrier": "韵达", "claim_amount_cents": 100}
    )
    response = await client.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "rules"
    assert body["sources"]
    assert body["carrier_terms"]["title"].startswith("韵达")
    assert any(term["label"] == "索赔时效" for term in body["carrier_terms"]["terms"])
    source_ids = {source["id"] for source in body["sources"]}
    for group in ("likely_reasons", "evidence_gaps", "counter_points"):
        for item in body[group]:
            assert item["name"]
            assert set(item["cites"]) <= source_ids
    detail = (await client.get(f"/api/tickets/{ticket['id']}")).json()
    assert detail["events"][-1]["kind"] == "ai_drafted"
    assert detail["events"][-1]["data"]["kind"] == "brief"


def _with_usage(content: str) -> AIMessage:
    return AIMessage(
        content,
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        response_metadata={"model_name": "test-model"},
    )


@pytest.mark.parametrize(
    ("script", "source"),
    [
        (["不是 JSON", '{"likely_reasons": []}'], "model"),
        (["不是 JSON", "还不是 JSON"], "rules"),
    ],
)
async def test_api_records_token_usage(app, client, member, app_engine, script, source):
    """退回规则版时，模型已经花掉的用量也要记下来。"""
    app.state.chat_model = ScriptedModel(script=[_with_usage(step) for step in script])
    ticket = await _ticket(client)
    body = (await client.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")).json()
    assert body["source"] == source
    async with app_engine.connect() as connection:
        await set_tenant(connection, member.tenant_id)
        query = text("SELECT input_tokens, output_tokens FROM ticket_ai_drafts")
        assert (await connection.execute(query)).one() == (200, 40)


async def test_api_uses_collected_evidence(client):
    ticket = await _ticket(client)
    await client.post(
        f"/api/tickets/{ticket['id']}/attachments",
        params={"category": "weight_record", "filename": "w.png"},
        content=PNG,
    )
    body = (await client.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")).json()
    assert "E01" not in {item["code"] for item in body["evidence_gaps"]}


async def test_api_rejects_address_change_and_other_tenant(client, seed, login):
    change = await _ticket(client, type="address_change")
    assert (await client.post(f"/api/tickets/{change['id']}/ai/claim-brief")).status_code == 422
    other = await login(await seed("beta"))
    ticket = await _ticket(client)
    assert (await other.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")).status_code == 404


async def test_api_warehouse_cannot_use_brief(client, member, login):
    from tests.test_ai import _member

    warehouse = await login(await _member(client, member, "warehouse"))
    ticket = await _ticket(client)
    response = await warehouse.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")
    assert response.status_code == 403
    assert (await client.get(f"/api/tickets/{ticket['id']}")).status_code == 200


async def test_brief_counts_toward_hourly_limit(app, client, monkeypatch):
    monkeypatch.setattr(app.state.settings, "ai_hourly_limit", 1)
    ticket = await _ticket(client)
    assert (await client.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")).status_code == 200
    assert (await client.post(f"/api/tickets/{ticket['id']}/ai/claim-brief")).status_code == 429
