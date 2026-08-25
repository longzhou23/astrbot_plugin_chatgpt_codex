from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import (
    TOKEN_FIELDS,
    TokenUsage,
    UsageRecord,
    UsageSnapshot,
    calculate_delta,
)

SCHEMA_VERSION = 2
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
    request_count INTEGER NOT NULL DEFAULT 1,
    cache_write_input_tokens INTEGER,
    context_total_tokens INTEGER,
    model_context_window INTEGER,
    source TEXT NOT NULL DEFAULT 'thread/tokenUsage/updated',
    counter_reset INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_records_date ON usage_records(local_date);
CREATE INDEX IF NOT EXISTS idx_usage_records_model ON usage_records(model);
CREATE INDEX IF NOT EXISTS idx_usage_records_timestamp ON usage_records(timestamp);
CREATE TABLE IF NOT EXISTS usage_snapshots (
    thread_id TEXT PRIMARY KEY,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    cache_write_input_tokens INTEGER,
    model_context_window INTEGER,
    last_turn_id TEXT,
    source TEXT NOT NULL DEFAULT 'thread/tokenUsage/updated',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_debug_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    thread_id TEXT,
    turn_id TEXT,
    source TEXT NOT NULL,
    semantic TEXT NOT NULL,
    previous_json TEXT,
    current_json TEXT NOT NULL,
    delta_json TEXT,
    persisted INTEGER NOT NULL DEFAULT 0,
    counter_reset INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_debug_timestamp ON usage_debug_events(timestamp);
CREATE TABLE IF NOT EXISTS usage_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class UsageStorage:
    """SQLite gateway for cumulative snapshots and turn deltas."""

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

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM usage_meta WHERE key='schema_version'"
            ).fetchone()
            try:
                version = int(row[0]) if row else 0
            except (TypeError, ValueError):
                version = 0
            migrated = False
            if version < SCHEMA_VERSION and self._table_exists(connection, "usage_records"):
                legacy = "usage_records_legacy_v1"
                if not self._table_exists(connection, legacy):
                    connection.execute(f"ALTER TABLE usage_records RENAME TO {legacy}")
                    migrated = True
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO usage_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            if migrated:
                connection.execute(
                    "INSERT INTO usage_meta(key, value) VALUES('historical_usage_status', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        "legacy_v1_preserved_inaccurate_last_snapshot_records",
                    ),
                )
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
            record.cache_write_input_tokens,
            record.context_total_tokens,
            record.model_context_window,
            record.source,
            int(record.counter_reset),
        )

    def _insert_sync(self, record: UsageRecord) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO usage_records(
                    timestamp, local_date, conversation_hash, thread_id, turn_id,
                    model, reasoning_effort, input_tokens, cached_input_tokens,
                    output_tokens, reasoning_tokens, total_tokens, request_count,
                    cache_write_input_tokens, context_total_tokens, model_context_window,
                    source, counter_reset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO NOTHING""",
                self._record_values(record),
            )
            return cursor.rowcount == 1

    async def insert(self, record: UsageRecord) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._insert_sync, record)

    @staticmethod
    def _usage_from_row(row: sqlite3.Row | None) -> TokenUsage | None:
        if row is None:
            return None
        values = {name: row[name] for name in TOKEN_FIELDS}
        if not any(value is not None for value in values.values()):
            return None
        return TokenUsage(**values)

    @staticmethod
    def _snapshot_values(snapshot: UsageSnapshot) -> tuple[Any, ...]:
        total = snapshot.total
        return (
            snapshot.thread_id,
            total.input_tokens,
            total.cached_input_tokens,
            total.output_tokens,
            total.reasoning_tokens,
            total.total_tokens,
            total.cache_write_input_tokens,
            snapshot.model_context_window,
            snapshot.turn_id,
            snapshot.source,
            int(time.time()),
        )

    @staticmethod
    def _snapshot_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        usage = {name: row[name] for name in TOKEN_FIELDS}
        return {
            "thread_id": row["thread_id"],
            "total": usage,
            "model_context_window": row["model_context_window"],
            "turn_id": row["last_turn_id"],
            "source": row["source"],
            "updated_at": row["updated_at"],
        }

    def _upsert_snapshot_sync(self, connection: sqlite3.Connection, snapshot: UsageSnapshot) -> None:
        if not snapshot.thread_id:
            return
        connection.execute(
            """INSERT INTO usage_snapshots(
                thread_id, input_tokens, cached_input_tokens, output_tokens,
                reasoning_tokens, total_tokens, cache_write_input_tokens,
                model_context_window, last_turn_id, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                input_tokens=excluded.input_tokens,
                cached_input_tokens=excluded.cached_input_tokens,
                output_tokens=excluded.output_tokens,
                reasoning_tokens=excluded.reasoning_tokens,
                total_tokens=excluded.total_tokens,
                cache_write_input_tokens=excluded.cache_write_input_tokens,
                model_context_window=excluded.model_context_window,
                last_turn_id=excluded.last_turn_id,
                source=excluded.source,
                updated_at=excluded.updated_at""",
            self._snapshot_values(snapshot),
        )

    def _record_snapshot_sync(
        self,
        record: UsageRecord,
        snapshot: UsageSnapshot,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = None
            if record.turn_id:
                existing = connection.execute(
                    "SELECT id FROM usage_records WHERE turn_id=?", (record.turn_id,)
                ).fetchone()
            previous_row = (
                connection.execute(
                    "SELECT * FROM usage_snapshots WHERE thread_id=?",
                    (snapshot.thread_id,),
                ).fetchone()
                if snapshot.thread_id
                else None
            )
            previous = self._usage_from_row(previous_row)
            delta = calculate_delta(previous, snapshot.total)
            if existing is not None:
                connection.rollback()
                return {
                    "persisted": False,
                    "duplicate": True,
                    "previous": previous.as_dict() if previous else None,
                    "current": snapshot.total.as_dict(),
                    "delta": delta.as_dict(),
                    "counter_reset": delta.counter_reset,
                    "reset_fields": list(delta.reset_fields),
                    "source": snapshot.source,
                    "semantic": "thread_cumulative",
                }
            record = replace(
                record,
                input_tokens=delta.input_tokens,
                cached_input_tokens=delta.cached_input_tokens,
                output_tokens=delta.output_tokens,
                reasoning_tokens=delta.reasoning_tokens,
                total_tokens=delta.total_tokens,
                cache_write_input_tokens=delta.cache_write_input_tokens,
                counter_reset=delta.counter_reset,
            )
            cursor = connection.execute(
                """INSERT INTO usage_records(
                    timestamp, local_date, conversation_hash, thread_id, turn_id,
                    model, reasoning_effort, input_tokens, cached_input_tokens,
                    output_tokens, reasoning_tokens, total_tokens, request_count,
                    cache_write_input_tokens, context_total_tokens, model_context_window,
                    source, counter_reset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._record_values(record),
            )
            self._upsert_snapshot_sync(connection, snapshot)
            diagnostic = {
                "persisted": cursor.rowcount == 1,
                "duplicate": False,
                "previous": previous.as_dict() if previous else None,
                "current": snapshot.total.as_dict(),
                "delta": delta.as_dict(),
                "counter_reset": delta.counter_reset,
                "reset_fields": list(delta.reset_fields),
                "source": snapshot.source,
                "semantic": "thread_cumulative",
                "context": snapshot.last.as_dict() if snapshot.last else None,
                "model_context_window": snapshot.model_context_window,
            }
            connection.execute(
                """INSERT INTO usage_debug_events(
                    timestamp, thread_id, turn_id, source, semantic,
                    previous_json, current_json, delta_json, persisted,
                    counter_reset, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(time.time()),
                    snapshot.thread_id,
                    snapshot.turn_id,
                    snapshot.source,
                    "thread_cumulative",
                    json.dumps(diagnostic["previous"], separators=(",", ":")),
                    json.dumps(diagnostic["current"], separators=(",", ":")),
                    json.dumps(diagnostic["delta"], separators=(",", ":")),
                    int(diagnostic["persisted"]),
                    int(delta.counter_reset),
                    "turn_delta",
                ),
            )
            connection.commit()
            return diagnostic

    async def record_snapshot(
        self,
        record: UsageRecord,
        snapshot: UsageSnapshot,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._record_snapshot_sync, record, snapshot)

    def _observe_snapshot_sync(self, snapshot: UsageSnapshot) -> dict[str, Any]:
        with self._connect() as connection:
            previous_row = (
                connection.execute(
                    "SELECT * FROM usage_snapshots WHERE thread_id=?",
                    (snapshot.thread_id,),
                ).fetchone()
                if snapshot.thread_id
                else None
            )
            previous = self._usage_from_row(previous_row)
            self._upsert_snapshot_sync(connection, snapshot)
            diagnostic = {
                "persisted": False,
                "baseline": True,
                "previous": previous.as_dict() if previous else None,
                "current": snapshot.total.as_dict(),
                "delta": None,
                "counter_reset": False,
                "reset_fields": [],
                "source": snapshot.source,
                "semantic": "thread_cumulative_baseline",
            }
            connection.execute(
                """INSERT INTO usage_debug_events(
                    timestamp, thread_id, turn_id, source, semantic,
                    previous_json, current_json, delta_json, persisted,
                    counter_reset, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(time.time()),
                    snapshot.thread_id,
                    snapshot.turn_id,
                    snapshot.source,
                    "thread_cumulative_baseline",
                    json.dumps(diagnostic["previous"], separators=(",", ":")),
                    json.dumps(diagnostic["current"], separators=(",", ":")),
                    None,
                    0,
                    0,
                    "resume_replay",
                ),
            )
            connection.commit()
            return diagnostic

    async def observe_snapshot(self, snapshot: UsageSnapshot) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._observe_snapshot_sync, snapshot)

    def _rows_sync(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_records WHERE local_date BETWEEN ? AND ? ORDER BY timestamp",
                (start_date, end_date),
            ).fetchall()
            return [dict(row) for row in rows]

    async def rows(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._rows_sync, start_date, end_date)

    def _recent_turns_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_records "
                "WHERE input_tokens IS NOT NULL "
                "OR cached_input_tokens IS NOT NULL "
                "OR output_tokens IS NOT NULL "
                "OR reasoning_tokens IS NOT NULL "
                "OR total_tokens IS NOT NULL "
                "OR cache_write_input_tokens IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
            return [dict(row) for row in rows]

    async def recent_turns(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recent_turns_sync, limit)

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
            connection.commit()
            return cursor.rowcount

    async def cleanup(self, cutoff: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_sync, cutoff)

    def _debug_sync(self) -> dict[str, Any]:
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            legacy = 0
            if self._table_exists(connection, "usage_records_legacy_v1"):
                legacy = int(
                    connection.execute("SELECT COUNT(*) FROM usage_records_legacy_v1").fetchone()[0]
                )
            last = connection.execute(
                "SELECT timestamp, local_date, model, thread_id, turn_id, source, "
                "counter_reset FROM usage_records ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            events = connection.execute(
                "SELECT * FROM usage_debug_events ORDER BY timestamp DESC, id DESC LIMIT 20"
            ).fetchall()
            snapshots = connection.execute(
                "SELECT * FROM usage_snapshots ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
            return {
                "ok": True,
                "schemaVersion": int(self._meta_sync("schema_version") or SCHEMA_VERSION),
                "records": int(count),
                "legacyRecords": legacy,
                "historicalUsageStatus": self._meta_sync("historical_usage_status"),
                "trackingStartedAt": self._meta_sync("tracking_started_at"),
                "lastRecord": dict(last) if last else None,
                "recentDiagnostics": [dict(row) for row in events],
                "snapshots": [self._snapshot_dict(row) for row in snapshots],
            }

    async def debug(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._debug_sync)

    def _reset_sync(self) -> dict[str, int]:
        with self._connect() as connection:
            records = connection.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            snapshots = connection.execute("SELECT COUNT(*) FROM usage_snapshots").fetchone()[0]
            connection.execute("DELETE FROM usage_records")
            connection.execute("DELETE FROM usage_snapshots")
            connection.execute("DELETE FROM usage_debug_events")
            connection.execute(
                "INSERT INTO usage_meta(key, value) VALUES('tracking_started_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(int(time.time())),),
            )
            connection.commit()
            return {"records": int(records), "snapshots": int(snapshots)}

    async def reset(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._reset_sync)
