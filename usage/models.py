from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cache_write_input_tokens",
)


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One Codex token breakdown.

    cached_input_tokens is a subset/breakdown of input_tokens and
    reasoning_tokens is a breakdown of model output accounting. Neither value
    is added to the server-provided total_tokens.
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

    @classmethod
    def from_snake_dict(cls, value: Any) -> TokenUsage | None:
        if not isinstance(value, dict):
            return None
        fields = {name: _non_negative_int(value.get(name)) for name in TOKEN_FIELDS}
        return cls(**fields) if any(item is not None for item in fields.values()) else None

    def as_dict(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_FIELDS}


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """A Codex cumulative thread snapshot plus the latest context snapshot.

    total is the session/thread cumulative counter used for accounting.
    last is the latest active-context snapshot and is never persisted as
    processed usage by itself.
    """

    total: TokenUsage
    last: TokenUsage | None = None
    model_context_window: int | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    source: str = "thread/tokenUsage/updated"

    def total_dict(self) -> dict[str, int | None]:
        return self.total.as_dict()

    def last_dict(self) -> dict[str, int | None] | None:
        return self.last.as_dict() if self.last else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total_dict(),
            "last": self.last_dict(),
            "model_context_window": self.model_context_window,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """True newly observed usage derived field-by-field from cumulative totals."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    counter_reset: bool = False
    reset_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_FIELDS}

    def is_zero(self) -> bool:
        values = [getattr(self, name) for name in TOKEN_FIELDS]
        return all(value in (None, 0) for value in values)


def calculate_delta(previous: TokenUsage | None, current: TokenUsage) -> UsageDelta:
    """Calculate a non-negative delta independently for every cumulative field."""

    values: dict[str, int | None] = {}
    reset_fields: list[str] = []
    for name in TOKEN_FIELDS:
        now = getattr(current, name)
        before = getattr(previous, name) if previous else None
        if now is None:
            values[name] = None
        elif before is None:
            values[name] = now
        elif now >= before:
            values[name] = now - before
        else:
            values[name] = now
            reset_fields.append(name)
    return UsageDelta(
        **values,
        counter_reset=bool(reset_fields),
        reset_fields=tuple(reset_fields),
    )


def parse_usage_snapshot_event(params: Any) -> tuple[str | None, str | None, UsageSnapshot | None]:
    """Parse the current v2 thread/tokenUsage/updated notification."""

    if not isinstance(params, dict):
        return None, None, None
    thread_id = params.get("threadId") if isinstance(params.get("threadId"), str) else None
    turn_id = params.get("turnId") if isinstance(params.get("turnId"), str) else None
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return thread_id, turn_id, None
    total = TokenUsage.from_dict(token_usage.get("total"))
    last = TokenUsage.from_dict(token_usage.get("last"))
    if total is None:
        return thread_id, turn_id, None
    context_window = token_usage.get("modelContextWindow")
    if not isinstance(context_window, int) or context_window < 0:
        context_window = None
    return (
        thread_id,
        turn_id,
        UsageSnapshot(
            total=total,
            last=last,
            model_context_window=context_window,
            thread_id=thread_id,
            turn_id=turn_id,
        ),
    )


def parse_token_usage_event(params: Any) -> tuple[str | None, str | None, TokenUsage | None]:
    """Backward-compatible parser returning the latest context snapshot."""

    thread_id, turn_id, snapshot = parse_usage_snapshot_event(params)
    return thread_id, turn_id, snapshot.last if snapshot else None


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
    cache_write_input_tokens: int | None = None
    context_total_tokens: int | None = None
    model_context_window: int | None = None
    source: str = "thread/tokenUsage/updated"
    counter_reset: bool = False
