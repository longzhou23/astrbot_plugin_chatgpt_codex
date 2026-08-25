from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ..usage.aggregate import heat_level
from ..usage.models import TokenUsage, parse_token_usage_event
from ..usage.service import UsageService


class UsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = UsageService(
            Path(self.temp_dir.name) / "usage.db",
            timezone_name="Asia/Shanghai",
            retention_days=0,
        )

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.temp_dir.cleanup()

    def test_parse_real_codex_event_uses_last_only(self) -> None:
        thread_id, turn_id, usage = parse_token_usage_event(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 100,
                        "cachedInputTokens": 70,
                        "outputTokens": 20,
                        "reasoningOutputTokens": 5,
                        "totalTokens": 120,
                    },
                    "total": {"inputTokens": 9000, "totalTokens": 10000},
                },
            }
        )
        self.assertEqual((thread_id, turn_id), ("thread-1", "turn-1"))
        self.assertEqual(usage, TokenUsage(100, 70, 20, 5, 120, None))

    async def test_deduplicates_turn_after_restart(self) -> None:
        kwargs = {
            "conversation_id": "private-session-id",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "model": "server-model",
            "reasoning_effort": "auto",
            "usage": TokenUsage(100, 70, 20, None, 120),
            "timestamp": int(datetime(2026, 8, 24, 16, 30, tzinfo=timezone.utc).timestamp()),
        }
        self.assertTrue(await self.service.record_turn_usage(**kwargs))
        self.assertFalse(await self.service.record_turn_usage(**kwargs))
        summary = await self.service.summary(30)
        self.assertEqual(summary["window"]["requests"], 1)
        self.assertEqual(summary["window"]["total_tokens"], 120)
        self.assertIsNone(summary["window"]["reasoning_tokens"])

    async def test_timezone_boundary_and_daily_aggregation(self) -> None:
        # 16:30 UTC is 00:30 on the next day in Asia/Shanghai.
        await self.service.record_turn_usage(
            conversation_id="session",
            thread_id="thread",
            turn_id="boundary",
            model="m",
            reasoning_effort="low",
            usage={"last": {"inputTokens": 10, "totalTokens": 10}},
            timestamp=int(datetime(2026, 8, 24, 16, 30, tzinfo=timezone.utc).timestamp()),
        )
        rows = await self.service.daily(10)
        item = next(row for row in rows if row["date"] == "2026-08-25")
        self.assertEqual(item["total_tokens"], 10)
        self.assertEqual(item["requests"], 1)

    async def test_null_reasoning_and_model_breakdown(self) -> None:
        await self.service.record_turn_usage(
            conversation_id="session",
            thread_id="thread",
            turn_id="no-reasoning",
            model="m",
            reasoning_effort="auto",
            usage=TokenUsage(input_tokens=5, total_tokens=5),
        )
        grouped = await self.service.by_model(1)
        self.assertEqual(grouped[0]["model"], "m")
        self.assertIsNone(grouped[0]["reasoning_tokens"])

    async def test_heat_levels_are_monotonic(self) -> None:
        values = [0, 100, 500, 1000, 5000, 10000]
        levels = [heat_level(value, values[1:]) for value in values]
        self.assertEqual(levels[0], 0)
        self.assertEqual(levels, sorted(levels))
        self.assertGreaterEqual(levels[-1], levels[1])


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(unittest.main())

