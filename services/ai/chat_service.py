from __future__ import annotations

import json
import math
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit


DEFAULT_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_LOCAL_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_LOCAL_MODEL = "gpt-oss-20b"
DEFAULT_UNKNOWN_CONTEXT_WINDOW = 128_000
MAX_CONTEXT_WINDOW = 1_000_000
KNOWN_MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
}


@dataclass(frozen=True)
class AiModelThinkingConfig:
    model: str
    modes: tuple[str, ...]
    default_mode: str
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str = ""


@dataclass(frozen=True)
class AiProviderConfig:
    id: str
    model_setting: str
    context_setting: str
    default_model: str
    default_endpoint: str
    endpoint_setting: str | None = None
    available_models: tuple[str, ...] = ()
    editable_model: bool = False
    requires_api_key: bool = False
    api_key_environment_variable: str = ""
    loopback_only: bool = False
    request_timeout_ms: int = 180_000
    disable_thinking_for_compaction: bool = False
    thinking_mode_setting: str | None = None
    reasoning_effort_setting: str | None = None
    model_thinking_configs: tuple[AiModelThinkingConfig, ...] = ()
    tool_choice: str | None = "auto"


DEEPSEEK_PROVIDER = AiProviderConfig(
    id="deepseek",
    model_setting="deepseek_model",
    context_setting="deepseek_context_window_tokens",
    default_model=DEFAULT_DEEPSEEK_MODEL,
    default_endpoint=DEFAULT_DEEPSEEK_ENDPOINT,
    available_models=tuple(KNOWN_MODEL_CONTEXT_WINDOWS),
    requires_api_key=True,
    api_key_environment_variable="DEEPSEEK_API_KEY",
    disable_thinking_for_compaction=True,
    thinking_mode_setting="deepseek_thinking_mode",
    reasoning_effort_setting="deepseek_reasoning_effort",
    model_thinking_configs=tuple(
        AiModelThinkingConfig(
            model=model,
            modes=("enabled", "disabled"),
            default_mode="enabled",
            reasoning_efforts=("high", "max"),
            default_reasoning_effort="high",
        )
        for model in KNOWN_MODEL_CONTEXT_WINDOWS
    ),
    tool_choice=None,
)
LOCAL_PROVIDER = AiProviderConfig(
    id="local",
    model_setting="local_ai_model",
    context_setting="local_ai_context_window_tokens",
    default_model=DEFAULT_LOCAL_MODEL,
    default_endpoint=DEFAULT_LOCAL_ENDPOINT,
    endpoint_setting="local_ai_endpoint",
    editable_model=True,
    loopback_only=True,
    request_timeout_ms=600_000,
)
AI_PROVIDER_CONFIGS = {
    config.id: config
    for config in (DEEPSEEK_PROVIDER, LOCAL_PROVIDER)
}


def get_ai_provider_config(provider: Any) -> AiProviderConfig:

    if isinstance(provider, AiProviderConfig):
        return provider
    return AI_PROVIDER_CONFIGS.get(str(provider or ""), DEEPSEEK_PROVIDER)


def thinking_config_for_model(
    provider: Any,
    model: str,
) -> AiModelThinkingConfig | None:

    config = get_ai_provider_config(provider)
    return next(
        (
            thinking
            for thinking in config.model_thinking_configs
            if thinking.model == model
        ),
        None,
    )


class ChatProtocolError(ValueError):
    """Raised when a chat-completions server returns an unsafe response."""


def context_window_for_model(model: str) -> int:

    normalized = str(model or "").casefold().replace("_", "-")
    if "gpt-oss-20b" in normalized:
        return 131_072
    return KNOWN_MODEL_CONTEXT_WINDOWS.get(
        model,
        DEFAULT_UNKNOWN_CONTEXT_WINDOW,
    )


def normalize_context_window(value: Any) -> int:

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return min(MAX_CONTEXT_WINDOW, max(0, parsed))


@dataclass(frozen=True)
class ChatToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatMessage:
    content: str
    reasoning_content: str
    tool_calls: tuple[ChatToolCall, ...]
    api_message: dict[str, Any]
    finish_reason: str = ""
    prompt_tokens: int = 0
    total_tokens: int = 0


def is_loopback_chat_endpoint(endpoint: str) -> bool:

    try:
        parsed = urlsplit(str(endpoint or "").strip())
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.port == 0:
            return False
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def estimate_chat_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Conservatively estimate tokens for compaction preflight.

    Provider tokenizers are not bundled. UTF-8 bytes divided by three is
    intentionally conservative for typical English, paths, and JSON-heavy MDF
    tool traffic. Server-reported usage remains authoritative afterward.
    """

    serialized = json.dumps(
        {"messages": messages, "tools": tools or []},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, math.ceil(len(serialized) / 3))


def build_chat_payload(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str,
    tool_choice: str | None = "auto",
) -> dict[str, Any]:
    """Build the request body while keeping credentials out of serializable state."""
    model = str(model or "").strip()
    if not model:
        raise ValueError("A chat model is required.")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    return payload


def _parse_tool_calls(
    message: dict[str, Any],
) -> tuple[tuple[ChatToolCall, ...], list[dict[str, Any]]]:
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ChatProtocolError("The AI server returned invalid tool calls.")

    calls = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise ChatProtocolError(
                "The AI server returned an invalid tool call."
            )
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise ChatProtocolError(
                "The AI server returned a tool call without a function."
            )
        call_id = raw_call.get("id")
        name = function.get("name")
        arguments = function.get("arguments", "{}")
        if not isinstance(call_id, str) or not call_id:
            raise ChatProtocolError(
                "The AI server returned a tool call without an id."
            )
        if not isinstance(name, str) or not name:
            raise ChatProtocolError(
                "The AI server returned a tool call without a name."
            )
        if not isinstance(arguments, str):
            raise ChatProtocolError(
                "The AI server returned non-text tool arguments."
            )
        calls.append(ChatToolCall(call_id, name, arguments))
    return tuple(calls), raw_calls


def _usage_token_count(usage: Any, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0


def parse_chat_response(payload: Any) -> ChatMessage:
    """Validate and normalize one chat-completions response."""
    if not isinstance(payload, dict):
        raise ChatProtocolError(
            "The AI server returned a non-object response."
        )

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise ChatProtocolError(str(error["message"]))
        raise ChatProtocolError(
            "The AI server returned no response choices."
        )

    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        raise ChatProtocolError(
            "The AI server returned an invalid assistant message."
        )
    finish_reason_value = first.get("finish_reason")
    finish_reason = (
        finish_reason_value if isinstance(finish_reason_value, str) else ""
    )

    content_value = message.get("content")
    content = content_value if isinstance(content_value, str) else ""
    reasoning_value = message.get("reasoning_content")
    reasoning_content = reasoning_value if isinstance(reasoning_value, str) else ""

    normalized_calls, raw_calls = _parse_tool_calls(message)

    api_message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if raw_calls and isinstance(reasoning_value, str):
        api_message["reasoning_content"] = reasoning_content
    if raw_calls:
        api_message["tool_calls"] = raw_calls

    if not content and not normalized_calls:
        raise ChatProtocolError(
            "The AI server returned an empty assistant message."
        )

    usage = payload.get("usage")
    prompt_tokens = _usage_token_count(usage, "prompt_tokens")
    total_tokens = _usage_token_count(usage, "total_tokens")

    return ChatMessage(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=normalized_calls,
        api_message=api_message,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
    )
