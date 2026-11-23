from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path


class SnapshotCache:
    """Share provider responses and fetch leases between workers using the same database."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS provider_snapshots ("
            "key TEXT PRIMARY KEY, payload TEXT, expires REAL NOT NULL DEFAULT 0, "
            "owner TEXT, lease_until REAL NOT NULL DEFAULT 0)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS provider_snapshots_expires ON provider_snapshots(expires)")
        self.pending: dict[str, asyncio.Task] = {}

    def peek(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload FROM provider_snapshots WHERE key = ? AND expires > ?",
            (key, time.time()),
        ).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    async def get(self, key: str, ttl: float, fetch: Callable[[], Awaitable[dict]]) -> dict:
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(self._load(key, ttl, fetch))
            self.pending[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task) -> None:
        if self.pending.get(key) is task:
            self.pending.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _load(self, key: str, ttl: float, fetch: Callable[[], Awaitable[dict]]) -> dict:
        owner = uuid.uuid4().hex
        while True:
            now = time.time()
            row = self.db.execute(
                "SELECT payload, expires FROM provider_snapshots WHERE key = ?", (key,)
            ).fetchone()
            if row and row[0] is not None and row[1] > now:
                return json.loads(row[0])
            claimed = self.db.execute(
                "INSERT INTO provider_snapshots (key, owner, lease_until) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET owner=excluded.owner, lease_until=excluded.lease_until "
                "WHERE provider_snapshots.lease_until <= ? AND provider_snapshots.expires <= ?",
                (key, owner, now + 120, now, now),
            ).rowcount
            if claimed:
                break
            await asyncio.sleep(0.1)
        try:
            payload = await asyncio.wait_for(fetch(), timeout=100)
            self.db.execute(
                "UPDATE provider_snapshots SET payload = ?, expires = ?, owner = NULL, lease_until = 0 "
                "WHERE key = ? AND owner = ?",
                (json.dumps(payload), time.time() + ttl, key, owner),
            )
            return payload
        finally:
            self.db.execute(
                "UPDATE provider_snapshots SET owner = NULL, lease_until = 0 WHERE key = ? AND owner = ?",
                (key, owner),
            )

    async def shutdown(self) -> None:
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.close()

    def close(self) -> None:
        self.db.close()
