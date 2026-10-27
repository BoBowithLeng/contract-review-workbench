"""Package-preserving DOCX delivery for confirmed, character-level edits.

This module deliberately implements a small WordprocessingML dialect.  It can
edit ordinary text runs in body paragraphs and table-cell paragraphs, while
leaving every unrelated OPC part byte-for-byte unchanged.  Anything more
complex is rejected rather than flattened into plain text.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import re
import zipfile
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from lxml import etree

from contract_core import parse_docx


PARSER_VERSION = "docx-delivery/1"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"

W = f"{{{W_NS}}}"
PKG_REL = f"{{{PKG_REL_NS}}}"
CT = f"{{{CT_NS}}}"

COMMENTS_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
COMMENTS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
)

_ROOT_KEYS = {
    "schema_version",
    "source_sha256",
    "revision",
    "author",
    "created_at",
    "edits",
    "comments",
}
_EDIT_REQUIRED = {
    "id",
    "piece_id",
    "paragraph_sha256",
    "start",
    "end",
    "old_text",
    "new_text",
    "confirmed",
}
_EDIT_OPTIONAL = {"finding_ids", "option_ids", "kind"}
_COMMENT_COMMON = {"id", "text", "confirmed"}
_COMMENT_LINKED = _COMMENT_COMMON | {"edit_id"}
_COMMENT_EXPLICIT = _COMMENT_COMMON | {
    "piece_id",
    "paragraph_sha256",
    "start",
    "end",
    "old_text",
}


class ValidationError(ValueError):
    """The source/plan pair cannot be rendered without guessing."""


@dataclass(frozen=True)
class _Piece:
    id: str
    text: str
    paragraph_sha256: str
    paragraph: etree._Element
    order: int


@dataclass(frozen=True)
class _Source:
    data: bytes
    sha256: str
    document_root: etree._Element
    document_xml: bytes
    pieces: dict[str, _Piece]
    names: frozenset[str]


@dataclass(frozen=True)
class _StyleSpan:
    start: int
    end: int
    rpr: etree._Element | None


@dataclass(frozen=True)
class _Anchor:
    comment_id: str
    piece_id: str
    start: int
    end: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _paragraph_sha256(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _parse_xml(data: bytes, label: str) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValidationError(f"{label} XML 无法解析。") from exc


def _xml_text(element: etree._Element) -> str:
    chunks: list[str] = []
    for node in element.iter():
        if node.tag == W + "t":
            chunks.append(node.text or "")
        elif node.tag == W + "tab":
            chunks.append("\t")
        elif node.tag in {W + "br", W + "cr"}:
            chunks.append("\n")
        elif node.tag == W + "noBreakHyphen":
            chunks.append("‑")
        elif node.tag == W + "softHyphen":
            chunks.append("\u00ad")
    return "".join(chunks)


def _paragraph_elements(root: etree._Element) -> dict[str, etree._Element]:
    body = root.find(W + "body")
    if body is None:
        raise ValidationError("DOCX 缺少正文区域。")
    result: dict[str, etree._Element] = {}
    paragraph_index = 0
    table_index = 0
    for child in body:
        if child.tag == W + "p":
            result[f"P{paragraph_index:03d}"] = child
            paragraph_index += 1
        elif child.tag == W + "tbl":
            rows = [node for node in child if node.tag == W + "tr"]
            for row_index, row in enumerate(rows):
                cells = [node for node in row if node.tag == W + "tc"]
                for cell_index, cell in enumerate(cells):
                    paragraphs = [node for node in cell if node.tag == W + "p"]
                    for paragraph_in_cell, paragraph in enumerate(paragraphs):
                        piece_id = (
                            f"T{table_index}R{row_index}C{cell_index}P{paragraph_in_cell}"
                        )
                        result[piece_id] = paragraph
            table_index += 1
    return result


def _load_source(data: bytes) -> _Source:
    if not isinstance(data, bytes):
        raise TypeError("DOCX 源必须是 bytes。")

    # Reuse the project's completeness/safety gate before touching the package.
    parsed = parse_docx(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document_xml = archive.read("word/document.xml")
            names = frozenset(archive.namelist())
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValidationError("DOCX 包缺少可用的 word/document.xml。") from exc

    root = _parse_xml(document_xml, "document")
    elements = _paragraph_elements(root)
    if any(
        node.tag in {W + "commentRangeStart", W + "commentRangeEnd", W + "commentReference"}
        for node in root.iter()
    ):
        raise ValidationError("源 DOCX 已含批注锚点；当前切片不合并已有批注。")

    pieces: dict[str, _Piece] = {}
    for order, parsed_piece in enumerate(parsed.pieces):
        paragraph = elements.get(parsed_piece.id)
        if paragraph is None or _xml_text(paragraph) != parsed_piece.text:
            raise ValidationError(f"{parsed_piece.id} 的 XML 定位与解析文本不一致。")
        pieces[parsed_piece.id] = _Piece(
            id=parsed_piece.id,
            text=parsed_piece.text,
            paragraph_sha256=_paragraph_sha256(parsed_piece.text),
            paragraph=paragraph,
            order=order,
        )
    return _Source(
        data=data,
        sha256=_sha256_bytes(data),
        document_root=root,
        document_xml=document_xml,
        pieces=pieces,
        names=names,
    )


@lru_cache(maxsize=4)
def _validation_material(data: bytes) -> tuple[_Source, dict[str, Any]]:
    """Content-keyed read-only XML; rendering must keep its separate fresh source."""
    source = _load_source(data)
    description = {
        "source_sha256": source.sha256,
        "parser_version": PARSER_VERSION,
        "pieces": [
            {
                "id": piece.id,
                "text": piece.text,
                "paragraph_sha256": piece.paragraph_sha256,
                "supports_paragraph_insertion": _supports_paragraph_insertion(piece),
            }
            for piece in sorted(source.pieces.values(), key=lambda item: item.order)
        ],
    }
    return source, description


def inspect_source(data: bytes) -> dict[str, Any]:
    """Return isolated source metadata without exposing cached XML or mutable rows."""
    if not isinstance(data, bytes):
        _load_source(data)  # Preserve the established validation error for invalid types.
    return copy.deepcopy(_validation_material(data)[1])


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} 必须是对象。")
    return value


def _strict_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], label: str
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValidationError(f"{label} 缺少字段：{', '.join(sorted(missing))}。")
    if unknown:
        raise ValidationError(f"{label} 含未知字段：{', '.join(sorted(unknown))}。")


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} 必须是非空字符串。")
    return value


def _string_ids(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} 必须是字符串数组。")
    result: list[str] = []
    for item in value:
        result.append(_nonempty_string(item, label))
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} 不能含重复值。")
    return result


def _utc_created_at(value: Any) -> str:
    text = _nonempty_string(value, "created_at")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError("created_at 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValidationError("created_at 必须显式使用 UTC 时区。")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _require_plain_paragraph(piece: _Piece, purpose: str) -> None:
    allowed_p = {W + "pPr", W + "r", W + "proofErr"}
    allowed_r = {W + "rPr", W + "t", W + "tab", W + "br", W + "cr"}
    for child in piece.paragraph:
        if child.tag not in allowed_p:
            name = etree.QName(child).localname
            raise ValidationError(
                f"{purpose} {piece.id} 含复杂段落节点 {name}；当前只支持普通文本 runs。"
            )
        if child.tag == W + "r":
            for run_child in child:
                if run_child.tag not in allowed_r:
                    name = etree.QName(run_child).localname
                    raise ValidationError(
                        f"{purpose} {piece.id} 的 run 含复杂节点 {name}；当前不改写该段。"
                    )
        elif child.tag == W + "proofErr":
            proof_type = child.get(W + "type")
            if len(child) or proof_type not in {
                "spellStart",
                "spellEnd",
                "gramStart",
                "gramEnd",
            }:
                raise ValidationError(
                    f"{purpose} {piece.id} 含无法识别的 proofErr；当前不改写该段。"
                )
    if _plain_run_text(piece.paragraph) != piece.text:
        raise ValidationError(f"{purpose} {piece.id} 不能由普通文本 runs 完整表示。")


def _plain_run_text(paragraph: etree._Element) -> str:
    chunks: list[str] = []
    for run in paragraph:
        if run.tag != W + "r":
            continue
        for child in run:
            if child.tag == W + "t":
                chunks.append(child.text or "")
            elif child.tag == W + "tab":
                chunks.append("\t")
            elif child.tag in {W + "br", W + "cr"}:
                chunks.append("\n")
    return "".join(chunks)


def _supports_paragraph_insertion(piece: _Piece) -> bool:
    if piece.paragraph.getparent().tag != W + "body":
        return False
    try:
        _require_plain_paragraph(piece, "新增段落锚点")
    except ValidationError:
        return False
    return True


def _check_edit_conflicts(edits: list[dict[str, Any]], piece_id: str) -> None:
    for index, left in enumerate(edits):
        for right in edits[index + 1 :]:
            if (left.get("kind") == "insert_paragraph_after"
                    or right.get("kind") == "insert_paragraph_after"):
                if left.get("kind") == right.get("kind") == "insert_paragraph_after":
                    raise ValidationError(f"{piece_id} 不能有多个顺序未确定的新增段落操作；请合为一个操作。")
                # Separate paragraph additions do not replace anchor characters.
                continue
            left_insert = left["start"] == left["end"]
            right_insert = right["start"] == right["end"]
            if left_insert and right_insert:
                conflict = left["start"] == right["start"]
            elif left_insert:
                conflict = right["start"] <= left["start"] <= right["end"]
            elif right_insert:
                conflict = left["start"] <= right["start"] <= left["end"]
            else:
                conflict = max(left["start"], right["start"]) < min(
                    left["end"], right["end"]
                )
            if conflict:
                raise ValidationError(
                    f"{piece_id} 的 edits {left['id']} 与 {right['id']} 重叠或顺序含糊。"
                )


def _position_is_changed(position: int, edits: Iterable[dict[str, Any]]) -> bool:
    return any(
        edit["start"] < edit["end"] and edit["start"] <= position < edit["end"]
        for edit in edits
    )


def _adjacent_anchor(
    edit: Mapping[str, Any], text: str, edits: Iterable[dict[str, Any]], label: str
) -> tuple[int, int]:
    candidates = (edit["end"], edit["start"] - 1)
    for position in candidates:
        if 0 <= position < len(text) and not _position_is_changed(position, edits):
            return position, position + 1
    raise ValidationError(f"{label} 找不到可见且未被修改的相邻字符作为批注锚点。")


def _anchors_overlap(anchors: list[_Anchor], label: str) -> None:
    by_piece: dict[str, list[_Anchor]] = {}
    for anchor in anchors:
        by_piece.setdefault(anchor.piece_id, []).append(anchor)
    for piece_id, items in by_piece.items():
        items.sort(key=lambda item: (item.start, item.end, item.comment_id))
        for previous, current in zip(items, items[1:]):
            if max(previous.start, current.start) < min(previous.end, current.end):
                raise ValidationError(
                    f"{label}在 {piece_id} 交叉或重叠：{previous.comment_id} / {current.comment_id}。"
                )


def _normalized_plan(source: _Source, raw_plan: Any) -> dict[str, Any]:
    plan = _mapping(raw_plan, "plan")
    _strict_keys(plan, _ROOT_KEYS, set(), "plan")
    if not isinstance(plan["schema_version"], int) or isinstance(
        plan["schema_version"], bool
    ) or plan["schema_version"] != 1:
        raise ValidationError("schema_version 只支持整数 1。")
    if plan["source_sha256"] != source.sha256:
        raise ValidationError("source_sha256 与本次原始 DOCX 不一致。")
    revision = plan["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise ValidationError("revision 必须是正整数。")
    author = _nonempty_string(plan["author"], "author")
    created_at = _utc_created_at(plan["created_at"])
    if not isinstance(plan["edits"], list) or not isinstance(plan["comments"], list):
        raise ValidationError("edits 和 comments 必须是数组。")

    normalized_edits: list[dict[str, Any]] = []
    edit_ids: set[str] = set()
    for index, raw_edit in enumerate(plan["edits"]):
        edit = _mapping(raw_edit, f"edits[{index}]")
        _strict_keys(edit, _EDIT_REQUIRED, _EDIT_OPTIONAL, f"edits[{index}]")
        edit_id = _nonempty_string(edit["id"], f"edits[{index}].id")
        if edit_id in edit_ids:
            raise ValidationError(f"edit id 重复：{edit_id}。")
        edit_ids.add(edit_id)
        if edit["confirmed"] is not True:
            raise ValidationError(f"edit {edit_id} 未被明确确认。")
        piece_id = _nonempty_string(edit["piece_id"], f"edit {edit_id}.piece_id")
        piece = source.pieces.get(piece_id)
        if piece is None:
            raise ValidationError(f"edit {edit_id} 引用了未知 piece：{piece_id}。")
        if edit["paragraph_sha256"] != piece.paragraph_sha256:
            raise ValidationError(f"edit {edit_id} 的 paragraph_sha256 已失效。")
        start, end = edit["start"], edit["end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end < start
            or end > len(piece.text)
        ):
            raise ValidationError(f"edit {edit_id} 的 start/end 越界或类型错误。")
        if not isinstance(edit["old_text"], str) or not isinstance(edit["new_text"], str):
            raise ValidationError(f"edit {edit_id} 的 old_text/new_text 必须是字符串。")
        if piece.text[start:end] != edit["old_text"]:
            raise ValidationError(f"edit {edit_id} 的 old_text 与原稿偏移不一致。")
        if edit["old_text"] == edit["new_text"]:
            raise ValidationError(f"edit {edit_id} 是无变化编辑。")
        kind = edit.get("kind", "replace")
        if kind not in {"replace", "insert_paragraph_after"}:
            raise ValidationError(f"edit {edit_id} 的 kind 不受支持。")
        if kind == "insert_paragraph_after":
            if (start != len(piece.text) or end != start or edit["old_text"] != ""
                    or not edit["new_text"].strip() or not _supports_paragraph_insertion(piece)):
                raise ValidationError(f"edit {edit_id} 必须在可用普通正文段落末尾新增独立段落。")
            if any(not line.strip() for line in edit["new_text"].splitlines()):
                raise ValidationError(f"edit {edit_id} 的新增段落不能包含空白段落。")
        normalized = {
            "id": edit_id,
            "piece_id": piece_id,
            "paragraph_sha256": piece.paragraph_sha256,
            "start": start,
            "end": end,
            "old_text": edit["old_text"],
            "new_text": edit["new_text"],
            "confirmed": True,
        }
        if "kind" in edit:
            normalized["kind"] = kind
        for optional in ("finding_ids", "option_ids"):
            if optional in edit:
                normalized[optional] = _string_ids(
                    edit[optional], f"edit {edit_id}.{optional}"
                )
        normalized_edits.append(normalized)
        _require_plain_paragraph(piece, f"edit {edit_id}")

    normalized_edits.sort(
        key=lambda item: (
            source.pieces[item["piece_id"]].order,
            item["start"],
            item["end"],
            item["id"],
        )
    )
    edits_by_piece: dict[str, list[dict[str, Any]]] = {}
    edits_by_id: dict[str, dict[str, Any]] = {}
    for edit in normalized_edits:
        edits_by_piece.setdefault(edit["piece_id"], []).append(edit)
        edits_by_id[edit["id"]] = edit
    for piece_id, piece_edits in edits_by_piece.items():
        _check_edit_conflicts(piece_edits, piece_id)

    normalized_comments: list[dict[str, Any]] = []
    comment_ids: set[str] = set()
    linked_edits: set[str] = set()
    for index, raw_comment in enumerate(plan["comments"]):
        comment = _mapping(raw_comment, f"comments[{index}]")
        keys = set(comment)
        if keys == _COMMENT_LINKED:
            mode = "linked"
        elif keys == _COMMENT_EXPLICIT:
            mode = "explicit"
        else:
            raise ValidationError(
                f"comments[{index}] 必须使用 edit_id 或完整显式原稿锚点，且不能含其它字段。"
            )
        comment_id = _nonempty_string(comment["id"], f"comments[{index}].id")
        if comment_id in comment_ids:
            raise ValidationError(f"comment id 重复：{comment_id}。")
        comment_ids.add(comment_id)
        if comment["confirmed"] is not True:
            raise ValidationError(f"comment {comment_id} 未被明确确认。")
        text = _nonempty_string(comment["text"], f"comment {comment_id}.text")
        if mode == "linked":
            edit_id = _nonempty_string(comment["edit_id"], f"comment {comment_id}.edit_id")
            if edit_id not in edits_by_id:
                raise ValidationError(f"comment {comment_id} 引用了未知 edit：{edit_id}。")
            if edit_id in linked_edits:
                raise ValidationError(f"edit {edit_id} 只能关联一个公开批注。")
            linked_edits.add(edit_id)
            normalized_comments.append(
                {"id": comment_id, "text": text, "confirmed": True, "edit_id": edit_id}
            )
            continue

        piece_id = _nonempty_string(
            comment["piece_id"], f"comment {comment_id}.piece_id"
        )
        piece = source.pieces.get(piece_id)
        if piece is None:
            raise ValidationError(f"comment {comment_id} 引用了未知 piece：{piece_id}。")
        if comment["paragraph_sha256"] != piece.paragraph_sha256:
            raise ValidationError(f"comment {comment_id} 的 paragraph_sha256 已失效。")
        start, end = comment["start"], comment["end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(piece.text)
        ):
            raise ValidationError(f"comment {comment_id} 必须锚定非空且有效的原稿范围。")
        if not isinstance(comment["old_text"], str) or piece.text[start:end] != comment["old_text"]:
            raise ValidationError(f"comment {comment_id} 的 old_text 与原稿偏移不一致。")
        for edit in edits_by_piece.get(piece_id, []):
            if edit["start"] == edit["end"]:
                intersects = start <= edit["start"] <= end
            else:
                intersects = max(start, edit["start"]) < min(end, edit["end"])
            if intersects:
                raise ValidationError(
                    f"comment {comment_id} 的显式锚点与 edit {edit['id']} 相交。"
                )
        _require_plain_paragraph(piece, f"comment {comment_id}")
        normalized_comments.append(
            {
                "id": comment_id,
                "text": text,
                "confirmed": True,
                "piece_id": piece_id,
                "paragraph_sha256": piece.paragraph_sha256,
                "start": start,
                "end": end,
                "old_text": comment["old_text"],
            }
        )

    normalized_comments.sort(key=lambda item: item["id"])
    normalized_plan = {
        "schema_version": 1,
        "source_sha256": source.sha256,
        "revision": revision,
        "author": author,
        "created_at": created_at,
        "edits": normalized_edits,
        "comments": normalized_comments,
    }

    # Validate both overlay modes now so render variants cannot diverge later.
    original_anchors = _resolve_source_anchors(
        source, normalized_plan, tracked=False, edits_by_piece=edits_by_piece
    )
    tracked_anchors = _resolve_source_anchors(
        source, normalized_plan, tracked=True, edits_by_piece=edits_by_piece
    )
    _anchors_overlap(original_anchors, "原稿批注锚点")
    _anchors_overlap(tracked_anchors, "修订版批注锚点")
    return normalized_plan


def validate_plan(data: bytes, plan: Any) -> dict[str, Any]:
    """Validate and normalize a delivery plan against immutable source bytes."""

    if not isinstance(data, bytes):
        _load_source(data)
    return _normalized_plan(_validation_material(data)[0], plan)


def _edits_by_piece(plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for edit in plan["edits"]:
        result.setdefault(edit["piece_id"], []).append(edit)
    return result


def _resolve_source_anchors(
    source: _Source,
    plan: Mapping[str, Any],
    *,
    tracked: bool,
    edits_by_piece: dict[str, list[dict[str, Any]]] | None = None,
) -> list[_Anchor]:
    edits_by_piece = edits_by_piece or _edits_by_piece(plan)
    edits_by_id = {edit["id"]: edit for edit in plan["edits"]}
    anchors: list[_Anchor] = []
    for comment in plan["comments"]:
        if "edit_id" not in comment:
            anchors.append(
                _Anchor(
                    comment["id"], comment["piece_id"], comment["start"], comment["end"]
                )
            )
            continue
        edit = edits_by_id[comment["edit_id"]]
        piece = source.pieces[edit["piece_id"]]
        if tracked and edit["new_text"]:
            # This anchor is emitted around the edit's w:ins, not source text.
            continue
        if not tracked and edit["start"] < edit["end"]:
            start, end = edit["start"], edit["end"]
        else:
            start, end = _adjacent_anchor(
                edit,
                piece.text,
                edits_by_piece.get(piece.id, []),
                f"comment {comment['id']}",
            )
        anchors.append(_Anchor(comment["id"], piece.id, start, end))
    return anchors


def _style_spans(piece: _Piece) -> tuple[list[_StyleSpan], etree._Element | None]:
    spans: list[_StyleSpan] = []
    position = 0
    fallback: etree._Element | None = None
    for run in piece.paragraph:
        if run.tag != W + "r":
            continue
        rpr = run.find(W + "rPr")
        if fallback is None and rpr is not None:
            fallback = copy.deepcopy(rpr)
        text_parts: list[str] = []
        for child in run:
            if child.tag == W + "t":
                text_parts.append(child.text or "")
            elif child.tag == W + "tab":
                text_parts.append("\t")
            elif child.tag in {W + "br", W + "cr"}:
                text_parts.append("\n")
        run_text = "".join(text_parts)
        if run_text:
            spans.append(
                _StyleSpan(
                    position,
                    position + len(run_text),
                    copy.deepcopy(rpr) if rpr is not None else None,
                )
            )
            position += len(run_text)
    return spans, fallback


def _proof_markers(piece: _Piece) -> dict[int, list[etree._Element]]:
    """Return proofing anchors at their source-text boundary.

    ``w:proofErr`` is non-text markup between runs.  It is safe to retain when
    adding comments to unchanged text, but all such anchors are intentionally
    omitted when that paragraph's text is edited because their ranges may then
    be stale.
    """

    markers: dict[int, list[etree._Element]] = {}
    position = 0
    for child in piece.paragraph:
        if child.tag == W + "proofErr":
            markers.setdefault(position, []).append(copy.deepcopy(child))
        elif child.tag == W + "r":
            position += len(_xml_text(child))
    return markers


def _style_at(
    spans: list[_StyleSpan], fallback: etree._Element | None, position: int, *, insert: bool
) -> etree._Element | None:
    candidates = (position - 1, position) if insert else (position, position - 1)
    for candidate in candidates:
        for span in spans:
            if span.start <= candidate < span.end:
                return copy.deepcopy(span.rpr) if span.rpr is not None else None
    return copy.deepcopy(fallback) if fallback is not None else None


def _text_nodes(text: str, *, deleted: bool) -> list[etree._Element]:
    result: list[etree._Element] = []
    tag = W + ("delText" if deleted else "t")
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        value = "".join(buffer)
        node = etree.Element(tag)
        node.text = value
        if value[:1].isspace() or value[-1:].isspace():
            node.set(f"{{{XML_NS}}}space", "preserve")
        result.append(node)
        buffer.clear()

    for character in text:
        if character == "\t":
            flush()
            result.append(etree.Element(W + "tab"))
        elif character in {"\n", "\r"}:
            flush()
            result.append(etree.Element(W + "br"))
        else:
            buffer.append(character)
    flush()
    return result


def _make_run(
    text: str, rpr: etree._Element | None, *, deleted: bool = False
) -> etree._Element | None:
    if not text:
        return None
    run = etree.Element(W + "r")
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    for child in _text_nodes(text, deleted=deleted):
        run.append(child)
    return run


def _runs_for_slice(
    text: str,
    spans: list[_StyleSpan],
    start: int,
    end: int,
    *,
    deleted: bool = False,
) -> list[etree._Element]:
    result: list[etree._Element] = []
    for span in spans:
        left, right = max(start, span.start), min(end, span.end)
        if left >= right:
            continue
        run = _make_run(text[left:right], span.rpr, deleted=deleted)
        if run is not None:
            result.append(run)
    return result


def _comment_start(comment_number: int) -> etree._Element:
    node = etree.Element(W + "commentRangeStart")
    node.set(W + "id", str(comment_number))
    return node


def _comment_end_and_reference(comment_number: int) -> list[etree._Element]:
    end = etree.Element(W + "commentRangeEnd")
    end.set(W + "id", str(comment_number))
    run = etree.Element(W + "r")
    rpr = etree.SubElement(run, W + "rPr")
    style = etree.SubElement(rpr, W + "rStyle")
    style.set(W + "val", "CommentReference")
    reference = etree.SubElement(run, W + "commentReference")
    reference.set(W + "id", str(comment_number))
    return [end, run]


def _source_anchor_events(
    anchors: Iterable[_Anchor], comment_numbers: Mapping[str, int]
) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    starts: dict[int, list[int]] = {}
    ends: dict[int, list[int]] = {}
    for anchor in anchors:
        number = comment_numbers[anchor.comment_id]
        starts.setdefault(anchor.start, []).append(number)
        ends.setdefault(anchor.end, []).append(number)
    return starts, ends


def _append_events(
    children: list[etree._Element],
    position: int,
    starts: dict[int, list[int]],
    ends: dict[int, list[int]],
    proof_markers: Mapping[int, list[etree._Element]] | None = None,
) -> None:
    for number in sorted(ends.get(position, [])):
        children.extend(_comment_end_and_reference(number))
    if proof_markers:
        children.extend(copy.deepcopy(proof_markers.get(position, [])))
    for number in sorted(starts.get(position, [])):
        children.append(_comment_start(number))


def _replace_paragraph_children(
    paragraph: etree._Element, children: list[etree._Element]
) -> None:
    ppr = paragraph.find(W + "pPr")
    for child in list(paragraph):
        paragraph.remove(child)
    if ppr is not None:
        paragraph.append(copy.deepcopy(ppr))
    paragraph.extend(children)


def _render_original_comments(
    source: _Source,
    plan: Mapping[str, Any],
    comment_numbers: Mapping[str, int],
) -> None:
    anchors = _resolve_source_anchors(source, plan, tracked=False)
    anchors_by_piece: dict[str, list[_Anchor]] = {}
    for anchor in anchors:
        anchors_by_piece.setdefault(anchor.piece_id, []).append(anchor)

    for piece_id, piece_anchors in anchors_by_piece.items():
        piece = source.pieces[piece_id]
        spans, _ = _style_spans(piece)
        proof_markers = _proof_markers(piece)
        starts, ends = _source_anchor_events(piece_anchors, comment_numbers)
        boundaries = sorted(
            {0, len(piece.text), *starts.keys(), *ends.keys(), *proof_markers.keys()}
        )
        children: list[etree._Element] = []
        for start, end in zip(boundaries, boundaries[1:]):
            _append_events(children, start, starts, ends, proof_markers)
            children.extend(_runs_for_slice(piece.text, spans, start, end))
        _append_events(children, boundaries[-1], starts, ends, proof_markers)
        _replace_paragraph_children(piece.paragraph, children)


def _revision_wrapper(
    tag: str,
    revision_id: int,
    author: str,
    created_at: str,
    runs: Iterable[etree._Element],
) -> etree._Element:
    wrapper = etree.Element(W + tag)
    wrapper.set(W + "id", str(revision_id))
    wrapper.set(W + "author", author)
    wrapper.set(W + "date", created_at)
    wrapper.extend(runs)
    return wrapper


def _render_edits(
    source: _Source,
    plan: Mapping[str, Any],
    *,
    tracked: bool,
    with_comments: bool,
    comment_numbers: Mapping[str, int],
) -> None:
    edits_by_piece = _edits_by_piece(plan)
    linked_comment_by_edit = {
        comment["edit_id"]: comment["id"]
        for comment in plan["comments"]
        if "edit_id" in comment
    }
    source_anchors = (
        _resolve_source_anchors(source, plan, tracked=True, edits_by_piece=edits_by_piece)
        if with_comments
        else []
    )
    source_anchors_by_piece: dict[str, list[_Anchor]] = {}
    for anchor in source_anchors:
        source_anchors_by_piece.setdefault(anchor.piece_id, []).append(anchor)

    target_piece_ids = {
        piece_id for piece_id, edits in edits_by_piece.items()
        if any(edit.get("kind") != "insert_paragraph_after" for edit in edits)
    }
    if with_comments:
        target_piece_ids.update(source_anchors_by_piece)

    revision_number = 0
    for piece_id in sorted(target_piece_ids, key=lambda item: source.pieces[item].order):
        piece = source.pieces[piece_id]
        spans, fallback = _style_spans(piece)
        piece_edits = [edit for edit in edits_by_piece.get(piece_id, [])
                       if edit.get("kind") != "insert_paragraph_after"]
        # Comments on an otherwise unchanged paragraph preserve Word's
        # spelling/grammar anchors.  Any real text edit drops all proofErr
        # anchors in that paragraph because their ranges are no longer valid.
        proof_markers = {} if piece_edits else _proof_markers(piece)
        starts, ends = _source_anchor_events(
            source_anchors_by_piece.get(piece_id, []), comment_numbers
        )
        event_boundaries = sorted({*starts.keys(), *ends.keys(), *proof_markers.keys()})
        children: list[etree._Element] = []
        cursor = 0

        def emit_normal(until: int) -> None:
            nonlocal cursor
            positions = [position for position in event_boundaries if cursor < position < until]
            for boundary in [*positions, until]:
                _append_events(children, cursor, starts, ends, proof_markers)
                children.extend(_runs_for_slice(piece.text, spans, cursor, boundary))
                cursor = boundary

        for edit in piece_edits:
            emit_normal(edit["start"])
            _append_events(children, cursor, starts, ends, proof_markers)
            if edit["start"] < edit["end"]:
                if tracked:
                    deleted_runs = _runs_for_slice(
                        piece.text, spans, edit["start"], edit["end"], deleted=True
                    )
                    children.append(
                        _revision_wrapper(
                            "del",
                            revision_number,
                            plan["author"],
                            plan["created_at"],
                            deleted_runs,
                        )
                    )
                    revision_number += 1
                cursor = edit["end"]
            new_text = edit["new_text"]
            if new_text:
                rpr = _style_at(
                    spans,
                    fallback,
                    edit["start"],
                    insert=edit["start"] == edit["end"],
                )
                new_run = _make_run(new_text, rpr)
                assert new_run is not None
                if tracked:
                    insertion = _revision_wrapper(
                        "ins",
                        revision_number,
                        plan["author"],
                        plan["created_at"],
                        [new_run],
                    )
                    revision_number += 1
                    linked_comment_id = linked_comment_by_edit.get(edit["id"])
                    if with_comments and linked_comment_id is not None:
                        number = comment_numbers[linked_comment_id]
                        children.append(_comment_start(number))
                        children.append(insertion)
                        children.extend(_comment_end_and_reference(number))
                    else:
                        children.append(insertion)
                else:
                    children.append(new_run)
        emit_normal(len(piece.text))
        _append_events(children, cursor, starts, ends, proof_markers)
        _replace_paragraph_children(piece.paragraph, children)

    for edit in plan["edits"]:
        if edit.get("kind") != "insert_paragraph_after":
            continue
        piece = source.pieces[edit["piece_id"]]
        spans, fallback = _style_spans(piece)
        rpr = _style_at(spans, fallback, len(piece.text), insert=True)
        previous = piece.paragraph
        for index, text in enumerate(edit["new_text"].splitlines()):
            paragraph = etree.Element(W + "p")
            original_ppr = piece.paragraph.find(W + "pPr")
            ppr = copy.deepcopy(original_ppr) if original_ppr is not None else etree.Element(W + "pPr")
            # Explicit clause numbers must not acquire an inherited automatic
            # number, section break, or source paragraph-mark revisions.
            for node in list(ppr):
                if node.tag in {W + "numPr", W + "sectPr", W + "rPr"}:
                    ppr.remove(node)
            if tracked:
                mark_pr = etree.SubElement(ppr, W + "rPr")
                mark_pr.append(_revision_wrapper("ins", revision_number, plan["author"], plan["created_at"], []))
                revision_number += 1
            if len(ppr):
                paragraph.append(ppr)
            run = _make_run(text, rpr)
            assert run is not None
            content = run
            if tracked:
                content = _revision_wrapper("ins", revision_number, plan["author"], plan["created_at"], [run])
                revision_number += 1
            linked_id = linked_comment_by_edit.get(edit["id"]) if with_comments and index == 0 else None
            if linked_id is not None:
                number = comment_numbers[linked_id]
                paragraph.append(_comment_start(number))
                paragraph.append(content)
                paragraph.extend(_comment_end_and_reference(number))
            else:
                paragraph.append(content)
            previous.addnext(paragraph)
            previous = paragraph


def _comments_xml(plan: Mapping[str, Any], comment_numbers: Mapping[str, int]) -> bytes:
    root = etree.Element(W + "comments", nsmap={"w": W_NS})
    for comment in plan["comments"]:
        node = etree.SubElement(root, W + "comment")
        node.set(W + "id", str(comment_numbers[comment["id"]]))
        node.set(W + "author", plan["author"])
        node.set(W + "date", plan["created_at"])
        paragraph = etree.SubElement(node, W + "p")
        reference_run = etree.SubElement(paragraph, W + "r")
        reference_pr = etree.SubElement(reference_run, W + "rPr")
        reference_style = etree.SubElement(reference_pr, W + "rStyle")
        reference_style.set(W + "val", "CommentReference")
        etree.SubElement(reference_run, W + "annotationRef")
        text_run = etree.SubElement(paragraph, W + "r")
        for child in _text_nodes(comment["text"], deleted=False):
            text_run.append(child)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _ensure_comments_relationship(data: bytes) -> bytes:
    root = _parse_xml(data, "document relationships")
    existing = [
        node
        for node in root
        if node.tag == PKG_REL + "Relationship" and node.get("Type") == COMMENTS_REL_TYPE
    ]
    if existing:
        if len(existing) != 1 or existing[0].get("Target") != "comments.xml":
            raise ValidationError("源 DOCX 的 comments relationship 结构不受支持。")
        return data
    ids = {node.get("Id", "") for node in root if node.tag == PKG_REL + "Relationship"}
    number = 1
    while f"rId{number}" in ids:
        number += 1
    rel = etree.SubElement(root, PKG_REL + "Relationship")
    rel.set("Id", f"rId{number}")
    rel.set("Type", COMMENTS_REL_TYPE)
    rel.set("Target", "comments.xml")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _new_relationships() -> bytes:
    root = etree.Element(PKG_REL + "Relationships", nsmap={None: PKG_REL_NS})
    rel = etree.SubElement(root, PKG_REL + "Relationship")
    rel.set("Id", "rId1")
    rel.set("Type", COMMENTS_REL_TYPE)
    rel.set("Target", "comments.xml")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _ensure_comments_content_type(data: bytes) -> bytes:
    root = _parse_xml(data, "content types")
    existing = [
        node
        for node in root
        if node.tag == CT + "Override" and node.get("PartName") == "/word/comments.xml"
    ]
    if existing:
        if len(existing) != 1 or existing[0].get("ContentType") != COMMENTS_CONTENT_TYPE:
            raise ValidationError("源 DOCX 的 comments content type 结构不受支持。")
        return data
    override = etree.SubElement(root, CT + "Override")
    override.set("PartName", "/word/comments.xml")
    override.set("ContentType", COMMENTS_CONTENT_TYPE)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _rewrite_zip(source: bytes, replacements: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as old, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as new:
        written: set[str] = set()
        for info in old.infolist():
            payload = replacements.get(info.filename, old.read(info.filename))
            new.writestr(info, payload)
            written.add(info.filename)
        for name, payload in replacements.items():
            if name not in written:
                new.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def render_docx(data: bytes, plan: Any, variant: str) -> bytes:
    """Render ``clean``, ``tracked``, ``comments`` or ``tracked_comments`` DOCX."""

    if variant not in {"clean", "tracked", "comments", "tracked_comments"}:
        raise ValidationError(f"未知 DOCX variant：{variant}。")
    source = _load_source(data)
    normalized = _normalized_plan(source, plan)
    if not normalized["edits"] and variant in {"clean", "tracked"}:
        return data
    if not normalized["comments"] and variant == "comments":
        return data

    comment_numbers = {
        comment["id"]: index for index, comment in enumerate(normalized["comments"])
    }
    if variant == "comments":
        _render_original_comments(source, normalized, comment_numbers)
    else:
        _render_edits(
            source,
            normalized,
            tracked=variant in {"tracked", "tracked_comments"},
            with_comments=variant == "tracked_comments",
            comment_numbers=comment_numbers,
        )

    document_xml = etree.tostring(
        source.document_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    replacements: dict[str, bytes] = {"word/document.xml": document_xml}
    if variant in {"comments", "tracked_comments"} and normalized["comments"]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            content_types = archive.read("[Content_Types].xml")
            if "word/_rels/document.xml.rels" in archive.namelist():
                rels = _ensure_comments_relationship(
                    archive.read("word/_rels/document.xml.rels")
                )
            else:
                rels = _new_relationships()
        replacements["[Content_Types].xml"] = _ensure_comments_content_type(content_types)
        replacements["word/_rels/document.xml.rels"] = rels
        replacements["word/comments.xml"] = _comments_xml(normalized, comment_numbers)
    return _rewrite_zip(data, replacements)


__all__ = [
    "PARSER_VERSION",
    "ValidationError",
    "inspect_source",
    "validate_plan",
    "render_docx",
]
