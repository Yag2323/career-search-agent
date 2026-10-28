from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

BASE = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE / "state" / "career_agent.db"

VALID_APPLICATION_STATUSES = {
    "FOUND",
    "SHORTLISTED",
    "RESUME_CREATED",
    "RESUME_APPROVED",
    "PDF_CREATED",
    "READY_MANUAL_APPLY",
    "APPLIED",
    "REJECTED",
    "INTERVIEW",
    "FOLLOW_UP",
    "OFFER",
    "WITHDRAWN",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class CareerStore:
    """SQLite source of truth for jobs, applications, approvals, and events."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    canonical_key TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    remote_type TEXT NOT NULL DEFAULT '',
                    primary_url TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    active_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    data_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_canonical_key
                ON jobs(canonical_key);

                CREATE TABLE IF NOT EXISTS job_aliases (
                    alias TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS job_sources (
                    source_url TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    source_domain TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT 'REVIEW',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_job_sources_job_id
                ON job_sources(job_id);

                CREATE TABLE IF NOT EXISTS job_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    snapshot_kind TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_path TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    http_status INTEGER,
                    active_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    change_status TEXT NOT NULL DEFAULT 'BASELINE',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_job_kind
                ON job_snapshots(job_id, snapshot_kind, captured_at DESC);

                CREATE TABLE IF NOT EXISTS applications (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'FOUND',
                    updated_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    next_action TEXT NOT NULL DEFAULT 'Review job',
                    applied_at TEXT,
                    follow_up_due TEXT,
                    resume_variant TEXT NOT NULL DEFAULT '',
                    portfolio_project TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS application_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT UNIQUE NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    event_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_events_job_time
                ON application_events(job_id, event_at DESC);

                CREATE TABLE IF NOT EXISTS resume_reviews (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    draft_hash TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    diff_path TEXT NOT NULL DEFAULT '',
                    feedback TEXT NOT NULL DEFAULT '',
                    approved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS job_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL DEFAULT '',
                    canonical_url TEXT NOT NULL DEFAULT '',
                    first_delivered_at TEXT NOT NULL,
                    last_delivered_at TEXT NOT NULL,
                    delivery_channel TEXT NOT NULL DEFAULT 'TELEGRAM',
                    message_ids_json TEXT NOT NULL DEFAULT '[]',
                    data_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_job_deliveries_job_id
                ON job_deliveries(job_id);

                CREATE INDEX IF NOT EXISTS idx_job_deliveries_url
                ON job_deliveries(canonical_url);

                """
            )

    def upsert_job(self, row: dict[str, Any], canonical_key: str) -> str:
        incoming_job_id = str(row["job_id"]).upper().strip()
        if not incoming_job_id:
            raise ValueError("A job_id is required before storing a job.")

        stamp = now_iso()
        with self.connect() as connection:
            # Serialize identity changes so a URL match and a canonical-key match
            # cannot create two owners for the same vacancy.
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT job_id FROM jobs WHERE canonical_key = ?",
                (canonical_key,),
            ).fetchone()
            effective_job_id = str(owner["job_id"]) if owner else incoming_job_id

            if effective_job_id != incoming_job_id:
                self._merge_jobs_in_connection(connection, incoming_job_id, effective_job_id)
                connection.execute(
                    "INSERT OR REPLACE INTO job_aliases(alias, job_id) VALUES (?, ?)",
                    (incoming_job_id, effective_job_id),
                )

            row["job_id"] = effective_job_id
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, canonical_key, company, title, location, remote_type,
                    primary_url, first_seen, last_seen, active_status, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    canonical_key = excluded.canonical_key,
                    company = excluded.company,
                    title = excluded.title,
                    location = excluded.location,
                    remote_type = excluded.remote_type,
                    primary_url = excluded.primary_url,
                    last_seen = excluded.last_seen,
                    data_json = excluded.data_json
                """,
                (
                    effective_job_id,
                    canonical_key,
                    str(row.get("company", "")),
                    str(row.get("title", "")),
                    str(row.get("location", "")),
                    str(row.get("remote_type", "")),
                    str(row.get("canonical_url") or row.get("link", "")),
                    str(row.get("first_seen") or row.get("date_found") or stamp),
                    stamp,
                    str(row.get("active_status") or "UNKNOWN"),
                    payload,
                ),
            )
        return effective_job_id

    def register_alias(self, alias: str, job_id: str) -> None:
        if not alias or alias == job_id:
            return
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO job_aliases(alias, job_id) VALUES (?, ?)",
                (alias.upper(), job_id.upper()),
            )

    def register_source(self, job_id: str, url: str, domain: str, source_type: str) -> None:
        if not url:
            return
        stamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_sources(source_url, job_id, source_domain, source_type, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    job_id = excluded.job_id,
                    source_domain = excluded.source_domain,
                    source_type = excluded.source_type,
                    last_seen = excluded.last_seen
                """,
                (url, job_id, domain, source_type, stamp, stamp),
            )

    def _merge_jobs_in_connection(
        self,
        connection: sqlite3.Connection,
        source_job_id: str,
        target_job_id: str,
    ) -> bool:
        source = source_job_id.upper()
        target = target_job_id.upper()
        if source == target:
            return False
        status_priority = {
            "FOUND": 1,
            "SHORTLISTED": 2,
            "RESUME_CREATED": 3,
            "RESUME_APPROVED": 4,
            "PDF_CREATED": 5,
            "READY_MANUAL_APPLY": 6,
            "APPLIED": 7,
            "FOLLOW_UP": 8,
            "REJECTED": 8,
            "INTERVIEW": 9,
            "OFFER": 10,
            "WITHDRAWN": 10,
        }
        source_job = connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (source,)).fetchone()
        target_job = connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (target,)).fetchone()
        if not source_job or not target_job:
            return False

        source_application = connection.execute(
            "SELECT * FROM applications WHERE job_id = ?", (source,)
        ).fetchone()
        target_application = connection.execute(
            "SELECT * FROM applications WHERE job_id = ?", (target,)
        ).fetchone()
        if source_application and target_application:
            source_status = str(source_application["status"])
            target_status = str(target_application["status"])
            if status_priority.get(source_status, 0) > status_priority.get(target_status, 0):
                connection.execute(
                    """
                    UPDATE applications SET status = ?, updated_at = ?, notes = ?, next_action = ?,
                    applied_at = COALESCE(?, applied_at), follow_up_due = COALESCE(?, follow_up_due),
                    resume_variant = COALESCE(NULLIF(?, ''), resume_variant),
                    portfolio_project = COALESCE(NULLIF(?, ''), portfolio_project)
                    WHERE job_id = ?
                    """,
                    (
                        source_status,
                        source_application["updated_at"],
                        source_application["notes"],
                        source_application["next_action"],
                        source_application["applied_at"],
                        source_application["follow_up_due"],
                        source_application["resume_variant"],
                        source_application["portfolio_project"],
                        target,
                    ),
                )
            connection.execute("DELETE FROM applications WHERE job_id = ?", (source,))
        elif source_application:
            connection.execute("UPDATE applications SET job_id = ? WHERE job_id = ?", (target, source))

        connection.execute(
            "DELETE FROM job_sources WHERE job_id = ? AND source_url IN (SELECT source_url FROM job_sources WHERE job_id = ?)",
            (source, target),
        )
        connection.execute("UPDATE job_sources SET job_id = ? WHERE job_id = ?", (target, source))
        connection.execute("UPDATE job_snapshots SET job_id = ? WHERE job_id = ?", (target, source))
        connection.execute("UPDATE application_events SET job_id = ? WHERE job_id = ?", (target, source))
        connection.execute("UPDATE job_deliveries SET job_id = ? WHERE job_id = ?", (target, source))
        target_review = connection.execute("SELECT 1 FROM resume_reviews WHERE job_id = ?", (target,)).fetchone()
        if target_review:
            connection.execute("DELETE FROM resume_reviews WHERE job_id = ?", (source,))
        else:
            connection.execute("UPDATE resume_reviews SET job_id = ? WHERE job_id = ?", (target, source))
        connection.execute("UPDATE job_aliases SET job_id = ? WHERE job_id = ?", (target, source))
        connection.execute("DELETE FROM jobs WHERE job_id = ?", (source,))
        connection.execute(
            "INSERT OR REPLACE INTO job_aliases(alias, job_id) VALUES (?, ?)",
            (source, target),
        )
        return True

    def merge_jobs(self, source_job_id: str, target_job_id: str) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._merge_jobs_in_connection(connection, source_job_id, target_job_id)

    def resolve_job_id(self, value: str) -> str | None:
        target = value.upper().strip()
        with self.connect() as connection:
            direct = connection.execute("SELECT job_id FROM jobs WHERE job_id = ?", (target,)).fetchone()
            if direct:
                return str(direct["job_id"])
            alias = connection.execute("SELECT job_id FROM job_aliases WHERE alias = ?", (target,)).fetchone()
            return str(alias["job_id"]) if alias else None

    def list_aliases(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT alias, job_id FROM job_aliases ORDER BY alias").fetchall()
        return {str(row["alias"]): str(row["job_id"]) for row in rows}

    def job_id_for_source(self, url: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT job_id FROM job_sources WHERE source_url = ?", (url,)).fetchone()
            return str(row["job_id"]) if row else None

    def job_id_for_canonical_key(self, canonical_key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT job_id FROM jobs WHERE canonical_key = ?", (canonical_key,)).fetchone()
            return str(row["job_id"]) if row else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        resolved = self.resolve_job_id(job_id) or job_id.upper()
        with self.connect() as connection:
            row = connection.execute("SELECT data_json FROM jobs WHERE job_id = ?", (resolved,)).fetchone()
        return json.loads(row["data_json"]) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT data_json FROM jobs ORDER BY last_seen DESC").fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def add_snapshot(
        self,
        job_id: str,
        snapshot_kind: str,
        content_hash: str,
        content_path: str,
        source_url: str = "",
        http_status: int | None = None,
        active_status: str = "UNKNOWN",
        change_status: str = "BASELINE",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_snapshots(
                    job_id, snapshot_kind, captured_at, content_hash, content_path,
                    source_url, http_status, active_status, change_status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    snapshot_kind,
                    now_iso(),
                    content_hash,
                    content_path,
                    source_url,
                    http_status,
                    active_status,
                    change_status,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            connection.execute(
                "UPDATE jobs SET active_status = ?, last_seen = ? WHERE job_id = ?",
                (active_status, now_iso(), job_id),
            )

    def latest_snapshot(self, job_id: str, snapshot_kind: str) -> dict[str, Any] | None:
        resolved = self.resolve_job_id(job_id) or job_id
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM job_snapshots
                WHERE job_id = ? AND snapshot_kind = ?
                ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1
                """,
                (resolved, snapshot_kind),
            ).fetchone()
        return dict(row) if row else None

    def ensure_application(self, job_id: str, portfolio_project: str = "") -> None:
        stamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO applications(
                    job_id, status, updated_at, next_action, portfolio_project
                ) VALUES (?, 'FOUND', ?, 'Review job', ?)
                """,
                (job_id, stamp, portfolio_project),
            )

    def set_application_status(
        self,
        job_id: str,
        status: str,
        notes: str = "",
        source: str = "",
        next_action: str = "",
        event_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        target = self.resolve_job_id(job_id) or job_id.upper()
        status = status.upper()
        if status not in VALID_APPLICATION_STATUSES:
            raise ValueError(f"Invalid application status: {status}")
        stamp = now_iso()
        event_key = event_key or str(uuid.uuid4())
        with self.connect() as connection:
            existing_event = connection.execute(
                "SELECT 1 FROM application_events WHERE event_key = ?", (event_key,)
            ).fetchone()
            if existing_event:
                return False
            row = connection.execute(
                "SELECT status FROM applications WHERE job_id = ?", (target,)
            ).fetchone()
            previous = str(row["status"]) if row else ""
            if not row:
                connection.execute(
                    "INSERT INTO applications(job_id, status, updated_at, notes, next_action) VALUES (?, ?, ?, ?, ?)",
                    (target, status, stamp, notes, next_action),
                )
            else:
                connection.execute(
                    """
                    UPDATE applications SET status = ?, updated_at = ?, notes = ?,
                    next_action = COALESCE(NULLIF(?, ''), next_action),
                    applied_at = CASE WHEN ? = 'APPLIED' THEN COALESCE(applied_at, ?) ELSE applied_at END
                    WHERE job_id = ?
                    """,
                    (status, stamp, notes, next_action, status, stamp, target),
                )
            connection.execute(
                """
                INSERT INTO application_events(
                    event_key, job_id, event_type, from_status, to_status,
                    event_at, source, notes, metadata_json
                ) VALUES (?, ?, 'STATUS_CHANGED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    target,
                    previous,
                    status,
                    stamp,
                    source,
                    notes,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
        return True

    def get_application(self, job_id: str) -> dict[str, Any] | None:
        target = self.resolve_job_id(job_id) or job_id.upper()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM applications WHERE job_id = ?", (target,)).fetchone()
        return dict(row) if row else None

    def update_application_fields(self, job_id: str, **fields: Any) -> None:
        allowed = {"notes", "next_action", "applied_at", "follow_up_due", "resume_variant", "portfolio_project"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        target = self.resolve_job_id(job_id) or job_id.upper()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [updates[key] for key in updates]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE applications SET {assignments}, updated_at = ? WHERE job_id = ?",
                (*values, now_iso(), target),
            )

    def list_applications(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, j.company, j.title, j.primary_url, j.data_json
                FROM applications a JOIN jobs j ON j.job_id = a.job_id
                ORDER BY a.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_resume_review(
        self,
        job_id: str,
        draft_hash: str,
        status: str,
        diff_path: str = "",
        feedback: str = "",
    ) -> None:
        target = self.resolve_job_id(job_id) or job_id.upper()
        stamp = now_iso()
        approved_at = stamp if status == "APPROVED" else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO resume_reviews(
                    job_id, draft_hash, review_status, created_at, updated_at,
                    diff_path, feedback, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    draft_hash = excluded.draft_hash,
                    review_status = excluded.review_status,
                    updated_at = excluded.updated_at,
                    diff_path = excluded.diff_path,
                    feedback = excluded.feedback,
                    approved_at = excluded.approved_at
                """,
                (target, draft_hash, status, stamp, stamp, diff_path, feedback, approved_at),
            )

    def get_resume_review(self, job_id: str) -> dict[str, Any] | None:
        target = self.resolve_job_id(job_id) or job_id.upper()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM resume_reviews WHERE job_id = ?", (target,)).fetchone()
        return dict(row) if row else None

    def was_job_delivered(self, job_id: str, delivery_key: str, canonical_url: str = "") -> bool:
        raw_id = str(job_id or "").upper().strip()
        target = self.resolve_job_id(raw_id) or raw_id
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM job_deliveries
                WHERE delivery_key = ?
                   OR (? <> '' AND job_id IN (?, ?))
                   OR (? <> '' AND canonical_url = ?)
                LIMIT 1
                """,
                (delivery_key, target, target, raw_id, canonical_url, canonical_url),
            ).fetchone()
        return row is not None

    def record_job_delivery(
        self,
        *,
        job_id: str,
        delivery_key: str,
        canonical_url: str = "",
        channel: str = "TELEGRAM",
        message_ids: list[int] | None = None,
        data: dict[str, Any] | None = None,
    ) -> bool:
        raw_id = str(job_id or "").upper().strip()
        target = self.resolve_job_id(raw_id) or raw_id
        stamp = now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO job_deliveries(
                    delivery_key, job_id, canonical_url, first_delivered_at,
                    last_delivered_at, delivery_channel, message_ids_json, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_key,
                    target,
                    canonical_url,
                    stamp,
                    stamp,
                    str(channel or "TELEGRAM").upper(),
                    json.dumps(message_ids or []),
                    json.dumps(data or {}, ensure_ascii=False),
                ),
            )
        return cursor.rowcount > 0

    def claim_job_delivery(
        self,
        *,
        job_id: str,
        delivery_key: str,
        canonical_url: str = "",
        channel: str = "TELEGRAM",
        data: dict[str, Any] | None = None,
    ) -> bool:
        raw_id = str(job_id or "").upper().strip()
        target = self.resolve_job_id(raw_id) or raw_id
        stamp = now_iso()
        stale_cutoff = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM job_deliveries WHERE delivery_channel LIKE 'RESERVED:%' AND first_delivered_at < ?",
                (stale_cutoff,),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO job_deliveries(
                    delivery_key, job_id, canonical_url, first_delivered_at,
                    last_delivered_at, delivery_channel, message_ids_json, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?)
                """,
                (
                    delivery_key,
                    target,
                    canonical_url,
                    stamp,
                    stamp,
                    f"RESERVED:{str(channel or 'TELEGRAM').upper()}",
                    json.dumps(data or {}, ensure_ascii=False),
                ),
            )
        return cursor.rowcount > 0

    def finalize_job_delivery(self, delivery_key: str, channel: str, message_ids: list[int] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE job_deliveries
                SET delivery_channel = ?, message_ids_json = ?, last_delivered_at = ?
                WHERE delivery_key = ? AND delivery_channel LIKE 'RESERVED:%'
                """,
                (str(channel or "TELEGRAM").upper(), json.dumps(message_ids or []), now_iso(), delivery_key),
            )

    def release_job_delivery(self, delivery_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM job_deliveries WHERE delivery_key = ? AND delivery_channel LIKE 'RESERVED:%'",
                (delivery_key,),
            )

    def list_job_deliveries(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM job_deliveries ORDER BY first_delivered_at DESC"
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (max(0, int(limit)),)
        with self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]
