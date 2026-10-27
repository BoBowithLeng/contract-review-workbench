"""Synthetic source metadata for legacy validation and saved-trace tests only.

These records are not legal material. IDs deliberately match historical packet
keys so replay compatibility is tested without retaining the retired library.
"""
from __future__ import annotations

import json
from pathlib import Path


def _record(source_id, checked_at, *, region="全国", effective_from="2025-01-01", effective_to=None):
    return {
        "id": source_id, "title": f"合成测试资料 {source_id}",
        "authority": "合成测试机关", "url": f"https://example.test/synthetic/{source_id}",
        "article": "合成测试条目", "text": f"合成测试文本 {source_id}；仅用于验证，不是法律依据。",
        "region": region, "effective_from": effective_from, "effective_to": effective_to,
        "checked_at": checked_at, "verified": True,
        "version_status": "合成测试版本", "notes": "测试夹具，不是真实法源。",
    }


def labor_sources(checked_at="2026-09-20"):
    ids = ["LCL14", "LCL23_24", "LCL30_50", "SPC10_11", "SPC13_14", "SHW7", "NATIONALW9", "LCL17"]
    return [_record(sid, checked_at, region="上海" if sid == "SHW7" else "全国",
                    effective_from="2026-08-01" if sid == "SHW7" else "2025-01-01",
                    effective_to="2031-07-31" if sid == "SHW7" else None) for sid in ids]


def service_sources(checked_at="2026-09-22"):
    records = [_record(sid, checked_at) for sid in
               ("CC496_497", "CC506", "CC509_511", "CC543_544", "CC562_566", "CC577_579", "CC584_585")]
    # The historical service validator requires this host prefix. These fake,
    # nonfetching paths only exercise that whitelist, not actual legal material.
    for record in records:
        # Exact historical validator label; all substance remains synthetic.
        record["title"] = "中华人民共和国民法典"
        record["url"] = f"https://www.spp.gov.cn/TEST-ONLY-NOT-A-LEGAL-SOURCE/{record['id']}"
    return records


def write_sources(path, records=None):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(labor_sources() if records is None else records, ensure_ascii=False), encoding="utf-8")
    return target
