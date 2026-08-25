from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import UsageRecord


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    conversation_hash TEXT,
    thread_id TEXT,
    turn_id TEXT UNIQUE,
    model TEXT,
    reasoning_effort TEXT,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    request_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(local_date);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_records(model);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_records(timestamp);
CREATE TABLE IF NOT EXISTS usage_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class UsageStorage:
    """Small SQLite gateway; all database work runs outside AstrBot's loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO usage_meta(key, value) VALUES('tracking_started_at', ?)",
                (str(int(time.time())),),
            )

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    @staticmethod
    def _record_values(record: UsageRecord) -> tuple[Any, ...]:
        return (
            record.timestamp,
            record.local_date,
            record.conversation_hash,
            record.thread_id,
            record.turn_id,
            record.model,
            record.reasoning_effort,
            record.input_tokens,
            record.cached_input_tokens,
            record.output_tokens,
            record.reasoning_tokens,
            record.total_tokens,
            record.request_count,
        )

    def _insert_sync(self, record: UsageRecord) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO usage_records(
                    timestamp, local_date, conversation_hash, thread_id, turn_id,
                    model, reasoning_effort, input_tokens, cached_input_tokens,
                    output_tokens, reasoning_tokens, total_tokens, request_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO NOTHING""",
                self._record_values(record),
            )
            return cursor.rowcount == 1

    async def insert(self, record: UsageRecord) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._insert_sync, record)

    def _rows_sync(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_records WHERE local_date BETWEEN ? AND ? ORDER BY timestamp",
                (start_date, end_date),
            ).fetchall()
            return [dict(row) for row in rows]

    async def rows(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._rows_sync, start_date, end_date)

    def _meta_sync(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM usage_meta WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None

    async def meta(self, key: str) -> str | None:
        return await asyncio.to_thread(self._meta_sync, key)

    def _cleanup_sync(self, cutoff: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM usage_records WHERE timestamp < ?", (cutoff,))
            connection.execute(
                "INSERT INTO usage_meta(key, value) VALUES('last_cleanup_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(int(time.time())),),
            )
            return cursor.rowcount

    async def cleanup(self, cutoff: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_sync, cutoff)

    def _debug_sync(self) -> dict[str, Any]:
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            last = connection.execute(
                "SELECT timestamp, local_date, model, turn_id FROM usage_records "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            return {
                "ok": True,
                "records": int(count),
                "trackingStartedAt": self._meta_sync("tracking_started_at"),
                "lastRecord": dict(last) if last else None,
            }

    async def debug(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._debug_sync)


