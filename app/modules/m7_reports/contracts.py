"""财政报送契约定义。

契约版本 1 为旧版：收款单位是一个合并字段 ``payee``。
契约版本 2 为新版：收款单位拆成法人 ``legal_entity`` 与分支 ``branch``。

契约随首次报送一起冻结。换版只影响之后的新报送；已存在的旧报送仍按
其原始契约校验与恢复，不会补写新栏，也不会因换版而重新发送。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.errors import DomainError

CENT = Decimal("0.01")

CONTRACTS: dict[int, dict] = {
    1: {
        "version": 1,
        "name": "legacy-merged-payee",
        "label": "旧版·合并收款单位",
        "payee_fields": ["payee"],
        "requires_branch": False,
        "split_payee": False,
    },
    2: {
        "version": 2,
        "name": "split-legal-branch",
        "label": "新版·法人与分支机构",
        "payee_fields": ["legal_entity", "branch"],
        "requires_branch": True,
        "split_payee": True,
    },
}

INITIAL_CONTRACT_VERSION = 1


def get_contract(version) -> dict:
    try:
        key = int(version)
    except (TypeError, ValueError):
        raise DomainError("UNKNOWN_CONTRACT", f"报送契约版本无效：{version!r}")
    definition = CONTRACTS.get(key)
    if definition is None:
        raise DomainError("UNKNOWN_CONTRACT", f"报送契约版本未登记：{version!r}")
    return dict(definition)


def all_contracts() -> list[dict]:
    return [dict(CONTRACTS[key]) for key in sorted(CONTRACTS)]


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value, field: str, line: int) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DomainError("INVALID_VALUE", f"第 {line} 行{field}无效：{value!r}")
    if not number.is_finite() or number < 0:
        raise DomainError("INVALID_VALUE", f"第 {line} 行{field}无效：{value!r}")
    return number


def validate_content(content, version) -> dict:
    """按指定契约校验报送原文。

    返回归一后的分摊输入（金额、权重、上限）。校验只读取契约要求的栏目，
    因此旧版原文在新版契约生效后依旧不会要求、也不会补出分支字段。
    """
    definition = get_contract(version)
    if not isinstance(content, dict):
        raise DomainError("EMPTY_VALUE", "报送内容必须是对象")
    approved = _decimal(content.get("approved"), "批复金额", 0).quantize(CENT)
    payees = content.get("payees")
    if not isinstance(payees, list) or not payees:
        raise DomainError("EMPTY_VALUE", "报送内容缺少收款单位明细")

    normalized = []
    seen: set[str] = set()
    for index, item in enumerate(payees, 1):
        if not isinstance(item, dict):
            raise DomainError("INVALID_VALUE", f"第 {index} 行收款单位格式无效")
        project_id = _text(item.get("project_id"))
        if not project_id:
            raise DomainError("EMPTY_VALUE", f"第 {index} 行缺少项目编号")
        if project_id in seen:
            raise DomainError("DUPLICATE_CONFLICT", f"第 {index} 行项目编号重复：{project_id}")
        seen.add(project_id)
        if definition["split_payee"]:
            if not _text(item.get("legal_entity")):
                raise DomainError("MISSING_LEGAL_ENTITY", f"第 {index} 行缺少法人单位")
            if not _text(item.get("branch")):
                raise DomainError("MISSING_BRANCH", f"第 {index} 行缺少分支机构")
        elif not _text(item.get("payee")):
            raise DomainError("EMPTY_VALUE", f"第 {index} 行缺少收款单位")
        normalized.append({
            "project_id": project_id,
            "weight": _decimal(item.get("weight"), "权重", index),
            "cap": _decimal(item.get("cap"), "上限", index).quantize(CENT),
        })
    return {"approved": approved, "payees": normalized, "contract": definition}
