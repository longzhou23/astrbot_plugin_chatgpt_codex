from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .aggregate import aggregate_by, aggregate_rows, heat_level
from .collector import UsageCollector
from .models import (
    TokenUsage,
    UsageRecord,
    UsageSnapshot,
)
from .storage import UsageStorage


class UsageService:
    """Shared usage service with cumulative-snapshot accounting."""

    def __init__(
        self,
        db_path: Path,
        *,
        timezone_name: str = "Asia/Shanghai",
        retention_days: int = 365,
        debug_enabled: bool = False,
    ) -> None:
        try:
            self.zone = ZoneInfo(timezone_name)
            self.timezone_name = timezone_name
        except Exception:
            self.zone = ZoneInfo("Asia/Shanghai")
            self.timezone_name = "Asia/Shanghai"
        self.retention_days = max(0, int(retention_days))
        self.storage = UsageStorage(db_path)
        self.collector = UsageCollector(self)
        self._initialized = False
        self._last_cleanup = 0.0
        self.debug_enabled = bool(debug_enabled)

    async def initialize(self) -> None:
        if not self._initialized:
            await self.storage.initialize()
            self._initialized = True
        if self.retention_days and time.time() - self._last_cleanup >= 86400:
            await self.storage.cleanup(int(time.time()) - self.retention_days * 86400)
            self._last_cleanup = time.time()

    def update_config(self, *, timezone_name: str, retention_days: int, debug_enabled: bool) -> None:
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        try:
            self.zone = ZoneInfo(self.timezone_name)
        except Exception:
            self.timezone_name = "Asia/Shanghai"
            self.zone = ZoneInfo(self.timezone_name)
        self.retention_days = max(0, int(retention_days or 0))
        self.debug_enabled = bool(debug_enabled)

    def _local_date(self, timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(self.zone).date().isoformat()

    def _today(self) -> date:
        return datetime.now(timezone.utc).astimezone(self.zone).date()

    @staticmethod
    def _hash_conversation(value: str | None) -> str | None:
        if not value:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _mask_identifier(value: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        return value[:4] + "…" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

    @classmethod
    def _mask_debug_ids(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._mask_debug_ids(item) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._mask_identifier(item)
                if key in {"thread_id", "turn_id"}
                else cls._mask_debug_ids(item)
                for key, item in value.items()
            }
        return value

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
        """Compatibility path for already-delta usage supplied by callers/tests."""

        await self.initialize()
        now = int(timestamp if timestamp is not None else time.time())
        if isinstance(usage, dict):
            raw_usage = usage.get("last", usage)
            # App Server events use camelCase; Responses Transport usage is
            # already normalized to snake_case.  Accept both at this boundary
            # so direct transport responses are persisted as real turns.
            usage = TokenUsage.from_snake_dict(raw_usage) or TokenUsage.from_dict(raw_usage)
        values = usage.as_dict() if isinstance(usage, TokenUsage) else {}
        # Do not turn a completed request without a usage payload into an
        # apparently valid, all-null Usage row.  The response itself remains
        # successful; only the local statistics record is skipped.
        if not any(values.get(field) is not None for field in TokenUsage.__dataclass_fields__):
            return False
        record = UsageRecord(
            timestamp=now,
            local_date=self._local_date(now),
            conversation_hash=self._hash_conversation(conversation_id),
            thread_id=thread_id,
            turn_id=turn_id,
            model=model,
            reasoning_effort=reasoning_effort,
            input_tokens=values.get("input_tokens"),
            cached_input_tokens=values.get("cached_input_tokens"),
            output_tokens=values.get("output_tokens"),
            reasoning_tokens=values.get("reasoning_tokens"),
            total_tokens=values.get("total_tokens"),
            cache_write_input_tokens=values.get("cache_write_input_tokens"),
            source="compatibility_delta",
        )
        return await self.storage.insert(record)

    async def observe_snapshot(self, snapshot: UsageSnapshot) -> dict[str, Any]:
        """Persist a resume/replay baseline without creating usage."""

        await self.initialize()
        return await self.storage.observe_snapshot(snapshot)

    async def record_turn_snapshot(
        self,
        *,
        conversation_id: str | None,
        thread_id: str | None,
        turn_id: str | None,
        model: str | None,
        reasoning_effort: str | None,
        snapshot: UsageSnapshot,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        """Convert a thread cumulative snapshot into one persisted turn delta."""

        await self.initialize()
        now = int(timestamp if timestamp is not None else time.time())
        record = UsageRecord(
            timestamp=now,
            local_date=self._local_date(now),
            conversation_hash=self._hash_conversation(conversation_id),
            thread_id=thread_id,
            turn_id=turn_id,
            model=model,
            reasoning_effort=reasoning_effort,
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            total_tokens=None,
            cache_write_input_tokens=None,
            context_total_tokens=snapshot.last.total_tokens if snapshot.last else None,
            model_context_window=snapshot.model_context_window,
            source=snapshot.source,
        )
        # The storage transaction derives the field-by-field delta and fills
        # the record values before insertion. This keeps snapshot update and
        # turn deduplication atomic.
        diagnostic = await self.storage.record_snapshot(record, snapshot)
        if self.debug_enabled:
            diagnostic["log_safe"] = True
        return diagnostic

    def _window(self, days: int) -> tuple[str, str]:
        safe_days = max(1, min(3660, int(days)))
        end = self._today()
        return (end - timedelta(days=safe_days - 1)).isoformat(), end.isoformat()

    async def daily(self, days: int = 180) -> list[dict[str, Any]]:
        await self.initialize()
        start, end = self._window(days)
        rows = await self.storage.rows(start, end)
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_date.setdefault(str(row["local_date"]), []).append(row)
        values = [aggregate_rows(group).get("total_tokens") or 0 for group in by_date.values()]
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        result = []
        current = start_day
        while current <= end_day:
            key = current.isoformat()
            item = aggregate_rows(by_date.get(key, []))
            item.update({"date": key, "level": heat_level(item.get("total_tokens") or 0, values)})
            result.append(item)
            current += timedelta(days=1)
        return result

    async def summary(self, days: int = 30) -> dict[str, Any]:
        await self.initialize()
        safe_days = max(1, min(3660, int(days)))
        today = self._today().isoformat()
        start, end = self._window(safe_days)
        rows = await self.storage.rows(start, end)
        today_rows = [row for row in rows if row.get("local_date") == today]
        last7_start, _ = self._window(7)
        last30_start, _ = self._window(30)
        window = aggregate_rows(rows)
        return {
            "timezone": self.timezone_name,
            "startDate": start,
            "endDate": end,
            "trackingStartedAt": await self.storage.meta("tracking_started_at"),
            "window": window,
            "today": aggregate_rows(today_rows),
            "last7Days": aggregate_rows([row for row in rows if str(row["local_date"]) >= last7_start]),
            "last30Days": aggregate_rows([row for row in rows if str(row["local_date"]) >= last30_start]),
        }

    async def by_model(self, days: int = 30) -> list[dict[str, Any]]:
        await self.initialize()
        start, end = self._window(days)
        return aggregate_by(await self.storage.rows(start, end), "model")

    async def by_effort(self, days: int = 30) -> list[dict[str, Any]]:
        await self.initialize()
        start, end = self._window(days)
        return aggregate_by(await self.storage.rows(start, end), "reasoning_effort")

    async def recent_turns(self, limit: int = 20) -> list[dict[str, Any]]:
        await self.initialize()
        return self._mask_debug_ids(await self.storage.recent_turns(limit))

    async def debug(self) -> dict[str, Any]:
        await self.initialize()
        result = self._mask_debug_ids(await self.storage.debug())
        result["database"] = str(self.storage.path)
        result["timezone"] = self.timezone_name
        result["retentionDays"] = self.retention_days
        result["accounting"] = {
            "source": "thread/tokenUsage/updated",
            "semantic": "total=thread_cumulative, last=context_snapshot",
            "persisted": "field_by_field_delta(total)",
            "cachedInput": "breakdown_of_input_not_added_to_total",
            "reasoning": "breakdown_of_output_not_added_to_total",
        }
        return result

    async def reset(self) -> dict[str, int]:
        await self.initialize()
        return await self.storage.reset()

    async def close(self) -> None:
        return None
