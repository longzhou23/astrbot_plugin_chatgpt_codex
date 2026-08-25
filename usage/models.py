from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One server-reported token breakdown.

    The names intentionally mirror the current Codex App Server JSON schema.
    ``cached_input_tokens`` is a breakdown of input accounting, not an extra
    quantity to add to ``total_tokens``.  ``total_tokens`` is always the
    server's authoritative value when it is present.
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_write_input_tokens: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> TokenUsage | None:
        if not isinstance(value, dict):
            return None
        fields = {
            "input_tokens": _non_negative_int(value.get("inputTokens")),
            "cached_input_tokens": _non_negative_int(value.get("cachedInputTokens")),
            "output_tokens": _non_negative_int(value.get("outputTokens")),
            "reasoning_tokens": _non_negative_int(value.get("reasoningOutputTokens")),
            "total_tokens": _non_negative_int(value.get("totalTokens")),
            "cache_write_input_tokens": _non_negative_int(value.get("cacheWriteInputTokens")),
        }
        return cls(**fields) if any(item is not None for item in fields.values()) else None

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
class UsageRecord:
    timestamp: int
    local_date: str
    conversation_hash: str | None
    thread_id: str | None
    turn_id: str | None
    model: str | None
    reasoning_effort: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    request_count: int = 1


def parse_token_usage_event(params: Any) -> tuple[str | None, str | None, TokenUsage | None]:
    """Parse the current ``thread/tokenUsage/updated`` notification.

    The protocol places the per-turn snapshot under ``tokenUsage.last`` and
    includes ``threadId`` and ``turnId`` at the notification level.  ``total``
    is the thread lifetime snapshot, so it must not be written as a second
    request record.
    """

    if not isinstance(params, dict):
        return None, None, None
    thread_id = params.get("threadId") if isinstance(params.get("threadId"), str) else None
    turn_id = params.get("turnId") if isinstance(params.get("turnId"), str) else None
    token_usage = params.get("tokenUsage")
    last = token_usage.get("last") if isinstance(token_usage, dict) else None
    return thread_id, turn_id, TokenUsage.from_dict(last)

