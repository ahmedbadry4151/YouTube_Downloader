from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'video',
    status TEXT NOT NULL DEFAULT 'queued',
    options TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    total_items INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    idx INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    duration REAL,
    status TEXT NOT NULL DEFAULT 'queued',
    percent REAL NOT NULL DEFAULT 0,
    speed REAL NOT NULL DEFAULT 0,
    eta REAL,
    downloaded INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    filepath TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_job ON items(job_id, idx);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

_ITEM_FIELDS = (
    "id",
    "job_id",
    "idx",
    "url",
    "title",
    "duration",
    "status",
    "percent",
    "speed",
    "eta",
    "downloaded",
    "total",
    "filepath",
    "error",
    "attempts",
    "created_at",
    "updated_at",
)

_JOB_FIELDS = (
    "id",
    "url",
    "title",
    "kind",
    "status",
    "options",
    "created_at",
    "updated_at",
    "error",
    "total_items",
)


class Store:
    """Small SQLite persistence layer (WAL, thread-safe)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes ---------------------------------------------------------
    def save_job(self, job: dict[str, Any]) -> None:
        values = [job.get(field) for field in _JOB_FIELDS]
        placeholders = ", ".join("?" for _ in _JOB_FIELDS)
        columns = ", ".join(_JOB_FIELDS)
        updates = ", ".join(f"{field}=excluded.{field}" for field in _JOB_FIELDS if field != "id")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
            self._conn.commit()

    def save_items(self, items: Iterable[dict[str, Any]]) -> None:
        rows = [[item.get(field) for field in _ITEM_FIELDS] for item in items]
        if not rows:
            return
        placeholders = ", ".join("?" for _ in _ITEM_FIELDS)
        columns = ", ".join(_ITEM_FIELDS)
        updates = ", ".join(f"{field}=excluded.{field}" for field in _ITEM_FIELDS if field != "id")
        with self._lock:
            self._conn.executemany(
                f"INSERT INTO items ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                rows,
            )
            self._conn.commit()

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM items WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()

    def prune(self, keep: int) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?", (keep,)
            ).fetchall()
            for row in rows:
                self._conn.execute("DELETE FROM items WHERE job_id = ?", (row["id"],))
                self._conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            self._conn.commit()

    # -- reads ----------------------------------------------------------
    def load(self, limit: int = 500) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        with self._lock:
            job_rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            jobs = [self._row_to_job(row) for row in job_rows]
            by_id: dict[str, list[dict[str, Any]]] = {job["id"]: [] for job in jobs}
            if by_id:
                placeholders = ", ".join("?" for _ in by_id)
                item_rows = self._conn.execute(
                    f"SELECT * FROM items WHERE job_id IN ({placeholders}) ORDER BY idx ASC",
                    tuple(by_id),
                ).fetchall()
                for row in item_rows:
                    by_id[row["job_id"]].append(dict(row))
        return jobs, by_id

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        try:
            job["options"] = json.loads(job.get("options") or "{}")
        except (TypeError, json.JSONDecodeError):
            job["options"] = {}
        return job

    def count_jobs(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
            return int(row["n"]) if row else 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass
