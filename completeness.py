"""Conservative Article 17 completeness pre-check for labour contracts.

This module only answers whether the eight generally required subject areas can
be located in the supplied text.  It deliberately does not decide whether the
located wording is lawful, effective, or adequate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


SOURCE_ID = "LCL17"
_TABLE_ROW = re.compile(r"^(T\d+R\d+)C\d+P\d+$")
_PLACEHOLDER = re.compile(
    r"(?:_{2,}|＿{2,}|…{2,}|\.\s*\.\s*\.|【\s*(?:待填|待填写|空缺)\s*】|"
    r"\[\s*(?:待填|待填写|空缺)\s*\]|<\s*(?:待填|待填写|空缺)\s*>)"
)
_ONLY_PUNCTUATION = re.compile(r"^[\s:：;；,，。()（）【】\[\]_/＿\-.·]*$")


@dataclass(frozen=True)
class _Piece:
    id: str
    text: str


@dataclass(frozen=True)
class _Field:
    name: str
    labels: tuple[re.Pattern[str], ...]
    content: tuple[re.Pattern[str], ...]
    blanks: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class _Item:
    id: str
    title: str
    fields: tuple[_Field, ...]


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.I) for pattern in patterns)


ITEMS = (
    _Item(
        "B01",
        "用人单位名称、住所和法定代表人或主要负责人",
        (
            _Field("用人单位名称", _rx(r"用人单位(?:名称)?", r"甲方(?:名称)?", r"公司名称"), _rx(r"(?:用人单位|甲方|公司)(?:名称)?\s*(?:系|为|[：:])\s*[^。；\n]{2,40}(?:公司|厂|中心|事务所|工作室|单位)")),
            _Field("用人单位住所", _rx(r"用人单位住所", r"单位住所", r"用人单位地址", r"单位地址", r"甲方住所", r"公司(?:住所|地址)", r"注册地址"), _rx(r"(?:住所|地址|注册地址)\s*(?:为|位于|[：:])\s*[^\s：:]{2,}")),
            _Field("法定代表人或主要负责人", _rx(r"法定代表人", r"主要负责人", r"负责人"), _rx(r"(?:法定代表人|主要负责人|负责人)\s*(?:为|是|系|[：:])\s*[\u4e00-\u9fff·]{2,4}(?=[，,。；;\s]|$)")),
        ),
    ),
    _Item(
        "B02",
        "劳动者姓名、住址和居民身份证或其他有效证件号码",
        (
            _Field("劳动者姓名", _rx(r"劳动者姓名", r"乙方(?:姓名)?", r"员工姓名"), _rx(r"(?:劳动者|乙方|员工)(?:姓名)?\s*(?:为|系|[：:])\s*[\u4e00-\u9fff·]{2,4}(?=[，,。；;\s]|$)", r"(?:劳动者|员工)\s*[\u4e00-\u9fff·]{2,4}\s*[,，]\s*(?:现?住址|身份证)")),
            _Field("劳动者住址", _rx(r"劳动者住址", r"乙方住址", r"员工(?:住址|地址)", r"现住址"), _rx(r"(?:住址|现住址)\s*(?:为|位于|[：:])\s*[^\s：:]{2,}")),
            _Field("居民身份证或其他有效证件号码", _rx(r"居民身份证(?:号码|号)?", r"身份证(?:号码|号)", r"有效证件(?:号码|号)", r"证件号码"), _rx(r"\b\d{17}[0-9Xx]\b", r"(?:身份证|证件)(?:号码|号)?\s*[：:]\s*[A-Za-z0-9]{6,}")),
        ),
    ),
    _Item(
        "B03",
        "劳动合同期限",
        (
            _Field(
                "劳动合同期限",
                _rx(r"劳动合同期限", r"合同期限"),
                _rx(r"(?:双方订立|本合同为|合同期限为)[^。；\n]{0,12}无固定期限(?:劳动合同)?", r"(?:固定期限|以完成[^。；\n]{1,30}工作任务为期限)[^。；\n]{0,35}(?:\d{4}年|\d+(?:年|个月|月))", r"(?:合同|劳动合同)?期限\s*(?:为|是|[：:])?\s*(?:\d+(?:年|个月|月)|自\s*\d{4}年)", r"自\s*\d{4}年\d{1,2}月\d{1,2}日\s*起?\s*至\s*\d{4}年\d{1,2}月\d{1,2}日"),
                _rx(r"(?:固定期限[^。；\n]{0,20})?自\s*(?:【\s*】|_{2,}|＿{2,}|\s{2,})\s*年", r"(?:劳动合同|合同)?期限\s*[：:]\s*(?:【\s*】|_{2,}|＿{2,})"),
            ),
        ),
    ),
    _Item(
        "B04",
        "工作内容和工作地点",
        (
            _Field("工作内容", _rx(r"工作内容", r"工作岗位", r"岗位(?:名称)?", r"职务"), _rx(r"(?:从事|担任|岗位为|工作内容为)\s*[^。；\n]{2,40}(?:工作|岗位|职务)?")),
            _Field("工作地点", _rx(r"工作地点", r"工作地", r"办公地点"), _rx(r"(?:工作|办公)地点\s*(?:为|在|位于|[：:])\s*[^。；\n]{2,40}", r"(?:乙方|劳动者|员工)\s*在\s*[^。；\n]{2,30}\s*(?:工作|办公)")),
        ),
    ),
    _Item(
        "B05",
        "工作时间和休息休假",
        (
            _Field("工作时间", _rx(r"工作时间", r"工时制度", r"工作制"), _rx(r"(?:标准|综合计算|不定时)工时(?:制|制度)?", r"(?:每日|每天)工作?[^。；\n]{0,12}(?:小时|时)", r"每周工作[^。；\n]{0,12}(?:天|小时)")),
            _Field("休息休假", _rx(r"休息休假", r"休假", r"休息日", r"年休假"), _rx(r"(?:享有|安排|依法享受)[^。；\n]{0,25}(?:休息|休假|法定节假日|年休假)", r"每周休息[^。；\n]{0,12}(?:天|日)")),
        ),
    ),
    _Item(
        "B06",
        "劳动报酬",
        (
            _Field(
                "劳动报酬",
                _rx(r"劳动报酬", r"工资(?:标准)?", r"薪资", r"薪酬"),
                _rx(r"(?:月工资|工资标准|基本工资|劳动报酬|税前薪酬|薪资)\s*(?:为|是|[：:])?\s*(?:人民币)?\s*\d+(?:\.\d+)?\s*元", r"(?:按月|每月)[^。；\n]{0,20}(?:支付|发放)[^。；\n]{0,12}(?:工资|劳动报酬)", r"(?:工资|劳动报酬)[^。；\n]{0,20}(?:支付|发放)"),
                _rx(r"(?:月工资|工资标准|基本工资|劳动报酬|税前薪酬|薪资)\s*(?:为|是|[：:])?\s*(?:人民币)?\s*(?:【\s*】|_{2,}|＿{2,}|\s{2,})\s*元"),
            ),
        ),
    ),
    _Item(
        "B07",
        "社会保险",
        (
            _Field("社会保险", _rx(r"社会保险", r"社保"), _rx(r"(?:依法|按规定)?[^。；\n]{0,12}(?:缴纳|办理|参加)[^。；\n]{0,12}(?:社会保险|社保)", r"(?:劳动者|乙方|员工)[^。；\n]{0,12}(?:知悉|确认)[^。；\n]{0,15}(?:社会保险|社保)", r"(?:社会保险|社保)[^。；\n]{0,20}(?:缴纳|办理|参加)")),
        ),
    ),
    _Item(
        "B08",
        "劳动保护、劳动条件和职业危害防护",
        (
            _Field("劳动保护", _rx(r"劳动保护", r"防护用品"), _rx(r"(?:提供|配备)[^。；\n]{0,20}(?:劳动保护|防护用品|劳动防护用品)")),
            _Field("劳动条件", _rx(r"劳动条件", r"工作条件"), _rx(r"(?:提供|具备|改善|符合)[^。；\n]{0,20}(?:劳动条件|工作条件)")),
            _Field("职业危害防护", _rx(r"职业危害防护", r"职业病危害", r"职业危害"), _rx(r"(?:职业病?危害|职业危害)[^。；\n]{0,30}(?:防护|告知|措施|无|不存在)", r"(?:防护|告知)[^。；\n]{0,25}(?:职业病?危害|职业危害)")),
        ),
    ),
)


def _pieces(contract: Any) -> list[_Piece]:
    raw = contract.get("pieces", []) if isinstance(contract, dict) else getattr(contract, "pieces", [])
    result: list[_Piece] = []
    for piece in raw or []:
        if isinstance(piece, dict):
            piece_id, text = piece.get("id"), piece.get("text")
        else:
            piece_id, text = getattr(piece, "id", None), getattr(piece, "text", None)
        if isinstance(piece_id, str) and isinstance(text, str):
            result.append(_Piece(piece_id, text))
    return result


def _is_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text)) or bool(_ONLY_PUNCTUATION.fullmatch(text))


def _same_table_row(left: _Piece, right: _Piece) -> bool:
    left_row = _TABLE_ROW.match(left.id)
    right_row = _TABLE_ROW.match(right.id)
    return bool(left_row and right_row and left_row.group(1) == right_row.group(1))


def _ref(piece: _Piece) -> dict[str, str]:
    return {"id": piece.id, "quote": piece.text}


def _dedupe_refs(refs: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref["id"], ref["quote"])
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def _field_state(field: _Field, pieces: list[_Piece]) -> tuple[str, list[dict[str, str]]]:
    """Return ``present``, ``blank`` or ``absent`` with exact-source refs."""

    # A real form blank wins over generic explanatory text elsewhere.  For
    # example, an empty basic-wage amount is not completed by a later overtime
    # formula mentioning “工资”, nor are empty fixed-term dates completed by a
    # paragraph explaining other possible contract types.
    explicit_blank_refs = [
        _ref(piece)
        for piece in pieces
        if any(pattern.search(piece.text) for pattern in field.blanks)
    ]
    if explicit_blank_refs:
        return "blank", _dedupe_refs(explicit_blank_refs)

    blank_refs: list[dict[str, str]] = []
    for index, piece in enumerate(pieces):
        text = piece.text
        content_match = next((pattern.search(text) for pattern in field.content if pattern.search(text)), None)
        if content_match and not _is_placeholder(content_match.group(0)):
            return "present", [_ref(piece)]

        label_match = next((pattern.search(text) for pattern in field.labels if pattern.search(text)), None)
        if not label_match:
            continue

        raw_suffix = text[label_match.end() :]
        # Bare party names are meaningful labels only as a standalone table
        # cell or before a field separator.  In ordinary sentences such as
        # “乙方依法休假” they are merely grammar, not identity information.
        if label_match.group(0) in {"甲方", "乙方"} and raw_suffix and not re.match(r"^\s*[：:=]", raw_suffix):
            continue
        suffix = raw_suffix
        suffix = re.sub(r"^[\s:：=（(]*", "", suffix).strip()
        if suffix and not _is_placeholder(suffix):
            return "present", [_ref(piece)]

        if index + 1 < len(pieces) and _same_table_row(piece, pieces[index + 1]):
            neighbour = pieces[index + 1]
            if neighbour.text.strip() and not _is_placeholder(neighbour.text):
                return "present", [_ref(piece), _ref(neighbour)]
            blank_refs.extend((_ref(piece), _ref(neighbour)))
        else:
            blank_refs.append(_ref(piece))

    return ("blank", _dedupe_refs(blank_refs)) if blank_refs else ("absent", [])


def _is_template(context: dict[str, Any]) -> bool:
    return "模板" in str((context or {}).get("input_kind", ""))


def check_completeness(contract: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Check the eight concrete subject areas listed in LCL Article 17.

    The result is a basic local pre-check for UI display.  A located item is not
    a legal conclusion; ambiguous and partially populated items remain explicit.
    """

    pieces = _pieces(contract)
    template = _is_template(context)
    results: list[dict[str, Any]] = []
    for item in ITEMS:
        states: list[tuple[_Field, str, list[dict[str, str]]]] = [
            (field, *_field_state(field, pieces)) for field in item.fields
        ]
        missing = [field.name for field, state, _ in states if state != "present"]
        refs = _dedupe_refs(ref for _, _, field_refs in states for ref in field_refs)
        present_count = sum(state == "present" for _, state, _ in states)
        has_blank = any(state == "blank" for _, state, _ in states)

        if not missing:
            status = "已找到"
            note = "基础预检已定位相关实质文字；只代表文本出现，不代表条款合法、有效或充分。"
        elif has_blank or present_count:
            if template:
                status = "待填写"
                note = "模板中已出现该项，但仍有空白或字段不全；签署前需补齐。基础预检不据此认定违法。"
            else:
                status = "需确认"
                note = "拟签文本中该项为空、含占位符或仅部分出现，请在签署前人工确认。"
        else:
            status = "未找到"
            note = "基础预检未定位到该项的实质文字；未命中不等于已经认定违法。"

        if item.id == "B07" and status == "已找到":
            note = "已定位社会保险文字；这只说明该项出现，不代表其表述合法、有效或足以免除任何责任。"

        results.append(
            {
                "id": item.id,
                "title": item.title,
                "status": status,
                "refs": refs,
                "note": note,
                "source_ids": [SOURCE_ID],
                "missing_fields": missing,
            }
        )
    return results


__all__ = ["check_completeness"]
