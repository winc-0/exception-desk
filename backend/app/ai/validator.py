"""模型输出的校验。这些是代码里的硬规则，不靠提示词请求模型遵守。

草稿虽然要人确认后才发出，但客服忙的时候容易直接复制，所以错误内容要在这里拦下来。
"""

import re

from app.ai.facts import Facts

MAX_LENGTH = 300

# 能不能赔、赔多少由承运商决定，客服不能替它承诺
_PROMISES = (
    "一定赔",
    "一定会赔",
    "肯定赔",
    "肯定能赔",
    "保证赔",
    "保证能赔",
    "全额赔",
    "全额退",
    "足额赔",
    "必赔",
    "包赔",
    "马上到账",
    "立即到账",
)
_INTERNAL = ("SHA-256", "RLS", "工单", "异常类型", "提示词", "system prompt", "拒赔率")

# 第一次用真实模型评测时，它会编造承运商规则和通知渠道，这些都是工单里没有的信息
_INVENTED_RULES = (
    "根据理赔规则",
    "根据承运商规则",
    "根据承运商要求",
    "根据承运商理赔要求",
    "理赔规则",
    "缺一不可",
    "短信",
    "站内信",
    "本渠道",
    "经核实",
    "经核查",
)
# 模型爱加的材料。只有工单清单里出现过才能提
_COMMON_INVENTED_MATERIALS = (
    "开箱视频",
    "装箱视频",
    "购买凭证",
    "订单截图",
    "底单",
    "书面确认",
    "签字",
    "包装说明",
    "保价凭证",
    "身份证",
    "银行卡",
)
_CLAIM_FILED = re.compile(
    r"已提交|已发起|已(?:向|经|同步至?|为您向)?.{0,6}(?:承运商|快递公司).{0,6}(?:提交|发起)"
    r"|进入.{0,8}(?:索赔|理赔)流程|发起.{0,4}(?:索赔|理赔)"
)
_NOT_FILED_STATUSES = (None, "准备中", "已撤回")
_DECIDED_STATUSES = ("已核定", "已到账")

_AMOUNT = re.compile(r"(?:¥|￥)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:元|块)")
_LONG_CODE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{8,}(?![A-Za-z0-9])")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{15,19}[\dXx]?(?!\d)")


def _same_amount(found: str, known: set[str]) -> bool:
    value = found.replace(",", "")
    try:
        return any(float(value) == float(item) for item in known)
    except ValueError:
        return False


def validate(draft: str, facts: Facts) -> list[str]:
    """返回不合格的原因，空列表表示通过。原因会反馈给模型重写，所以写成可执行的要求。"""
    text = draft.strip()
    problems = []
    if not text:
        return ["回复不能为空"]
    if len(text) > MAX_LENGTH:
        problems.append(f"回复太长，请控制在 {MAX_LENGTH} 字以内")

    for phrase in _PROMISES:
        if phrase in text:
            problems.append(f"不能承诺赔付结果，请去掉“{phrase}”")
    lowered = text.lower()
    for phrase in _INTERNAL:
        if phrase.lower() in lowered:
            problems.append(f"不能出现内部信息“{phrase}”")

    if "**" in text or "##" in text:
        problems.append("这是发给客户的消息，不要用 Markdown 符号，请写纯文本")
    for phrase in _INVENTED_RULES:
        if phrase in text:
            problems.append(f"工单事实里没有这个信息，请去掉“{phrase}”")

    listed = "".join((*facts.missing_required, *facts.missing_recommended, *facts.collected))
    for material in _COMMON_INVENTED_MATERIALS:
        if material in text and material not in listed:
            problems.append(f"“{material}”不在证据清单里，不能要求客户提供")

    if not facts.missing_required:
        asking = re.sub(r"(?:无需|不用|不需要)(?:您|再|您再)?(?:补充|提供)", "", text)
        if re.search(r"补充|请(?:您)?(?:尽快)?提供|烦请|欢迎.{0,6}提供|聊天记录|凭证", asking):
            problems.append("现在没有缺少必备材料，不要向客户索要材料")

    if not facts.collected and "材料已" in text:
        problems.append("还没有收到任何材料，不能说材料已收到或已齐全")

    status = facts.claim.status if facts.claim else None
    if status is None and ("索赔" in text or "理赔" in text):
        problems.append("这张单没有索赔，回复里不要提索赔或理赔")
    if status in _NOT_FILED_STATUSES:
        carrier = re.escape(facts.claim.carrier) if facts.claim else ""
        filed = _CLAIM_FILED.search(text)
        if not filed and carrier:
            filed = re.search(rf"已.{{0,6}}{carrier}.{{0,6}}(?:提交|发起)", text)
        if filed:
            problems.append(f"索赔还没有提交给承运商，不能说“{filed.group()}”")
    if status in _DECIDED_STATUSES and "是否赔付" in text:
        problems.append(f"索赔状态是{status}，不要再说是否赔付待定")

    known_amounts = facts.amounts()
    for match in _AMOUNT.finditer(text):
        found = match.group(1) or match.group(2)
        if not _same_amount(found, known_amounts):
            problems.append(f"金额 {found} 不在工单事实里，不能编造")

    for match in _LONG_CODE.finditer(text):
        if match.group() != facts.tracking_number:
            problems.append(f"单号 {match.group()} 不在工单事实里，不能编造")

    if _PHONE.search(text) or _LONG_NUMBER.search(text):
        problems.append("不能出现手机号、证件号或银行卡号")

    missing = [label for label in facts.missing_required if label not in text]
    if missing:
        problems.append(f"请原样写出缺少的必备材料：{'、'.join(missing)}")
    return problems
