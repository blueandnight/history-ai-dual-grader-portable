"""HTTP response parsing only; keys, prompts and formal gates remain in app."""
from __future__ import annotations
import json
import math
import re
import ipaddress
from typing import Any
from urllib.request import HTTPRedirectHandler,build_opener,Request
from urllib.parse import urlparse


class NoRedirectHandler(HTTPRedirectHandler):
    """Do not forward an API Key to a different Origin via HTTP 30x."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("模型接口返回重定向，已拒绝继续发送 Key；请直接填写最终 HTTPS 接口地址")


class ModelProviderResponseError(ValueError):
    """A safe marker for a successful HTTP response that cannot be graded."""

    def __init__(self, stage: str, message: str = "模型服务商返回了不可解析的内容"):
        super().__init__(message)
        self.stage = stage


def read_model_chat_content(
    response,
    diagnostics: dict[str, Any] | None = None,
    raw_capture: dict[str, Any] | None = None,
) -> str:
    """Read an OpenAI-compatible response without retaining provider content."""
    raw_bytes = response.read()
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ModelProviderResponseError("body_decode") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ModelProviderResponseError("body_json") from exc
    if not isinstance(raw, dict):
        raise ModelProviderResponseError("top_level")
    if diagnostics is not None:
        response_id = raw.get("id") or raw.get("request_id")
        if isinstance(response_id, str) and response_id:
            diagnostics["provider_response_id"] = response_id[:200]
        response_status = raw.get("status")
        if isinstance(response_status, (str, int, float)):
            diagnostics["provider_response_status"] = str(response_status)[:80]
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                header_request_id = headers.get("x-request-id") or headers.get("request-id")
            except AttributeError:
                header_request_id = None
            if isinstance(header_request_id, str) and header_request_id:
                diagnostics.setdefault("provider_response_id", header_request_id[:200])
        http_status = getattr(response, "status", None)
        if http_status is None and hasattr(response, "getcode"):
            try:
                http_status = response.getcode()
            except Exception:
                http_status = None
        if isinstance(http_status, (int, float)):
            diagnostics["http_status"] = int(http_status)
        provider_error = raw.get("error")
        if isinstance(provider_error, dict):
            safe_provider_error = {}
            for key in ("type", "code"):
                value = provider_error.get(key)
                if isinstance(value, (str, int, float)):
                    safe_provider_error[key] = str(value)[:120]
            if safe_provider_error:
                diagnostics["provider_error"] = safe_provider_error
    usage = raw.get("usage")
    if diagnostics is not None and isinstance(usage, dict):
        normalized_usage = {}
        for target, candidates in {
            "input_tokens": ("input_tokens", "prompt_tokens"),
            "output_tokens": ("output_tokens", "completion_tokens"),
            "total_tokens": ("total_tokens",),
        }.items():
            for candidate in candidates:
                value = usage.get(candidate)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    normalized_usage[target] = int(value)
                    break
        details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
        reasoning_tokens = usage.get("reasoning_tokens")
        if not isinstance(reasoning_tokens, (int, float)) and isinstance(details, dict):
            reasoning_tokens = details.get("reasoning_tokens")
        if isinstance(reasoning_tokens, (int, float)) and math.isfinite(reasoning_tokens) and reasoning_tokens >= 0:
            normalized_usage["reasoning_tokens"] = int(reasoning_tokens)
        if normalized_usage:
            # Preserve legacy names consumed by existing UI and run summaries.
            if "input_tokens" in normalized_usage:
                normalized_usage["prompt_tokens"] = normalized_usage["input_tokens"]
            if "output_tokens" in normalized_usage:
                normalized_usage["completion_tokens"] = normalized_usage["output_tokens"]
            diagnostics["token_usage"] = normalized_usage
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelProviderResponseError("choices")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if diagnostics is not None and finish_reason:
        diagnostics["finish_reason"] = str(finish_reason)[:40]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelProviderResponseError("message")
    content = message.get("content")
    if raw_capture is not None:
        raw_capture["assistant_content_original"] = content
    if isinstance(content, str):
        if content.strip():
            if raw_capture is not None:
                raw_capture["parser_input"] = content
            return content
        raise ModelProviderResponseError("message_content_empty")
    if isinstance(content, dict):
        combined = json.dumps(content, ensure_ascii=False)
        if raw_capture is not None:
            raw_capture["parser_input"] = combined
        return combined
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(text, dict):
                value = text.get("value")
                if isinstance(value, str):
                    parts.append(value)
        combined = "".join(parts)
        if combined.strip():
            if raw_capture is not None:
                raw_capture["parser_input"] = combined
            return combined
        raise ModelProviderResponseError("message_content_empty")
    raise ModelProviderResponseError("message_content_type")


def _balanced_json_object(text: str) -> str | None:
    """Find the first balanced JSON object, respecting braces inside strings."""
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json(text: str) -> dict[str, Any]:
    text = str(text or "").strip().lstrip("\ufeff")
    if not text:
        raise ValueError("模型返回内容为空")
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            balanced = _balanced_json_object(candidate)
            if not balanced or balanced == candidate:
                continue
            try:
                value = json.loads(balanced)
            except json.JSONDecodeError:
                continue
        if not isinstance(value, dict):
            raise ValueError("模型返回格式不是对象")
        return value
    raise ValueError("模型没有返回有效 JSON")


def open_model_request(request: Request, timeout: float, *, opener_factory=build_opener, redirect_handler=NoRedirectHandler):
    return opener_factory(redirect_handler).open(request, timeout=timeout)



def is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().strip("[]").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_model_base_url(value: str) -> tuple[str, str]:
    """Validate an API URL and return (normalized_url, origin).

    Non-loopback HTTP is deliberately rejected: the temporary mobile token is
    not encryption, and a model Key must not be sent over an untrusted LAN.
    """
    base_url = str(value or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口地址必须是 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise ValueError("接口地址不能包含用户名或密码")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname or ""):
        raise ValueError("非本机接口地址必须使用 HTTPS；本机回环地址才允许 HTTP")
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return base_url, origin


def model_chat_completions_endpoint(model_config: dict[str, str], *, validate_model_base_url) -> str:
    base_url, _ = validate_model_base_url(str(model_config.get("base_url", "")))
    return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
