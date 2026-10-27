"""Read-only projection of a validated delivery plan onto original quote anchors."""
from __future__ import annotations

from typing import Any, Mapping


def project_contract(original: list[dict[str, Any]], edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match clean Word text without rendering DOCX or renumbering original anchors.

    DeliveryManager has already validated offsets, edit ordering/conflicts and
    paragraph insertion support. Replacement CRs become Word breaks (LF), while
    standalone additions use the renderer's splitlines() and remain paragraphs.
    Empty replacement paragraphs remain present, just as they do in clean Word.
    """
    by_piece: dict[str, list[dict[str, Any]]] = {}
    for edit in edits:
        by_piece.setdefault(edit["piece_id"], []).append(edit)
    if set(by_piece) - {piece["id"] for piece in original}:
        raise ValueError("已确认修改引用了无法定位的原文段落。")
    current = []
    for piece in original:
        changes = by_piece.get(piece["id"], [])
        replacements = sorted((e for e in changes if e.get("kind") != "insert_paragraph_after"),
                              key=lambda e: (e["start"], e["end"], e["id"]))
        parts, cursor = [], 0
        for edit in replacements:
            parts.extend((piece["text"][cursor:edit["start"]], edit["new_text"].replace("\r", "\n")))
            cursor = edit["end"]
        parts.append(piece["text"][cursor:])
        current.append({"id": "current:" + piece["id"], "source_piece_id": piece["id"],
                        "location": piece["location"], "text": "".join(parts),
                        "confirmed_edit_ids": [e["id"] for e in replacements]})
        for edit in changes:
            if edit.get("kind") == "insert_paragraph_after":
                for index, text in enumerate(edit["new_text"].splitlines()):
                    current.append({"id": f"inserted:{edit['id']}:{index}", "source_piece_id": None,
                                    "after_source_piece_id": piece["id"],
                                    "location": piece["location"] + " / 后新增段落",
                                    "text": text, "confirmed_edit_ids": [edit["id"]]})
    return current


def active_reviews(loaded: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude manual decisions belonging to an older accepted opinion version."""
    reviews = {}
    for finding in loaded.get("report", {}).get("findings", []):
        fid = finding["id"]
        revision = loaded.get("opinion_revisions", {}).get(fid, finding.get("opinion_revision", 0))
        stored = state.get("reviews", {}).get(fid, {})
        stored_revision = stored.get("opinion_revision", 0)
        valid = bool(stored) and stored_revision == revision
        reviews[fid] = {"decision": "未复核", "risk": finding.get("risk") or "待判断",
                        "note": "", "clause_draft": "", "opinion_revision": revision}
        if valid:
            reviews[fid].update({key: stored.get(key, reviews[fid][key])
                                 for key in ("decision", "risk", "note", "clause_draft")})
        reviews[fid].update(applies_to_current_opinion=valid, stored_opinion_revision=stored_revision if stored else None)
    return {"revision": state.get("revision", 0), "reviews": reviews}
