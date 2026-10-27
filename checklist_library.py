"""Compile authored four-column checklists into the product's business fields.

This explicit maintenance command does not run at request time or read a house
path implicitly. Historical job snapshots and personal checklists are untouched.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from checklist_store import ChecklistError, normalize_checklist_snapshot


SPECS = (
    ("通用合同-检查清单.md", "builtin-general-v01", "通用合同清单", "通用合同", "G", 24),
    ("定制软件开发服务-客户立场-复核稿.md", "builtin-software-client-v02", "软件开发清单", "定制软件开发", "C", 31),
)
HEADER = ["检查事项（编号）", "检查内容", "适用情形", "备注"]


def _columns(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def cell_markdown(value: str) -> str:
    """Keep legal wording intact while adapting table line breaks for the UI."""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = html.unescape(value.replace(r"\|", "|"))
    lines = []
    for line in value.splitlines():
        line = line.strip()
        heading = re.fullmatch(r"\*\*(.+)\*\*", line)
        if heading:
            line = "### " + heading[1]
        elif line.startswith("• "):
            line = "- " + line[2:]
        lines.append(line)
    return "\n".join(lines).strip()


def parse_checklist(text: str, *, checklist_id: str, name: str,
                    contract_type: str, prefix: str, expected_count: int) -> dict:
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    front = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
    if not front:
        raise ChecklistError("清单缺少版本属性。")
    version = re.search(r'^版本:\s*["\']?([0-9]+(?:\.[0-9]+)*)["\']?\s*$', front[1], re.M)
    if not version:
        raise ChecklistError("清单版本格式无效。")
    metadata = {}
    for label, key in (("适用范围", "scope"), ("审查立场", "stance")):
        found = re.findall(r"^\*\*" + label + r"\*\*[：:]\s*(.+)$", text, re.M)
        if len(found) != 1 or not found[0].strip():
            raise ChecklistError(f"清单必须明确且只写一处{label}。")
        metadata[key] = found[0].strip()
    items = []
    in_checks = False
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if not line.strip().startswith("|"):
            in_checks = False
            continue
        cells = _columns(line)
        if cells == HEADER:
            in_checks = True
            continue
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if not in_checks:
            continue  # directory tables are not review content
        identity = re.fullmatch(r"\*\*(" + re.escape(prefix) + r"\d{2,3})\*\*<br\s*/?>\s*(.+)",
                                cells[0] if cells else "", re.I)
        if len(cells) != 4 or not identity:
            raise ChecklistError(f"第{number}行不是完整的四列检查项，未跳过或截断。")
        title, requirement, when, notes = (cell_markdown(value) for value in
                                         (identity[2], cells[1], cells[2], cells[3]))
        items.append({"id": identity[1], "title": title, "requirement": requirement,
                      "when": "" if when == "—" else when, "notes": "" if notes == "—" else notes,
                      "facts": "", "guidance": "", "enabled": True})
    expected = [f"{prefix}{index:02d}" for index in range(1, expected_count + 1)]
    if [item["id"] for item in items] != expected:
        raise ChecklistError("检查项缺失、重复、顺序或数量变化，请先核对源文件再更新编译配置。")
    return normalize_checklist_snapshot({"id": checklist_id, "name": name,
                                        "contract_type": contract_type, **metadata,
                                        "version": version[1], "builtin": True, "items": items})


def parse_rules(text: str) -> str:
    match = re.search(r"^## 规则正文\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match or not match[1].strip():
        raise ChecklistError("未找到通用审查规则正文。")
    return match[1].strip() + "\n"


def compile_library(root: Path) -> tuple[dict, str]:
    folder = Path(root) / "05-实务工具" / "审核清单"
    lists = [parse_checklist((folder / filename).read_text(encoding="utf-8-sig"),
                            checklist_id=identity, name=name, contract_type=kind,
                            prefix=prefix, expected_count=count)
             for filename, identity, name, kind, prefix, count in SPECS]
    rules = parse_rules((folder / "通用审查规则.md").read_text(encoding="utf-8-sig"))
    return {"checklists": lists}, rules


def main() -> None:
    parser = argparse.ArgumentParser(description="将已核清单表格及共同规则编译到产品文件，不修改历史任务或个人清单。")
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("builtin_checklists.json"))
    parser.add_argument("--rules-output", type=Path,
                        default=Path(__file__).with_name("prompts") / "common_review_rules.md")
    parser.add_argument("--check", action="store_true", help="只比较现役文件是否与库内正文一致")
    args = parser.parse_args()
    catalog, rules = compile_library(args.library_root)
    data = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if json.loads(args.output.read_text(encoding="utf-8-sig")) != catalog or args.rules_output.read_text(encoding="utf-8-sig") != rules:
            raise SystemExit("产品清单或规则与源文件不同，未自动覆盖。")
    else:
        args.output.write_text(data, encoding="utf-8", newline="\n")
        args.rules_output.write_text(rules, encoding="utf-8", newline="\n")
    print("通用24项、软件31项及通用规则正文核对完成。")


if __name__ == "__main__":
    main()
