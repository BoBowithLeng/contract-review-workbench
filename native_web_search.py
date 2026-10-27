"""DeepSeek's native Anthropic-compatible server-side web search only.

This adapter never follows redirects, executes local tools, or stores raw tool
results. Returned sources are titles and HTTP(S) URLs observed in the actual
web_search_tool_result, not links invented in the assistant's answer.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from model_client import ModelClientError, _NoRedirect, output_budget


ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 17 * 1024 * 1024
TIMEOUT_SECONDS = 180
MAX_QUERIES = 20
_PARTIAL_REASONS = {"nonterminal_stop", "missing_final_text", "missing_tool_result", "unmatched_tool_result",
                    "tool_error", "continuation_failed", "request_failed"}


def sanitize_search_diagnostics(raw: Any, secret: str = "") -> dict[str, Any] | None:
    """Copy only bounded protocol counters/codes; never retain opaque blocks."""
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1
            or type(raw.get("continuation_count")) is not int or raw["continuation_count"] not in {0, 1}
            or not isinstance(raw.get("continuation_mode"), str)
            or raw["continuation_mode"] not in {"none", "resume_search", "finish_answer"}
            or type(raw.get("answer_complete")) is not bool
            or not isinstance(raw.get("stages"), list) or not 1 <= len(raw["stages"]) <= 2):
        return None
    def code(value):
        return (isinstance(value, str) and bool(re.fullmatch(r"[a-z_]{1,64}", value))
                and (not secret or secret not in value))
    def codes(value, *, reasons=False):
        if (not isinstance(value, list) or len(value) > 10 or any(not code(v) for v in value)
                or (reasons and any(v not in _PARTIAL_REASONS for v in value))):
            raise ValueError("diagnostic codes")
        return list(dict.fromkeys(value))
    def error(value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("diagnostic error")
        return value.replace(secret, "[已隐藏 API Key]") if secret else value
    try:
        result = {key: raw[key] for key in ("version", "continuation_count", "continuation_mode", "answer_complete")}
        result.update(completion_error=error(raw.get("completion_error")),
                      error_codes=codes(raw.get("error_codes")),
                      partial_reasons=codes(raw.get("partial_reasons"), reasons=True), stages=[])
        for stage in raw["stages"]:
            if (not isinstance(stage, dict) or stage.get("phase") not in {"search", "resume_search", "finish_answer"}
                    or stage.get("outcome") not in {"response", "failed"}):
                return None
            clean = {"phase": stage["phase"], "outcome": stage["outcome"]}
            if stage["outcome"] == "failed":
                clean.update(stop_reason=None, error=error(stage.get("error")))
            else:
                if not code(stage.get("stop_reason")) or type(stage.get("has_final_text")) is not bool:
                    return None
                clean.update(stop_reason=stage["stop_reason"], has_final_text=stage["has_final_text"])
                for key in ("tool_call_count", "matched_result_count", "pending_tool_count", "request_count"):
                    if type(stage.get(key)) is not int or not 0 <= stage[key] <= 100_000:
                        return None
                    clean[key] = stage[key]
                if stage.get("request_count_origin") not in {"usage", "tool_ids"}:
                    return None
                clean.update(request_count_origin=stage["request_count_origin"],
                             error_codes=codes(stage.get("error_codes")),
                             partial_reasons=codes(stage.get("partial_reasons"), reasons=True))
                if "usage" in stage:
                    if not isinstance(stage["usage"], dict):
                        return None
                    clean["usage"] = {k: v for k, v in stage["usage"].items() if k in {"input_tokens", "output_tokens"}
                                      and type(v) is int and 0 <= v <= 100_000_000}
            result["stages"].append(clean)
        if len(result["stages"]) != result["continuation_count"] + 1:
            return None
        if (result["continuation_count"] == 0) != (result["continuation_mode"] == "none"):
            return None
        if result["stages"][0]["phase"] != "search":
            return None
        if result["continuation_count"] and result["stages"][-1]["phase"] != result["continuation_mode"]:
            return None
        return result
    except (ValueError, TypeError):
        return None


def validate_deepseek_address(base_url: Any) -> None:
    """Refuse third-party origins before sending their key to DeepSeek."""
    if not isinstance(base_url, str) or len(base_url) > 2000:
        raise ValueError("联网搜索仅支持 DeepSeek 官方 API 地址。")
    try:
        parsed = urllib.parse.urlsplit(base_url.strip())
        port = parsed.port
    except ValueError:
        raise ValueError("联网搜索仅支持 DeepSeek 官方 API 地址。") from None
    if (parsed.scheme != "https" or parsed.hostname != "api.deepseek.com"
            or port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1", "/chat/completions", "/v1/chat/completions"}):
        raise ValueError("联网搜索仅支持 DeepSeek 官方 API 地址。")


def _safe_source(row: Any, secret: str) -> dict[str, str] | None:
    if not isinstance(row, dict) or row.get("type") != "web_search_result":
        return None
    title, url = row.get("title"), row.get("url")
    if not isinstance(title, str) or not isinstance(url, str):
        return None
    title, url = title.strip(), url.strip()
    if (not title or not url or len(title) > 500 or len(url) > 2048
            or secret in title or secret in url
            or any(ord(char) < 32 or ord(char) == 127 for char in title + url)):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or port == 0:
        return None
    return {"title": title, "url": url}


def _parse_response(envelope: Any, secret: str, *, tool_state: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(envelope, dict) or not isinstance(envelope.get("content"), list):
        raise ModelClientError("本次联网搜索的结果无法读取。")
    blocks = envelope["content"]
    if tool_state is None:
        tool_state = {"tool_ids": set(), "result_ids": set(), "error_codes": [], "unmatched_result": False}
    # Text announcing a search is not the answer. Keep text after the last tool.
    last_tool = max((i for i, block in enumerate(blocks) if isinstance(block, dict)
                     and block.get("type") in {"server_tool_use", "web_search_tool_result", "tool_use"}), default=-1)
    texts = [block.get("text") for block in blocks[last_tool + 1:] if isinstance(block, dict) and block.get("type") == "text"]
    text = "\n".join(item for item in texts if isinstance(item, str)).strip().replace(secret, "[已隐藏 API Key]")
    tool_ids: set[str] = set()
    queries: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "server_tool_use" or block.get("name") != "web_search":
            continue
        tool_id = block.get("id")
        if isinstance(tool_id, str) and tool_id:
            tool_ids.add(tool_id)
        query = block.get("input", {}).get("query") if isinstance(block.get("input"), dict) else None
        if (isinstance(query, str) and query.strip() and len(query) <= 500 and secret not in query
                and not any(ord(char) < 32 or ord(char) == 127 for char in query)
                and len(queries) < MAX_QUERIES):
            queries.append(query.strip())
    result_error = False
    error_codes: list[str] = []
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    tool_state["tool_ids"].update(tool_ids)
    matched_results: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        result_id = block.get("tool_use_id")
        if not isinstance(result_id, str) or result_id not in tool_state["tool_ids"]:
            tool_state["unmatched_result"] = True
            continue
        matched_results.add(result_id)
        tool_state["result_ids"].add(result_id)
        content = block.get("content")
        for item in content if isinstance(content, list) else [content]:
            if isinstance(item, dict) and item.get("type") == "web_search_tool_result_error":
                result_error = True
                code = item.get("error_code")
                known = {"too_many_requests", "invalid_tool_input", "max_uses_exceeded", "query_too_long", "request_too_large", "unavailable"}
                code = code if code in known else "unknown_tool_error"
                if code not in error_codes:
                    error_codes.append(code)
                continue
            if not isinstance(content, list):
                result_error = True
                if "invalid_tool_result" not in error_codes:
                    error_codes.append("invalid_tool_result")
                continue
            source = _safe_source(item, secret)
            if source and source["url"] not in seen_urls:
                sources.append(source)
                seen_urls.add(source["url"])
    usage = envelope.get("usage")
    server_usage = usage.get("server_tool_use") if isinstance(usage, dict) else None
    raw_count = server_usage.get("web_search_requests") if isinstance(server_usage, dict) else None
    request_count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) and 0 <= raw_count <= 100 else len(tool_ids)
    stop_reason = envelope.get("stop_reason")
    pending_tools = tool_state["tool_ids"] - tool_state["result_ids"]
    if stop_reason != "end_turn" or pending_tools or tool_state["unmatched_result"]:
        status = "partial"
    elif result_error:
        status = "partial" if sources else "error"
    elif not tool_state["tool_ids"]:
        status = "not_used"
    elif sources:
        status = "searched"
    else:
        status = "no_results"
    if not text and status not in {"partial", "error"}:
        status = "partial"
    search = {
        "status": status, "queries": queries, "sources": sources, "request_count": request_count,
    }
    if error_codes:
        search["error_codes"] = error_codes
    tool_state["error_codes"] = list(dict.fromkeys(tool_state["error_codes"] + error_codes))
    reasons = []
    if stop_reason != "end_turn":
        reasons.append("nonterminal_stop")
    if not text:
        reasons.append("missing_final_text")
    if pending_tools:
        reasons.append("missing_tool_result")
    if tool_state["unmatched_result"]:
        reasons.append("unmatched_tool_result")
    if tool_state["error_codes"]:
        reasons.append("tool_error")
    safe_stop = (stop_reason if isinstance(stop_reason, str) and re.fullmatch(r"[a-z_]{1,64}", stop_reason)
                 and secret not in stop_reason else "unknown")
    stage = {"phase": "search", "stop_reason": safe_stop, "has_final_text": bool(text),
             "tool_call_count": len(tool_ids), "matched_result_count": len(matched_results),
             "pending_tool_count": len(pending_tools), "request_count": request_count,
             "request_count_origin": "usage" if request_count == raw_count and type(raw_count) is int else "tool_ids",
             "error_codes": error_codes, "partial_reasons": reasons, "outcome": "response"}
    usage_counts = {key: usage[key] for key in ("input_tokens", "output_tokens") if isinstance(usage, dict)
                    and type(usage.get(key)) is int and 0 <= usage[key] <= 100_000_000}
    if usage_counts:
        stage["usage"] = usage_counts
    answer_complete = stop_reason == "end_turn" and bool(text)
    diagnostics = {"version": 1, "continuation_count": 0, "continuation_mode": "none",
                   "answer_complete": answer_complete, "completion_error": None,
                   "error_codes": list(tool_state["error_codes"]), "partial_reasons": reasons, "stages": [stage]}
    return {"text": text, "search": search, "answer_complete": answer_complete, "diagnostics": diagnostics}


def deepseek_search_completion(config: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Run one search-enabled request, with at most one bounded continuation.

    Transport failures are not retried. Successful responses with tool failures
    may still contain a final answer, which is preserved without another call.
    """
    if not isinstance(config, dict):
        raise ValueError("模型配置格式不正确。")
    validate_deepseek_address(config.get("base_url"))
    secret, model = config.get("api_key"), config.get("model")
    if not isinstance(secret, str) or not secret.strip() or not isinstance(model, str) or not model.strip():
        raise ValueError("API Key 或模型名称不能为空。")
    secret = secret.strip()
    if not isinstance(messages, list) or not messages:
        raise ValueError("讨论消息不能为空。")
    system: list[str] = []
    conversation: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"} or not isinstance(item.get("content"), str):
            raise ValueError("讨论消息格式不正确。")
        if item["role"] == "system":
            system.append(item["content"])
        else:
            conversation.append({"role": item["role"], "content": item["content"]})
    if not conversation:
        raise ValueError("讨论消息不能为空。")
    payload = {
        "model": model.strip(), "max_tokens": output_budget(config) or 8192,
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "system": "\n".join(system), "messages": conversation,
        # Do not impose a local search-use quota. The service may still enforce
        # its own limits; keep reporting max_uses_exceeded when it returns that.
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    try:
        envelope = _send(payload, secret)
    except ModelClientError as exc:
        message = str(exc).replace(secret, "[已隐藏 API Key]")
        exc.search_diagnostics = {"version": 1, "continuation_count": 0, "continuation_mode": "none",
            "answer_complete": False, "completion_error": message, "error_codes": [],
            "partial_reasons": ["request_failed"],
            "stages": [{"phase": "search", "outcome": "failed", "stop_reason": None, "error": message}]}
        raise
    tool_state = {"tool_ids": set(), "result_ids": set(), "error_codes": [], "unmatched_result": False}
    result = _parse_response(envelope, secret, tool_state=tool_state)
    if result["answer_complete"]:
        return result
    # One bounded continuation only. Opaque blocks stay in worker memory and go
    # back unchanged. A server pause resumes the original turn with its tools;
    # a finished search gets a separate, search-disabled answer phase.
    blocks = envelope["content"]
    has_result = any(isinstance(b, dict) and b.get("type") == "web_search_tool_result" for b in blocks)
    paused_search = envelope.get("stop_reason") == "pause_turn" and any(
        isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("name") == "web_search" for b in blocks)
    if not has_result and not paused_search:
        return result
    followup = {**payload, "messages": [*conversation, {"role": "assistant", "content": blocks}]}
    if envelope.get("stop_reason") != "pause_turn":
        followup["tool_choice"] = {"type": "none"}
        followup["messages"].append({"role": "user", "content":
            "搜索阶段已结束。不要再搜索或调用任何工具。请根据已有检索结果和原问题直接给出最终回答；"
            "部分检索失败不妨碍说明已有结论，但须区分已找到的依据与仍无法确认的内容。不要只说准备如何回答。"})
    result["search"]["answer_requests"] = 1
    diagnostics = result["diagnostics"]
    mode = "resume_search" if envelope.get("stop_reason") == "pause_turn" else "finish_answer"
    diagnostics.update(continuation_count=1, continuation_mode=mode)
    try:
        final = _parse_response(_send(followup, secret), secret, tool_state=tool_state)
    except ModelClientError as exc:
        result["completion_error"] = str(exc).replace(secret, "[已隐藏 API Key]")
        diagnostics["completion_error"] = result["completion_error"]
        diagnostics["partial_reasons"] = list(dict.fromkeys(diagnostics["partial_reasons"] + ["continuation_failed"]))
        diagnostics["stages"].append({"phase": mode, "outcome": "failed", "stop_reason": None,
                                       "error": result["completion_error"]})
        return result
    final_stage = final["diagnostics"]["stages"][0]
    final_stage["phase"] = mode
    diagnostics["stages"].append(final_stage)
    diagnostics.update(answer_complete=final["answer_complete"],
                       error_codes=list(tool_state["error_codes"]),
                       partial_reasons=final["diagnostics"]["partial_reasons"])
    # The second phase may only add actual returned evidence, never model links.
    merged = result["search"]
    seen_urls = {item["url"] for item in merged["sources"]}
    for source in final["search"]["sources"]:
        if source["url"] not in seen_urls:
            merged["sources"].append(source)
            seen_urls.add(source["url"])
    merged["queries"] = list(dict.fromkeys(merged["queries"] + final["search"]["queries"]))[:MAX_QUERIES]
    merged["request_count"] = min(100, merged["request_count"] + final["search"]["request_count"])
    codes = list(dict.fromkeys(merged.get("error_codes", []) + final["search"].get("error_codes", [])))
    if codes:
        merged["error_codes"] = codes
    if final["text"]:
        result["text"] = final["text"]
    result["answer_complete"] = final["answer_complete"]
    if result["answer_complete"]:
        unresolved = tool_state["tool_ids"] - tool_state["result_ids"] or tool_state["unmatched_result"]
        if unresolved:
            merged["status"] = "partial"
        else:
            merged["status"] = ("partial" if codes else "searched") if merged["sources"] else ("error" if codes else "no_results")
    else:
        merged["status"] = "partial"
    return result


def _send(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ModelClientError("检索上下文过大，未继续生成回答。")
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "x-api-key": secret,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "BoBo-Contract-Review/2026-09-23",
    })
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise ModelClientError(f"DeepSeek 联网搜索返回 HTTP {exc.code}，请检查 API Key。") from None
        raise ModelClientError(f"联网搜索服务返回 HTTP {exc.code}，本次搜索没有完成。") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ModelClientError("本次联网搜索连接失败，可以重试。") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelClientError("DeepSeek 联网搜索响应过大，已停止读取。")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelClientError("本次联网搜索的返回内容无法解析。") from None
    if not isinstance(envelope, dict):
        raise ModelClientError("本次联网搜索的返回内容不完整。")
    return envelope


__all__ = ["deepseek_search_completion", "validate_deepseek_address", "sanitize_search_diagnostics"]
