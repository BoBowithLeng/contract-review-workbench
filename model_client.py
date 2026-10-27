"""Small, provider-neutral compatible API client.

Only the ``/chat/completions`` request and ``/models`` catalog formats are supported.
The module performs no network request at import time.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 120
MAX_MODEL_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MODEL_ROWS = 2500
MODEL_LIST_TIMEOUT_SECONDS = 15
# A full review with drafts can outgrow a provider's default output budget; in
# DeepSeek's thinking mode that default is 64K tokens including the thinking.
# Only providers with a documented, larger ceiling get an explicit budget
# (DeepSeek allows 384K); others keep their own default rather than risk a 400.
OUTPUT_BUDGETS = {"api.deepseek.com": 131072}


def output_budget(config: dict[str, Any]) -> int | None:
    """max_tokens sent for this config: the explicit value, a preset budget, or None."""
    if type(config.get("max_tokens")) is int:
        return config["max_tokens"]
    try:
        host = urllib.parse.urlsplit(str(config.get("base_url") or "").strip()).hostname
    except ValueError:
        return None
    return OUTPUT_BUDGETS.get(host or "")


class ModelClientError(RuntimeError):
    """A safe-to-display provider or protocol error."""

    def __init__(self, message: str, *, code: str = "client_error", http_status: int | None = None,
                 finish_reason: str | None = None, usage: dict | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.finish_reason = finish_reason if isinstance(finish_reason, str) and finish_reason in {"stop", "length", "max_tokens", "content_filter", "tool_calls"} else None
        self.usage = {k: v for k, v in (usage or {}).items()
                      if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                      and type(v) is int and v >= 0} if isinstance(usage, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("模型提供方地址不能为空。")
    if len(base_url) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in base_url):
        raise ValueError("模型提供方地址包含无效字符或过长。")
    try:
        parsed = urllib.parse.urlsplit(base_url.strip())
        port = parsed.port
    except ValueError:
        raise ValueError("模型提供方地址格式不正确。") from None
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("模型提供方地址仅支持 HTTPS；本机回环地址可使用 HTTP。")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ValueError("非本机模型提供方必须使用 HTTPS。")
    if parsed.username or parsed.password:
        raise ValueError("模型提供方地址不能包含用户名或密码。")
    if parsed.query or parsed.fragment:
        raise ValueError("模型提供方地址不能包含查询参数或片段。")
    if not parsed.hostname:
        raise ValueError("模型提供方地址缺少主机名。")
    if port is not None and port == 0:
        raise ValueError("模型提供方端口无效。")

    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _models_endpoint(base_url: str) -> str:
    """Use the same validated origin and API prefix as Chat Completions."""
    chat_url = _endpoint(base_url)
    parsed = urllib.parse.urlsplit(chat_url)
    prefix = parsed.path[: -len("/chat/completions")]
    if prefix.endswith("/models"):
        path = prefix
    else:
        path = prefix + "/models"
    query = ""
    # SiliconFlow's catalog otherwise mixes chat, embedding and media models.
    if parsed.hostname in {"api.siliconflow.cn", "api-st.siliconflow.cn"}:
        query = "sub_type=chat"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def list_models(config: dict[str, Any]) -> list[dict[str, str]]:
    """Fetch a compatible provider's model catalog, without testing inference.

    Only IDs and display names are returned. The key is sent as a bearer header
    to the validated endpoint, never persisted, logged, or included in errors.
    """
    if not isinstance(config, dict):
        raise ValueError("模型配置格式不正确。")
    endpoint = _models_endpoint(config.get("base_url", ""))
    api_key = config.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("API Key 不能为空。")
    secret = api_key.strip()
    if len(secret) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in secret):
        raise ValueError("API Key 格式不正确。")
    try:
        request = urllib.request.Request(
            endpoint,
            method="GET",
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": "application/json",
                "User-Agent": "BoBo-Contract-Review/2026-09-20",
            },
        )
    except ValueError:
        raise ModelClientError("无法从这个地址获取模型列表，请检查服务地址，或手动填写模型名称。") from None
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=MODEL_LIST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise ModelClientError(f"暂时无法获取模型列表（HTTP {exc.code}），请检查 API Key 和使用权限。") from None
        raise ModelClientError(f"暂时无法获取模型列表（HTTP {exc.code}），可以手动填写模型名称。") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = "连接超时" if isinstance(exc, TimeoutError) else "连接失败"
        raise ModelClientError(f"获取模型列表时{reason}，可以重新获取或手动填写模型名称。") from None

    if len(raw) > MAX_MODEL_RESPONSE_BYTES:
        raise ModelClientError("模型列表太长，暂时无法读取，可以手动填写模型名称。")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelClientError("收到的模型列表无法读取，可以重新获取或手动填写模型名称。") from None
    rows = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(rows, list):
        raise ModelClientError("收到的内容无法识别为模型列表，可以手动填写模型名称。")
    if len(rows) > MAX_MODEL_ROWS:
        raise ModelClientError("模型列表条目太多，暂时无法展示，可以手动填写模型名称。")
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id")
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in identifier)):
            continue
        identifier = identifier.strip()
        if secret in identifier or identifier in seen:
            continue
        name = row.get("name")
        if (not isinstance(name, str) or not name.strip() or len(name) > 256 or secret in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            name = identifier
        models.append({"id": identifier, "name": name.strip()})
        seen.add(identifier)
    if rows and not models:
        raise ModelClientError("列表里没有可选择的模型，可以手动填写模型名称。")
    return models


def chat_completion(config: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """Return the first normal text response from a compatible provider.

    ``config`` must contain ``base_url``, ``api_key`` and ``model``.  Redirects
    are deliberately rejected so the Authorization header cannot be forwarded
    to another origin.  No automatic retry is performed.
    """

    if not isinstance(config, dict):
        raise ValueError("模型配置格式不正确。")
    endpoint = _endpoint(config.get("base_url", ""))
    api_key = config.get("api_key")
    model = config.get("model")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("API Key 不能为空。")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("模型名称不能为空。")
    if not isinstance(messages, list) or not messages:
        raise ValueError("消息列表不能为空。")

    payload: dict[str, Any] = {"model": model.strip(), "messages": messages}
    for key in ("temperature", "max_tokens"):
        if key in config:
            payload[key] = config[key]
    if "max_tokens" not in payload and output_budget(config):
        payload["max_tokens"] = output_budget(config)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BoBo-Contract-Review/2026-09-20",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Never include response bodies: providers sometimes echo request data.
        if exc.code in {401, 403}:
            message = f"模型服务未接受当前连接配置（HTTP {exc.code}），请在模型设置中检查密钥和使用权限。"
        elif exc.code == 429:
            message = "模型服务暂时限制了请求频率或用量（HTTP 429），可以稍后重试或更换模型。"
        elif 500 <= exc.code <= 599:
            message = f"模型服务暂时未能完成请求（HTTP {exc.code}），可以重新尝试。"
        else:
            message = f"这次请求未被模型服务接受（HTTP {exc.code}）。"
        raise ModelClientError(message, code="http_error", http_status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
        raise ModelClientError("等待回复超时，可以重新尝试。" if timed_out else
                               "这次未能连接到模型服务，可以检查网络后重新尝试。",
                               code="timeout" if timed_out else "connection_error") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelClientError("这次回复超过可接收的长度，未能完整读取。", code="response_too_large")
    try:
        envelope = json.loads(raw.decode("utf-8"))
        choice = envelope["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason")
        usage = envelope.get("usage")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        raise ModelClientError("收到的回复格式无法读取，可以重新尝试或更换模型。", code="invalid_response") from None
    if isinstance(finish_reason, str) and finish_reason in {"length", "max_tokens"}:
        raise ModelClientError("这次回复达到输出长度上限，内容没有完整返回。",
                               code="output_limit", finish_reason=finish_reason, usage=usage)
    if not isinstance(content, str) or not content.strip():
        raise ModelClientError("这次没有收到可显示的回复，可以重新尝试或更换模型。",
                               code="empty_response", finish_reason=finish_reason, usage=usage)
    return content


__all__ = ["ModelClientError", "chat_completion", "list_models"]
