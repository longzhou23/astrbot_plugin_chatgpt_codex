from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any


class SessionStore:
    """Durable AstrBot-session to Codex-thread mapping without prompt storage."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                bootstrapped INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                prompt_version TEXT,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0
            )"""
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)").fetchall()}
        for name, definition in (
            ("prompt_version", "TEXT"),
            ("created_at", "REAL NOT NULL DEFAULT 0"),
            ("turn_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self._db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
        self._db.execute(
            "UPDATE sessions SET created_at=updated_at WHERE created_at=0 OR created_at IS NULL"
        )
        self._db.commit()
        self._db_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_key: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_key, asyncio.Lock())

    async def get(self, session_key: str) -> dict | None:
        async with self._db_lock:
            row = self._db.execute(
                """SELECT thread_id, bootstrapped, model, prompt_version,
                   created_at, updated_at, turn_count
                   FROM sessions WHERE session_key=?""",
                (session_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "thread_id": row[0],
            "bootstrapped": bool(row[1]),
            "model": row[2],
            "prompt_version": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "turn_count": row[6],
        }

    async def put(
        self,
        session_key: str,
        thread_id: str,
        *,
        bootstrapped: bool,
        model: str | None,
        prompt_version: str | None = None,
        increment_turn: bool = False,
    ) -> None:
        now = time.time()
        async with self._db_lock:
            self._db.execute(
                """INSERT INTO sessions(
                       session_key, thread_id, bootstrapped, model, prompt_version,
                       created_at, updated_at, turn_count
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_key) DO UPDATE SET thread_id=excluded.thread_id,
                   bootstrapped=excluded.bootstrapped, model=excluded.model, updated_at=excluded.updated_at""",
                (
                    session_key,
                    thread_id,
                    int(bootstrapped),
                    model,
                    prompt_version,
                    now,
                    now,
                    0,
                ),
            )
            if increment_turn:
                self._db.execute(
                    "UPDATE sessions SET turn_count=turn_count+1 WHERE session_key=?",
                    (session_key,),
                )
            self._db.commit()

    async def cleanup(self, *, idle_ttl: float, max_age: float) -> int:
        now = time.time()
        async with self._db_lock:
            cursor = self._db.execute(
                "DELETE FROM sessions WHERE updated_at < ? OR created_at < ?",
                (now - max(0.0, idle_ttl), now - max(0.0, max_age)),
            )
            self._db.commit()
            return cursor.rowcount

    async def snapshot(self) -> list[dict[str, Any]]:
        """Return non-secret mapping metadata for diagnostics."""

        async with self._db_lock:
            rows = self._db.execute(
                """SELECT session_key, thread_id, prompt_version, created_at,
                   updated_at, turn_count FROM sessions ORDER BY updated_at DESC"""
            ).fetchall()
        return [
            {
                "session_key": row[0],
                "thread_id": row[1],
                "prompt_version": row[2],
                "created_at": row[3],
                "updated_at": row[4],
                "turn_count": row[5],
            }
            for row in rows
        ]

    async def reset(self, session_key: str) -> bool:
        async with self._db_lock:
            cursor = self._db.execute("DELETE FROM sessions WHERE session_key=?", (session_key,))
            self._db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        async with self._db_lock:
            self._db.close()
