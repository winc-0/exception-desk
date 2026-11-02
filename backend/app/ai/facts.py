"""AI 起草回复时能看到的事实。

事实由代码取出，模型只负责组织语言。这里有意只放起草需要的最少内容：
不含评论、文件名、员工姓名和附件内容，既减少个人信息外发，也缩小提示词注入的入口。
"""

import re
from dataclasses import asdict, dataclass

from app.claim_flow import REJECTION_LABELS
from app.schemas import TicketDetail

_TYPE_LABELS = {
    "damaged": "破损",
    "lost": "丢件",
    "delayed": "超时",
    "address_change": "改址",
    "other": "其他",
}
_CLAIM_LABELS = {
    "preparing": "准备中",
    "submitted": "已提交",
    "approved": "已核定",
    "rejected": "被拒赔",
    "paid": "已到账",
    "withdrawn": "已撤回",
}

_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


_CLAIM_PROGRESS = {
    None: "没有发起索赔，回复里不要提索赔",
    "准备中": "正在准备材料，还没有提交给承运商",
    "已提交": "已提交给承运商，正在审核，结果未定",
    "已核定": "承运商已核定金额，还没有到账",
    "被拒赔": "承运商这次没有通过，我们正在核对，结果未定。不要自己建议客户补什么材料",
    "已到账": "赔付款已到账",
    "已撤回": "索赔已撤回，目前没有在进行的索赔",
}


def redact(text: str) -> str:
    """工单描述由人填写，发给模型前去掉最常见的个人信息。"""
    text = _ID_CARD.sub("[证件号已隐藏]", text)
    text = _BANK_CARD.sub("[卡号已隐藏]", text)
    return _PHONE.sub("[手机号已隐藏]", text)


def yuan(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}元"


@dataclass(frozen=True)
class ClaimFacts:
    carrier: str
    status: str
    insured: bool
    goods_quantity: int | None
    claim_amount: str | None
    approved_amount: str | None
    paid_amount: str | None
    rejection: str | None
    weight_gap_g: int | None


@dataclass(frozen=True)
class Facts:
    number: int
    type: str
    title: str
    description: str
    tracking_number: str | None
    missing_required: tuple[str, ...]
    missing_recommended: tuple[str, ...]
    collected: tuple[str, ...]
    claim: ClaimFacts | None

    def amounts(self) -> set[str]:
        if self.claim is None:
            return set()
        values = (self.claim.claim_amount, self.claim.approved_amount, self.claim.paid_amount)
        return {value.removesuffix("元") for value in values if value}

    def ticket_payload(self) -> dict:
        return {
            "工单号": self.number,
            "异常类型": self.type,
            "运单号": self.tracking_number,
            # 标题和描述是人填写的内容，只能当作资料，不能当作指令
            "工单标题（资料）": self.title,
            "工单描述（资料）": self.description,
        }

    def evidence_payload(self) -> dict:
        return {
            "缺少的必备材料": list(self.missing_required),
            "缺少的建议材料": list(self.missing_recommended),
            "已收集的材料": list(self.collected),
        }

    def claim_payload(self) -> dict:
        status = self.claim.status if self.claim else None
        return {
            "索赔": asdict(self.claim) if self.claim else None,
            "索赔进度说明": _CLAIM_PROGRESS[status],
        }


def build_facts(ticket: TicketDetail) -> Facts:
    claim = None
    if ticket.claim is not None:
        record = ticket.claim
        gap = None
        if record.pickup_weight_g and record.billed_weight_g:
            gap = record.pickup_weight_g - record.billed_weight_g
        rejection = None
        if record.status == "rejected":
            label = REJECTION_LABELS.get(record.rejection_code or "", "")
            rejection = "：".join(part for part in (label, record.rejection_reason) if part)
        claim = ClaimFacts(
            carrier=record.carrier,
            status=_CLAIM_LABELS[record.status],
            insured=record.insured,
            goods_quantity=record.goods_quantity,
            claim_amount=yuan(record.claim_amount_cents) if record.claim_amount_cents else None,
            approved_amount=yuan(record.approved_cents) if record.approved_cents else None,
            paid_amount=yuan(record.paid_cents) if record.paid_cents else None,
            rejection=redact(rejection) if rejection else None,
            weight_gap_g=gap,
        )

    checklist = ticket.evidence_checklist
    return Facts(
        number=ticket.number,
        type=_TYPE_LABELS[ticket.type],
        title=redact(ticket.title),
        description=redact(ticket.description)[:1000],
        tracking_number=ticket.tracking_number,
        missing_required=tuple(i.label for i in checklist if i.level == "required" and not i.count),
        missing_recommended=tuple(
            i.label for i in checklist if i.level == "recommended" and not i.count
        ),
        collected=tuple(i.label for i in checklist if i.count),
        claim=claim,
    )


def risks(facts: Facts) -> list[str]:
    """给客服看的提醒，全部由规则算出，不经过模型。"""
    items = []
    claim = facts.claim
    if facts.missing_required:
        items.append(f"缺必备材料：{'、'.join(facts.missing_required)}，常见拒赔理由是材料不足。")
    if claim is None:
        return items
    if not claim.insured:
        items.append("未保价：承运商可能按运费倍数或封顶金额赔付，不要向客户承诺全额。")
    if claim.goods_quantity is None:
        items.append("还没填货物数量。")
    if claim.weight_gap_g and claim.weight_gap_g > 0:
        items.append(f"揽收重量比计费重量多 {claim.weight_gap_g} 克，可能有短少，建议核对。")
    if claim.rejection:
        items.append(f"已被拒赔（{claim.rejection}），再次提交前要针对原因补材料。")
    return items


def template_reply(facts: Facts) -> str:
    """没有配置模型或模型结果不合格时使用。只用事实拼接，永远不会编造内容。"""
    subject = f"运单 {facts.tracking_number} " if facts.tracking_number else "您反馈"
    parts = [f"您好，关于{subject}的{facts.type}问题，我们正在跟进。"]
    claim = facts.claim
    if facts.missing_required:
        parts.append(f"为了继续处理，请您提供：{'、'.join(facts.missing_required)}。")
    elif claim is not None and claim.status == "被拒赔":
        parts.append("承运商暂未通过这次理赔，我们正在核对，需要您配合时会再联系您。")
    elif claim is not None and claim.status == "已提交":
        parts.append("已提交给承运商审核，有结果会第一时间告诉您。")
    elif claim is not None and claim.status == "已核定":
        parts.append(f"承运商已核定金额 {claim.approved_amount}，到账后会通知您。")
    elif claim is not None and claim.status == "已到账":
        parts.append(f"理赔款 {claim.paid_amount} 已到账，请您留意查收。")
    elif facts.collected:
        parts.append("目前所需材料已收集齐全，有进展会第一时间告诉您。")
    else:
        # 这类异常没有必备材料，并不等于收齐了，不能这样告诉客户
        parts.append("有进展会第一时间告诉您。")
    parts.append("给您带来不便，非常抱歉。")
    return "".join(parts)
