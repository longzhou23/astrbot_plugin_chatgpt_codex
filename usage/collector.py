from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import TokenUsage

if TYPE_CHECKING:
    from .service import UsageService


class UsageCollector:
    """Turn-completion side channel kept separate from response rendering."""

    def __init__(self, service: UsageService) -> None:
        self.service = service

    async def record_turn_usage(
        self,
        *,
        conversation_id: str | None,
        thread_id: str | None,
        turn_id: str | None,
        model: str | None,
        reasoning_effort: str | None,
        usage: TokenUsage | dict[str, Any] | None,
        timestamp: int | None = None,
    ) -> bool:
        """Delegate persistence while keeping collection a replaceable boundary."""

        return await self.service.record_turn_usage(
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            model=model,
            reasoning_effort=reasoning_effort,
            usage=usage,
            timestamp=timestamp,
        )

