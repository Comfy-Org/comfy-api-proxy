"""Optional SQLite-backed durable state for a single proxy process.

Persists proxy-layer job records, ``Idempotency-Key`` claims, the asset
index, and the HMAC secret for output asset ids. Write-through behind
in-memory indexes; one proxy ↔ one ComfyUI (see ``docs/batch-workloads.md``).

Not a substitute for ComfyUI's ``--database-url`` asset catalog
(``--enable-assets``), which does not store v2 jobs or idempotency keys.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .assets import AssetRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    metadata   TEXT,
    priority   INTEGER,
    workflow   TEXT NOT NULL
);

-- load_jobs sorts by created_at at every startup, over a table nothing prunes.
CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL,
    job_id     TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id           TEXT PRIMARY KEY,
    hash         TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    file_path    TEXT,
    created_at   TEXT NOT NULL,
    comfy_ref    TEXT,
    disk_path    TEXT,
    tags         TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS asset_hash_index (
    hash     TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL
);
"""


def _job_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "created_at": datetime.fromisoformat(str(row["created_at"])),
        "client_id": str(row["client_id"]),
        "metadata": row["metadata"],
        "priority": row["priority"],
    }


def _chmod_quietly(target: Path, mode: int) -> None:
    """Best-effort chmod — filesystems without POSIX modes must not be fatal."""
    try:
        os.chmod(target, mode)
    except OSError:
        pass


class StateStore:
    """Thread-safe (check_same_thread=False + lock) SQLite state for one proxy."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The DB holds the HMAC secret for output asset ids: owner-only, or any
        # local user could read it and mint valid ids. chmod after mkdir/connect
        # because umask masks the mode= argument. 0700 on the directory also
        # covers the WAL/SHM sidecars, which SQLite creates itself.
        _chmod_quietly(self.path.parent, 0o700)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        _chmod_quietly(self.path, 0o600)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta ----------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # -- jobs ----------------------------------------------------------------
    def upsert_job(self, record: dict[str, Any]) -> None:
        created = record["created_at"]
        created_s = created.isoformat() if isinstance(created, datetime) else str(created)
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(id, created_at, client_id, metadata, priority, workflow) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "created_at=excluded.created_at, client_id=excluded.client_id, "
                "metadata=excluded.metadata, priority=excluded.priority, "
                "workflow=excluded.workflow",
                (
                    record["id"],
                    created_s,
                    record["client_id"],
                    record.get("metadata"),
                    record.get("priority"),
                    json.dumps(record.get("workflow") or {}),
                ),
            )
            self._conn.commit()

    def load_jobs(self, limit: int) -> dict[str, dict[str, Any]]:
        """Newest ``limit`` job records, without their stored workflow graphs.

        ``workflow`` is write-only today (kept for forensics), and hydrating one
        graph per job is what makes a long-lived --state-dir expensive to start.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, client_id, metadata, priority FROM jobs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in reversed(rows):  # oldest-first so dict order stays FIFO
            out[str(row["id"])] = _job_record(row)
        return out

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """One job record by id, or None. The read path for rows older than the
        ``load_jobs`` startup window, which is much shorter than ComfyUI's
        history ring (see ``docs/batch-workloads.md``)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, client_id, metadata, priority FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else _job_record(row)

    def get_job_workflow(self, job_id: str) -> dict[str, Any] | None:
        """The resolved (executed) workflow graph for one job, or None. Separate from
        ``get_job`` (which deliberately skips this column) so the common
        job-lookup path stays cheap; only GET /jobs/{id}/workflow pays for it."""
        with self._lock:
            row = self._conn.execute("SELECT workflow FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        workflow = json.loads(row["workflow"])
        return workflow if isinstance(workflow, dict) else None

    # -- idempotency ---------------------------------------------------------
    def claim_idempotency(self, key: str, *, claimed_at: str, job_id: str | None = None) -> bool:
        """Return True if the key was newly claimed; False if already present."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO idempotency_keys(key, claimed_at, job_id) VALUES (?, ?, ?)",
                    (key, claimed_at, job_id),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def release_idempotency(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
            self._conn.commit()

    def load_idempotency_keys(self) -> list[str]:
        """Oldest-first so callers can rebuild an OrderedDict with FIFO eviction."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM idempotency_keys ORDER BY claimed_at ASC"
            ).fetchall()
        return [str(r["key"]) for r in rows]

    def trim_idempotency(self, max_keys: int) -> None:
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) AS c FROM idempotency_keys").fetchone()["c"]
            excess = int(count) - max_keys
            if excess <= 0:
                return
            self._conn.execute(
                "DELETE FROM idempotency_keys WHERE key IN ("
                "SELECT key FROM idempotency_keys ORDER BY claimed_at ASC LIMIT ?"
                ")",
                (excess,),
            )
            self._conn.commit()

    # -- assets --------------------------------------------------------------
    def upsert_asset(self, record: AssetRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO assets(id, hash, size_bytes, content_type, file_path, "
                "created_at, comfy_ref, disk_path, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "hash=excluded.hash, size_bytes=excluded.size_bytes, "
                "content_type=excluded.content_type, file_path=excluded.file_path, "
                "created_at=excluded.created_at, comfy_ref=excluded.comfy_ref, "
                "disk_path=excluded.disk_path, tags=excluded.tags",
                (
                    record.id,
                    record.hash,
                    record.size_bytes,
                    record.content_type,
                    record.file_path,
                    record.created_at,
                    json.dumps(record.comfy_ref) if record.comfy_ref else None,
                    record.disk_path,
                    json.dumps(record.tags),
                ),
            )
            if record.hash:
                self._conn.execute(
                    "INSERT INTO asset_hash_index(hash, asset_id) VALUES (?, ?) "
                    "ON CONFLICT(hash) DO NOTHING",
                    (record.hash, record.id),
                )
            self._conn.commit()

    def load_assets(self) -> tuple[dict[str, AssetRecord], dict[str, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM assets").fetchall()
            hash_rows = self._conn.execute("SELECT hash, asset_id FROM asset_hash_index").fetchall()
        by_id: dict[str, AssetRecord] = {}
        for row in rows:
            comfy_raw = row["comfy_ref"]
            tags_raw = row["tags"] or "[]"
            by_id[str(row["id"])] = AssetRecord(
                id=str(row["id"]),
                hash=str(row["hash"] or ""),
                size_bytes=int(row["size_bytes"]),
                content_type=str(row["content_type"]),
                file_path=row["file_path"],
                created_at=str(row["created_at"]),
                comfy_ref=json.loads(comfy_raw) if comfy_raw else None,
                disk_path=row["disk_path"],
                tags=list(json.loads(tags_raw)),
            )
        id_by_hash = {str(r["hash"]): str(r["asset_id"]) for r in hash_rows}
        return by_id, id_by_hash
