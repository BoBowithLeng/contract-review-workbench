"""File-based tool handoff: host AI reviews the same request the API would get.

prepare -> actual host-model invocation -> complete. No canned model response.
This is a local command tool, not an installed MCP server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from contract_core import ParsedContract, _ai_prompt, review_ai
from contract_core import parse_docx, parse_text
from review_engine import SCHEMA_VERSION, build_messages, review_detailed
from document_view import docx_headings

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def hash_request(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare(input_path, output_path, *, region="上海", review_date="2026-09-20", payday=None,
            business_goal=None, detailed=False, sources=None):
    path = Path(input_path)
    contract = parse_docx(path.read_bytes()) if path.suffix.lower() == ".docx" else parse_text(path.read_text(encoding="utf-8"))
    context = {"region": region, "review_date": review_date, "input_kind": "模板审阅", "payday": payday}
    if business_goal is not None:
        if not isinstance(business_goal, str) or len(business_goal) > 4000:
            raise ValueError("公司目标应为不超过4000字的文本。")
        context["business_goal"] = business_goal
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        raise ValueError("sources 必须是显式提供的数组。")
    messages = build_messages(contract, context, sources) if detailed else _ai_prompt(contract, context, sources)
    packet = {"contract": contract.model_dump(), "context": context, "sources": sources,
              "messages": messages, "request_digest": hash_request(messages)}
    if detailed:
        packet["schema_version"] = SCHEMA_VERSION
        packet["document_name"] = path.name
        packet["headings"] = docx_headings(path.read_bytes()) if path.suffix.lower() == '.docx' else {}
    # New run directories/files only: never overwrite an earlier model trace.
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=2)
    return packet


def load_review(packet_path, response_path, *, model):
    packet = read_json(packet_path)
    response = read_json(response_path)
    contract = ParsedContract.model_validate(packet["contract"])
    # The packet's source snapshot is part of the signed request.  Never swap
    # current sources into an old model trace merely because sources.json grew.
    sources = packet.get("sources")
    if not isinstance(sources, list):
        raise ValueError("工具请求缺少当次法源快照。")
    packet_version = packet.get("schema_version", 1)
    if packet_version == SCHEMA_VERSION:
        messages = build_messages(contract, packet["context"], sources)
    elif packet_version in (None, 1):
        messages = _ai_prompt(contract, packet["context"], sources)
    else:
        raise ValueError("不支持的工具请求 schema_version。")
    expected = hash_request(messages)
    if expected != packet["request_digest"] or expected != response.get("request_digest") or expected != hash_request(packet.get("messages")):
        raise ValueError("合同、提示、资料或回传编号已变化，不能把旧回复套进新请求。")
    if response.get("model") != model:
        raise ValueError("回传模型与这次记录的模型不一致。")
    raw = json.dumps(response["result"], ensure_ascii=False)

    def host_response(config, actual_messages):
        if hash_request(actual_messages) != expected:
            raise ValueError("实际审核请求与模型读到的请求不一致。")
        return raw

    if packet_version == SCHEMA_VERSION:
        report = review_detailed(contract, packet["context"], sources, {"model": model}, completion=host_response)
    else:
        report = review_ai(contract, packet["context"], sources, {"model": model}, completion=host_response)
    report["mode"] = "host_ai"
    report["metadata"].update({"execution_route": "host_tool_handoff", "request_digest": expected,
        "api_connection_tested": False,
        "source_snapshot_current": None,
        "provenance_note": "宿主模型读取工具请求后回传；模型名由执行记录提供，不是供应商签名证明。"})
    bundle = {"contract": contract.model_dump(), "report": report, "sources": sources,
              "packet_file": str(Path(packet_path).resolve()), "response_file": str(Path(response_path).resolve())}
    if packet.get('headings'):
        bundle['contract']['headings'] = packet['headings']
    report['document_name'] = packet.get('document_name', '劳动合同模板')
    return bundle


def complete(packet_path, response_path, output_path, *, model):
    bundle = load_review(packet_path, response_path, model=model)
    with Path(output_path).open("x", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    return bundle


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--payday", type=int)
    p.add_argument("--goal-file")
    p.add_argument("--detailed", action="store_true")
    p = sub.add_parser("complete")
    p.add_argument("packet")
    p.add_argument("response")
    p.add_argument("output")
    p.add_argument("--model", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        goal = Path(args.goal_file).read_text(encoding="utf-8") if args.goal_file else None
        result = prepare(args.input, args.output, payday=args.payday, business_goal=goal, detailed=args.detailed)
        print(json.dumps({"request_digest": result["request_digest"], "pieces": len(result["contract"]["pieces"]),
                          "schema_version": result.get("schema_version", 1)}))
    else:
        result = complete(args.packet, args.response, args.output, model=args.model)
        print(json.dumps({"findings": [{"rule": f["rule_id"], "risk": f["risk"], "status": f["status"]} for f in result["report"]["findings"]]}, ensure_ascii=False))
