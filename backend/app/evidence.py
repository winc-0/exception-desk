"""证据类别和每种异常的证据清单。

清单是根据公开资料整理的初版，还没有经过真实用户确认，不同承运商的要求也可能不同。
规则都集中在这里，调整时不用改接口和前端。
"""

CATEGORY_LABELS: dict[str, str] = {
    "label_photo": "面单照片",
    "package_photo": "外包装照片",
    "damage_photo": "内件破损照片",
    "packing_video": "打包或发货视频",
    "weight_record": "称重记录",
    "tracking_screenshot": "物流轨迹截图",
    "value_proof": "货值凭证",
    "customer_proof": "买家开箱证据",
    "other": "其他",
}

# (必备, 建议)。提交索赔前必备项要齐全。
_CHECKLISTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "damaged": (
        frozenset({"label_photo", "package_photo", "damage_photo"}),
        frozenset({"packing_video", "value_proof", "customer_proof", "weight_record"}),
    ),
    "lost": (
        frozenset({"tracking_screenshot", "value_proof"}),
        frozenset({"label_photo", "packing_video", "weight_record"}),
    ),
    "delayed": (
        frozenset({"tracking_screenshot"}),
        frozenset({"value_proof", "customer_proof"}),
    ),
    "address_change": (frozenset(), frozenset()),
    "other": (frozenset(), frozenset()),
}

# 改址是操作请求，不涉及赔偿
CLAIMABLE_TYPES = frozenset({"damaged", "lost", "delayed", "other"})


def checklist(ticket_type: str, counts: dict[str, int]) -> list[dict]:
    required, recommended = _CHECKLISTS[ticket_type]
    return [
        {
            "category": category,
            "label": label,
            "level": (
                "required"
                if category in required
                else "recommended"
                if category in recommended
                else "optional"
            ),
            "count": counts.get(category, 0),
        }
        for category, label in CATEGORY_LABELS.items()
    ]


def missing_required(ticket_type: str, counts: dict[str, int]) -> list[str]:
    required, _ = _CHECKLISTS[ticket_type]
    return [
        label
        for category, label in CATEGORY_LABELS.items()
        if category in required and counts.get(category, 0) == 0
    ]
