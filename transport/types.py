from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class TransportError(Exception):
    """Base error for the direct Responses transport."""

    retryable = False


class TransportAuthError(TransportError):
    """The local ChatGPT OAuth material is absent, expired or rejected."""


class TransportModelError(TransportError):
    """The requested model is unavailable or the models endpoint changed."""


class TransportProtocolError(TransportError):
    """The server response is not a supported Responses stream."""


class TransportQuotaError(TransportError):
    """The account is rate limited or out of Codex allowance."""


class TransportNetworkError(TransportError):
    retryable = True


class TransportModeError(TransportError):
    """The selected mode cannot be used in the current environment."""


@dataclass(frozen=True, slots=True)
class TransportUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_write_input_tokens: int | None = None

    @classmethod
    def from_response(cls, value: Any) -> TransportUsage | None:
        if not isinstance(value, dict):
            return None
        details = value.get("input_tokens_details") or value.get("inputTokensDetails") or {}
        output_details = value.get("output_tokens_details") or value.get("outputTokensDetails") or {}

        def number(*keys: str) -> int | None:
            for key in keys:
                item = value.get(key)
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    return item
            return None

        def nested_number(source: Any, *keys: str) -> int | None:
            if not isinstance(source, dict):
                return None
            for key in keys:
                item = source.get(key)
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    return item
            return None

        result = cls(
            input_tokens=number("input_tokens", "inputTokens"),
            cached_input_tokens=nested_number(details, "cached_tokens", "cachedTokens"),
            output_tokens=number("output_tokens", "outputTokens"),
            reasoning_tokens=nested_number(output_details, "reasoning_tokens", "reasoningTokens"),
            total_tokens=number("total_tokens", "totalTokens"),
            cache_write_input_tokens=nested_number(
                details, "cache_write_tokens", "cacheWriteTokens"
            ),
        )
        return result if any(value is not None for value in result.as_dict().values()) else None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
        }


@dataclass(frozen=True, slots=True)
class TransportToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(slots=True)
class TransportResponse:
    text: str = ""
    response_id: str | None = None
    usage: TransportUsage | None = None
    tool_calls: list[TransportToolCall] = field(default_factory=list)
    rate_limits: dict[str, Any] = field(default_factory=dict)
    event_count: int = 0
