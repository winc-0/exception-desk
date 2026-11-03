"""索赔依据的评测：用没有进检索库的 10 个判例当考题。用法：

uv run python -m evals.run_brief                    # 规则版，不调用模型
uv run python -m evals.run_brief --mode model       # 真实模型，需要配置 DESK_LLM_*

只给案情（发生了什么、是否保价、已有证据），不给裁判要点，让它预测拒赔理由、缺失证据和反驳，
再和判决中实际出现的标签对比。
样本只有 10 个，案情也很简略，分数只能用来比较版本，不能代表真实准确率。
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.ai.brief import BriefInput, accept_for, generate_brief, retrieve
from app.ai.kb import Hit, Retriever, load_all_docs
from app.config import get_settings
from app.main import build_chat_model, build_embeddings

# 人工写的案情，只有事实，没有法院的认定和结果。是看过标准答案后写的，有一定的偏差风险
SCENARIOS = json.loads(Path(__file__).with_name("brief_scenarios.json").read_text("utf-8"))
_TYPES = {"破损": "破损", "损毁": "破损", "丢失": "丢件", "短少": "破损", "延误": "超时"}


def eval_docs():
    return [doc for doc in load_all_docs() if doc.is_eval]


def case_input(doc) -> BriefInput:
    case = doc.case
    loss = case.get("loss_type") or ""
    insured = case.get("insured")
    carrier = doc.carrier if doc.carrier and doc.carrier != "未具名" else None
    return BriefInput(
        type=next((label for word, label in _TYPES.items() if word in loss), "其他"),
        title=case.get("item") or "",
        description=SCENARIOS[doc.id],
        carrier=carrier,
        insured=insured if isinstance(insured, bool) else None,
        status=None,
        rejection_code=None,
        rejection=None,
        collected_evidence=frozenset(case.get("evidence_present") or ()),
        weight_gap_g=None,
    )


def gold(doc) -> dict[str, set[str]]:
    case = doc.case
    return {
        "拒赔理由": set(case.get("carrier_defenses") or ()),
        "缺失证据": set(case.get("evidence_missing_or_weak") or ()),
        "反驳": set(case.get("counter_arguments_accepted") or ()),
    }


def predicted(brief) -> dict[str, set[str]]:
    return {
        "拒赔理由": {item.code for item in brief.likely_reasons},
        "缺失证据": {item.code for item in brief.evidence_gaps},
        "反驳": {item.code for item in brief.counter_points},
    }


def score(rows: list[tuple[dict, dict]]) -> dict[str, tuple[float, float, int]]:
    result = {}
    for label in ("拒赔理由", "缺失证据", "反驳"):
        hit = sum(len(answer[label] & truth[label]) for answer, truth in rows)
        guessed = sum(len(answer[label]) for answer, _ in rows)
        total = sum(len(truth[label]) for _, truth in rows)
        result[label] = (hit / guessed if guessed else 0.0, hit / total if total else 0.0, total)
    return result


async def retrieval_coverage(retriever, vectors_by_doc: dict[str, list[float]], mode: str, k: int):
    """检索本身的效果：判决里实际出现的拒赔理由，有多少出现在取回文档的标签上。"""
    covered = total = 0
    for doc in eval_docs():
        facts = case_input(doc)
        if mode == "keyword":
            hits = retrieve(retriever, facts, None, k)
        elif mode == "vector":
            ranked = retriever.rank_vector(vectors_by_doc[doc.id])
            kept = [item for item in ranked if accept_for(facts.carrier)(item)]
            hits = [Hit(item, 0.0, ("vector",)) for item in kept[:k]]
        else:
            hits = retrieve(retriever, facts, vectors_by_doc[doc.id], k)
        tags = {code for hit in hits for code in hit.doc.reasons}
        truth = gold(doc)["拒赔理由"]
        covered += len(truth & tags)
        total += len(truth)
    return covered / total if total else 0.0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["rules", "model"], default="rules")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    embeddings = build_embeddings(settings)
    retriever = Retriever()
    docs = eval_docs()
    vectors = {}
    if embeddings is not None and retriever.vectors:
        queries = [case_input(doc).query() for doc in docs]
        vectors = dict(
            zip([d.id for d in docs], await embeddings.aembed_documents(queries), strict=True)
        )

    print("检索覆盖率（k=8）：")
    print(f"  关键词 {await retrieval_coverage(retriever, vectors, 'keyword', 8):.0%}")
    if vectors:
        print(f"  向量   {await retrieval_coverage(retriever, vectors, 'vector', 8):.0%}")
        print(f"  混合   {await retrieval_coverage(retriever, vectors, 'hybrid', 8):.0%}")

    model = build_chat_model(settings) if args.mode == "model" else None
    rows = []
    fallbacks = 0
    for doc in docs:
        facts = case_input(doc)
        hits = retrieve(retriever, facts, vectors.get(doc.id))
        brief, source, reason, _ = await generate_brief(model, facts, hits)
        fallbacks += source == "rules" and model is not None
        rows.append((predicted(brief), gold(doc)))
        if args.show:
            print(f"\n[{doc.id}] {doc.title} ({source}) {reason}")
            print("  预测", rows[-1][0])
            print("  答案", rows[-1][1])

    print(f"\n{args.mode}（{len(docs)} 个判例，退回规则 {fallbacks} 次）：")
    for label, (precision, recall, total) in score(rows).items():
        print(f"  {label}：精确率 {precision:.0%}，召回率 {recall:.0%}（标准答案 {total} 项）")


if __name__ == "__main__":
    asyncio.run(main())
