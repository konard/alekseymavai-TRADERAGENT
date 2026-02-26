"""
Job Store — SQLite-based persistence for backtest jobs.

Tracks backtest and optimization job metadata including status,
configuration, results, and timestamps.

Usage:
    store = JobStore("/tmp/backtest_jobs.db")
    store.initialize()
    job_id = store.create("backtest", {"symbol": "BTC/USDT"})
    store.update_status(job_id, "running")
    store.update_status(job_id, "completed", result={"return_pct": 5.2})
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


class JobStore:
    """Synchronous SQLite job store for backtest/optimization jobs."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Create the jobs table if it doesn't exist."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                config_json TEXT,
                result_json TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        self._conn.commit()

    def create(self, job_type: str, config: dict[str, Any] | None = None) -> str:
        """Create a new job and return its ID."""
        job_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO jobs (job_id, job_type, config_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, job_type, json.dumps(config) if config else None, now, now),
        )
        self._conn.commit()
        return job_id

    def update_status(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Update job status, optionally setting result or error."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, error_message = ?, updated_at = ? "
            "WHERE job_id = ?",
            (
                status,
                json.dumps(result) if result else None,
                error,
                now,
                job_id,
            ),
        )
        self._conn.commit()

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Get a job by ID."""
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_jobs(
        self,
        job_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List jobs, optionally filtered by type and/or status."""
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        conditions = []

        if job_type:
            conditions.append("job_type = ?")
            params.append(job_type)
        if status:
            conditions.append("status = ?")
            params.append(status)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def cleanup_old(self, days: int = 30) -> int:
        """Delete jobs older than N days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self._conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Convert sqlite3.Row to dict with parsed JSON fields."""
        d = dict(row)
        if d.get("config_json"):
            d["config"] = json.loads(d["config_json"])
        else:
            d["config"] = None
        if d.get("result_json"):
            d["result"] = json.loads(d["result_json"])
        else:
            d["result"] = None
        return d
