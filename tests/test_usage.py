from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ..usage.aggregate import heat_level
from ..usage.models import (
    TokenUsage,
    UsageSnapshot,
    calculate_delta,
    parse_token_usage_event,
    parse_usage_snapshot_event,
)
from ..usage.service import UsageService
from ..usage.storage import UsageStorage


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

    def test_parse_snapshot_keeps_total_and_last_separate(self) -> None:
        thread_id, turn_id, snapshot = parse_usage_snapshot_event(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {"inputTokens": 100, "totalTokens": 120},
                    "total": {"inputTokens": 9000, "totalTokens": 10000},
                    "modelContextWindow": 128000,
                },
            }
        )
        self.assertEqual((thread_id, turn_id), ("thread-1", "turn-1"))
        self.assertEqual(snapshot.total.total_tokens, 10000)
        self.assertEqual(snapshot.last.total_tokens, 120)
        self.assertEqual(snapshot.model_context_window, 128000)

    def test_delta_does_not_double_count_breakdowns(self) -> None:
        delta = calculate_delta(
            None,
            TokenUsage(
                input_tokens=120,
                cached_input_tokens=100,
                output_tokens=30,
                reasoning_tokens=10,
                total_tokens=150,
            ),
        )
        self.assertEqual(delta.total_tokens, 150)
        self.assertEqual(delta.cached_input_tokens, 100)
        self.assertEqual(delta.reasoning_tokens, 10)

    async def test_cumulative_snapshots_are_persisted_as_deltas(self) -> None:
        def snapshot(total: int, turn: str) -> UsageSnapshot:
            return UsageSnapshot(
                total=TokenUsage(input_tokens=total, output_tokens=total // 100, total_tokens=total),
                last=TokenUsage(input_tokens=100, total_tokens=100),
                thread_id="thread-cumulative",
                turn_id=turn,
            )

        for total, turn in ((14000, "t1"), (28700, "t2"), (43600, "t3")):
            result = await self.service.record_turn_snapshot(
                conversation_id="session",
                thread_id="thread-cumulative",
                turn_id=turn,
                model="m",
                reasoning_effort="auto",
                snapshot=snapshot(total, turn),
            )
            self.assertTrue(result["persisted"])
        summary = await self.service.summary(30)
        self.assertEqual(summary["window"]["total_tokens"], 43600)
        rows = await self.service.recent_turns(10)
        self.assertEqual(sorted(row["total_tokens"] for row in rows), [14000, 14700, 14900])

    async def test_duplicate_snapshot_does_not_advance_baseline(self) -> None:
        snapshot = UsageSnapshot(
            total=TokenUsage(input_tokens=100, total_tokens=100),
            thread_id="thread-duplicate",
            turn_id="turn-duplicate",
        )
        kwargs = {
            "conversation_id": "session",
            "thread_id": "thread-duplicate",
            "turn_id": "turn-duplicate",
            "model": "m",
            "reasoning_effort": "auto",
            "snapshot": snapshot,
        }
        first = await self.service.record_turn_snapshot(**kwargs)
        second = await self.service.record_turn_snapshot(**kwargs)
        self.assertTrue(first["persisted"])
        self.assertTrue(second["duplicate"])
        self.assertEqual((await self.service.summary(30))["window"]["requests"], 1)

    async def test_counter_reset_starts_new_delta_without_negative_usage(self) -> None:
        def make(total: int, turn: str) -> UsageSnapshot:
            return UsageSnapshot(
                total=TokenUsage(input_tokens=total, total_tokens=total),
                thread_id="thread-reset",
                turn_id=turn,
            )

        await self.service.record_turn_snapshot(
            conversation_id="session", thread_id="thread-reset", turn_id="before",
            model="m", reasoning_effort="auto", snapshot=make(44000, "before"),
        )
        result = await self.service.record_turn_snapshot(
            conversation_id="session", thread_id="thread-reset", turn_id="after",
            model="m", reasoning_effort="auto", snapshot=make(14000, "after"),
        )
        self.assertTrue(result["counter_reset"])
        self.assertEqual((await self.service.summary(30))["window"]["total_tokens"], 58000)
        row = (await self.service.recent_turns(1))[0]
        self.assertEqual(row["total_tokens"], 14000)
        self.assertEqual(row["counter_reset"], 1)

    async def test_v1_migration_preserves_legacy_table(self) -> None:
        path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE usage_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO usage_meta VALUES('schema_version', '1')")
            connection.execute(
                "CREATE TABLE usage_records(id INTEGER PRIMARY KEY, timestamp INTEGER, local_date TEXT, "
                "conversation_hash TEXT, thread_id TEXT, turn_id TEXT UNIQUE, model TEXT, "
                "reasoning_effort TEXT, input_tokens INTEGER, cached_input_tokens INTEGER, "
                "output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER, request_count INTEGER)"
            )
            connection.execute("INSERT INTO usage_records VALUES(1, 1, '2026-08-25', NULL, NULL, 'old', 'm', 'auto', 1, 0, 1, NULL, 2, 1)")
            connection.commit()
        storage = UsageStorage(path)
        await storage.initialize()
        debug = await storage.debug()
        self.assertEqual(debug["schemaVersion"], 2)
        self.assertEqual(debug["legacyRecords"], 1)
        self.assertEqual((await storage.rows("2026-08-25", "2026-08-25")), [])

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

    async def test_empty_usage_payload_is_not_presented_as_a_turn(self) -> None:
        recorded = await self.service.record_turn_usage(
            conversation_id="session",
            thread_id=None,
            turn_id="empty-usage",
            model="m",
            reasoning_effort="auto",
            usage=None,
        )
        self.assertFalse(recorded)
        self.assertEqual(await self.service.recent_turns(10), [])

    async def test_transport_snake_case_usage_is_persisted(self) -> None:
        recorded = await self.service.record_turn_usage(
            conversation_id="transport-session",
            thread_id=None,
            turn_id="transport-response-1",
            model="gpt-transport",
            reasoning_effort="low",
            usage={
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 7,
                "reasoning_tokens": 2,
                "total_tokens": 107,
            },
        )
        self.assertTrue(recorded)
        row = (await self.service.recent_turns(1))[0]
        self.assertEqual(row["model"], "gpt-transport")
        self.assertEqual(row["total_tokens"], 107)
        self.assertEqual(row["cached_input_tokens"], 20)

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
