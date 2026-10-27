"""Versioned deterministic review units bound to the frozen check requirements."""
from __future__ import annotations

import re
from typing import Any

PROTOCOL_VERSION = 1
VERSION_FIELD = "checklist_group_protocol_version"


def split_groups(check: dict[str, Any]) -> list[dict[str, str]]:
    text = check.get("requirement", "")
    if not isinstance(text, str):
        raise ValueError("检查内容必须是文本。")
    sections: list[tuple[str, str]] = []
    lines: list[str] = []
    title = "其他检查内容"
    fence = None
    has_heading = False
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        heading = None if fence is not None or marker else re.match(r"^\s{0,3}###\s+(.+?)\s*$", line)
        if heading:
            if lines and "".join(lines).strip():
                sections.append((title, "".join(lines)))
                lines = []
            title = re.sub(r"\s+#+\s*$", "", heading.group(1)).strip()
            lines.append(line)
            has_heading = True
        else:
            lines.append(line)
    if has_heading:
        if lines and "".join(lines).strip():
            sections.append((title, "".join(lines)))
    else:
        # Plain requirements remain reviewable; no headings are invented in the source.
        sections = [(check["title"], text)]
    return [{"group_id": f"{check['id']}:G{n:02d}", "title": title, "requirement": content}
            for n, (title, content) in enumerate(sections, 1)]


def freeze_groups(preparation: dict[str, Any], version: int = PROTOCOL_VERSION) -> None:
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError("不支持的清单分组协议版本。")
    preparation[VERSION_FIELD] = version
    for check in preparation["checks"]:
        check["groups"] = split_groups(check)


def uses_groups(preparation: dict[str, Any] | None) -> bool:
    if not preparation or VERSION_FIELD not in preparation:
        return False
    version = preparation[VERSION_FIELD]
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError("不支持的清单分组协议版本。")
    for check in preparation["checks"]:
        if check.get("groups") != split_groups(check):
            raise ValueError("清单分组与冻结的检查内容不一致。")
    return True
