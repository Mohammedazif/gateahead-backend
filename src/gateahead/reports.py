from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path


class Reports:
    ttl = 600

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS gate_reports ("
            "crossing_id TEXT NOT NULL, reporter TEXT NOT NULL, state TEXT NOT NULL, "
            "created_at REAL NOT NULL, PRIMARY KEY (crossing_id, reporter))"
        )
        self.db.commit()

    def submit(self, crossing_id: str, installation: str, state: str) -> dict:
        now = time.time()
        reporter = hashlib.sha256(installation.encode()).hexdigest()
        self.db.execute("DELETE FROM gate_reports WHERE created_at <= ?", (now - self.ttl,))
        self.db.execute(
            "INSERT INTO gate_reports VALUES (?, ?, ?, ?) "
            "ON CONFLICT(crossing_id, reporter) DO UPDATE SET state = excluded.state, "
            "created_at = excluded.created_at",
            (crossing_id, reporter, state, now),
        )
        self.db.commit()
        return self.recent(crossing_id)

    def recent(self, crossing_id: str) -> dict:
        now = time.time()
        rows = self.db.execute(
            "SELECT state, created_at FROM gate_reports WHERE crossing_id = ? "
            "AND created_at > ? ORDER BY created_at DESC LIMIT 20",
            (crossing_id, now - self.ttl),
        ).fetchall()
        return {
            "verified": False,
            "expires_after_seconds": self.ttl,
            "reports": [
                {"state": row["state"], "at": row["created_at"], "expires_at": row["created_at"] + self.ttl}
                for row in rows
            ],
        }
