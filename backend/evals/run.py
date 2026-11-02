"""AI 回复草稿的离线评测。用法：

uv run python -m evals.run                 # 只测模板兜底，不需要模型，CI 也能跑
uv run python -m evals.run --mode model    # 调真实模型，需要配置 DESK_LLM_*

用例是自己写的合成数据，覆盖常见状态和提示词注入。通过只说明守住了这些规则，
不代表回复在真实场景里好用。评测直接调用校验器和图，不经过接口和数据库。
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from langchain_openai import ChatOpenAI

from app.ai.facts import ClaimFacts, Facts, template_reply
from app.ai.graph import build_graph, initial_state
from app.ai.validator import validate
from app.config import get_settings

CASES = Path(__file__).with_name("cases.jsonl")


def load_cases() -> list[dict]:
    with CASES.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def build_case_facts(raw: dict) -> Facts:
    claim = None
    if raw.get("claim"):
        claim = ClaimFacts(
            carrier="顺丰",
            status=raw["claim"]["status"],
            insured=raw["claim"].get("insured", False),
            goods_quantity=1,
            claim_amount=raw["claim"].get("claim_amount"),
            approved_amount=raw["claim"].get("approved_amount"),
            paid_amount=raw["claim"].get("paid_amount"),
            rejection=raw["claim"].get("rejection"),
            weight_gap_g=None,
        )
    return Facts(
        number=1,
        type=raw["type"],
        title=raw["title"],
        description=raw.get("description", ""),
        tracking_number=raw.get("tracking_number"),
        missing_required=tuple(raw.get("missing_required", ())),
        missing_recommended=(),
        collected=tuple(raw.get("collected", ())),
        claim=claim,
    )


@dataclass
class Outcome:
    id: str
    draft: str
    source: str
    attempts: int = 0
    seconds: float = 0
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def judge(case: dict, facts: Facts, draft: str) -> list[str]:
    # 再校验一次最终结果，防止图的路径上有漏洞让不合格的内容漏出去
    problems = validate(draft, facts)
    problems += [
        f"出现了不该出现的内容：{word}" for word in case.get("forbidden", ()) if word in draft
    ]
    return problems


async def run_case(case: dict, model) -> Outcome:
    facts = build_case_facts(case["facts"])
    if model is None:
        draft = template_reply(facts)
        return Outcome(case["id"], draft, "template", problems=judge(case, facts, draft))

    started = time.perf_counter()
    try:
        state = await build_graph(model, facts).ainvoke(initial_state())
    except Exception as error:  # 记成未通过，让一次失败不影响其余用例
        return Outcome(case["id"], "", "error", problems=[type(error).__name__])
    source = "template" if state["used_fallback"] else "model"
    draft = template_reply(facts) if state["used_fallback"] else state["draft"]
    return Outcome(
        case["id"],
        draft,
        source,
        state["attempts"],
        time.perf_counter() - started,
        judge(case, facts, draft),
    )


def report(outcomes: list[Outcome]) -> str:
    total = len(outcomes)
    lines = [
        f"用例 {total}，通过 {sum(o.passed for o in outcomes)}",
        f"模型一次通过 {sum(o.source == 'model' and o.attempts == 1 for o in outcomes)}",
        f"模型重写后通过 {sum(o.source == 'model' and o.attempts > 1 for o in outcomes)}",
        f"退回模板 {sum(o.source == 'template' for o in outcomes)}",
    ]
    timed = sorted(o.seconds for o in outcomes if o.seconds)
    if timed:
        lines.append(f"耗时中位数 {timed[len(timed) // 2]:.1f} 秒")
    for outcome in outcomes:
        if not outcome.passed:
            lines.append(f"未通过 {outcome.id}：{'；'.join(outcome.problems)}")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["template", "model"], default="template")
    parser.add_argument("--show", action="store_true", help="打印每条草稿，人工看语气")
    args = parser.parse_args()

    model = None
    if args.mode == "model":
        settings = get_settings()
        if not (settings.llm_api_key and settings.llm_model):
            raise SystemExit("请先配置 DESK_LLM_API_KEY 和 DESK_LLM_MODEL")
        model = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout_seconds,
            temperature=0.2,
        )

    outcomes = [await run_case(case, model) for case in load_cases()]
    if args.show:
        for outcome in outcomes:
            print(f"[{outcome.id}] ({outcome.source}) {outcome.draft}\n")
    print(report(outcomes))
    return 0 if all(outcome.passed for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
