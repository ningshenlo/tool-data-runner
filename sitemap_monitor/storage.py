from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .comparability import (
    resource_family_counts_json,
    resource_manifest_json,
    stored_site_scan_from_row,
)
from .models import (
    CheckOutcome,
    ComparabilityResult,
    DueSite,
    ResourceState,
    SitemapJob,
    SiteScanSnapshot,
    StoredSiteScan,
)


def stable_id(prefix: str, *parts: str, length: int = 32) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:length]}"


def site_id_for(homepage_url: str) -> str:
    hostname = (urlsplit(homepage_url).hostname or "").lower()
    return stable_id("site", hostname, length=24)


def resource_id_for(site_id: str, url: str) -> str:
    return stable_id("smr", site_id, url)


def job_id_for(site_id: str, scheduled_for: int, schedule_version: int) -> str:
    return stable_id("smjob", site_id, str(scheduled_for), str(schedule_version))


class ObjectStore(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, body: bytes, content_type: str) -> None: ...


class MetadataStore(Protocol):
    async def ensure_site(
        self,
        site_id: str,
        homepage_url: str,
        now_ms: int,
        *,
        check_interval_sec: int | None = None,
    ) -> None: ...

    async def get_resource(self, site_id: str, url: str) -> ResourceState | None: ...

    async def save_resource(
        self,
        site_id: str,
        state: ResourceState,
        *,
        canonical_fetch_url: str,
        parent_id: str | None,
        checked_at_ms: int,
        changed: bool,
    ) -> None: ...

    async def mark_not_modified(
        self,
        site_id: str,
        resource_id: str,
        *,
        canonical_fetch_url: str,
        checked_at_ms: int,
        etag: str | None,
        last_modified: str | None,
    ) -> None: ...

    async def mark_failure(
        self,
        site_id: str,
        resource_id: str,
        url: str,
        *,
        canonical_fetch_url: str,
        parent_id: str | None,
        checked_at_ms: int,
        missing: bool,
    ) -> None: ...

    async def record_run(
        self,
        site_id: str,
        outcome: CheckOutcome,
        *,
        finished_at_ms: int,
        run_key: str,
        started_at_ms: int,
        site_scan_id: str | None = None,
    ) -> None: ...


class SchedulerStore(MetadataStore, Protocol):
    async def ensure_site(
        self,
        site_id: str,
        homepage_url: str,
        now_ms: int,
        *,
        check_interval_sec: int | None = None,
    ) -> None: ...

    async def claim_due_sites(
        self,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        limit: int,
        now_ms: int,
    ) -> tuple[DueSite, ...]: ...

    async def ensure_job(
        self,
        due_site: DueSite,
        *,
        max_attempts: int,
        now_ms: int,
    ) -> SitemapJob | None: ...

    async def claim_jobs(
        self,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        limit: int,
        now_ms: int,
    ) -> tuple[SitemapJob, ...]: ...

    async def renew_job_lease(
        self,
        job: SitemapJob,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        now_ms: int,
    ) -> bool: ...

    async def finish_job_success(
        self,
        job: SitemapJob,
        *,
        finished_at_ms: int,
        next_check_at_ms: int,
        comparability: ComparabilityResult | None = None,
        site_scan: SiteScanSnapshot | None = None,
    ) -> bool: ...

    async def finish_job_failure(
        self,
        job: SitemapJob,
        *,
        available_at_ms: int,
        dead: bool,
        error: str,
        finished_at_ms: int,
        next_check_at_ms: int | None,
        comparability: ComparabilityResult | None = None,
        site_scan: SiteScanSnapshot | None = None,
    ) -> bool: ...

    async def get_site_scan_context(
        self,
        site_id: str,
    ) -> tuple[StoredSiteScan | None, StoredSiteScan | None]: ...

def _safe_object_key(key: str) -> str:
    normalized = key.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        raise ValueError("Object key must be a safe relative path.")
    return normalized


class FileObjectStore:
    """Filesystem implementation mirroring the R2 object-key layout."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        relative = _safe_object_key(key)
        path = (self.root / relative).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("Object key escapes the configured object root.")
        return path

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(body)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class SqliteMetadataStore:
    """Local D1-compatible metadata store for development and offline validation."""

    def __init__(self, database_path: str | Path):
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        self.connection.executescript(schema)
        self._migrate_local_schema()

    def _migrate_local_schema(self) -> None:
        """Keep pre-release local state usable as the Phase 1 schema grows."""

        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(sitemap_sites)")
        }
        additions = {
            "schedule_version": "INTEGER NOT NULL DEFAULT 1",
            "last_attempt_at": "INTEGER",
            "last_success_at": "INTEGER",
            "dispatch_lease_owner": "TEXT",
            "dispatch_lease_token": "TEXT",
            "dispatch_lease_expires_at": "INTEGER",
            "semantic_baseline_scan_id": "TEXT",
        }
        with self.connection:
            for name, definition in additions.items():
                if name not in existing:
                    self.connection.execute(
                        f"ALTER TABLE sitemap_sites ADD COLUMN {name} {definition}"
                    )
            job_columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(sitemap_jobs)")
            }
            if "base_error_streak" not in job_columns:
                self.connection.execute(
                    "ALTER TABLE sitemap_jobs ADD COLUMN "
                    "base_error_streak INTEGER NOT NULL DEFAULT 0"
                )
            run_columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(sitemap_runs)")
            }
            for name in ("state_key", "site_scan_id"):
                if name not in run_columns:
                    self.connection.execute(
                        f"ALTER TABLE sitemap_runs ADD COLUMN {name} TEXT"
                    )

    def close(self) -> None:
        self.connection.close()

    async def ensure_site(
        self,
        site_id: str,
        homepage_url: str,
        now_ms: int,
        *,
        check_interval_sec: int | None = None,
    ) -> None:
        domain = (urlsplit(homepage_url).hostname or "").lower()
        interval = check_interval_sec or 3_600
        if interval <= 0:
            raise ValueError("check_interval_sec must be positive.")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sitemap_sites (
                    id, domain, homepage_url, check_interval_sec, next_check_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    homepage_url = excluded.homepage_url,
                    check_interval_sec = CASE
                        WHEN ? IS NULL THEN sitemap_sites.check_interval_sec
                        ELSE excluded.check_interval_sec
                    END,
                    schedule_version = schedule_version + CASE
                        WHEN homepage_url <> excluded.homepage_url THEN 1
                        WHEN ? IS NOT NULL
                         AND check_interval_sec <> excluded.check_interval_sec THEN 1
                        ELSE 0
                    END,
                    next_check_at = CASE
                        WHEN homepage_url <> excluded.homepage_url THEN excluded.next_check_at
                        WHEN ? IS NOT NULL
                         AND check_interval_sec <> excluded.check_interval_sec
                            THEN excluded.next_check_at
                        ELSE sitemap_sites.next_check_at
                    END,
                    dispatch_lease_owner = CASE
                        WHEN homepage_url <> excluded.homepage_url THEN NULL
                        WHEN ? IS NOT NULL
                         AND check_interval_sec <> excluded.check_interval_sec THEN NULL
                        ELSE dispatch_lease_owner
                    END,
                    dispatch_lease_token = CASE
                        WHEN homepage_url <> excluded.homepage_url THEN NULL
                        WHEN ? IS NOT NULL
                         AND check_interval_sec <> excluded.check_interval_sec THEN NULL
                        ELSE dispatch_lease_token
                    END,
                    dispatch_lease_expires_at = CASE
                        WHEN homepage_url <> excluded.homepage_url THEN NULL
                        WHEN ? IS NOT NULL
                         AND check_interval_sec <> excluded.check_interval_sec THEN NULL
                        ELSE dispatch_lease_expires_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    site_id,
                    domain,
                    homepage_url,
                    interval,
                    now_ms,
                    now_ms,
                    now_ms,
                    check_interval_sec,
                    check_interval_sec,
                    check_interval_sec,
                    check_interval_sec,
                    check_interval_sec,
                    check_interval_sec,
                ),
            )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> SitemapJob:
        return SitemapJob(
            attempts=int(row["attempts"]),
            check_interval_sec=int(row["check_interval_sec"]),
            homepage_url=str(row["homepage_url"]),
            idempotency_key=str(row["idempotency_key"]),
            job_id=str(row["id"]),
            lease_token=str(row["lease_token"] or ""),
            max_attempts=int(row["max_attempts"]),
            schedule_version=int(row["schedule_version"]),
            scheduled_for=int(row["scheduled_for"]),
            site_id=str(row["site_id"]),
            status=row["status"],
        )

    async def claim_due_sites(
        self,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        limit: int,
        now_ms: int,
    ) -> tuple[DueSite, ...]:
        if lease_duration_ms <= 0 or limit <= 0 or not lease_owner:
            raise ValueError("A positive lease, limit, and lease owner are required.")
        expires_at = now_ms + lease_duration_ms
        with self.connection:
            rows = self.connection.execute(
                """
                UPDATE sitemap_sites
                SET dispatch_lease_owner = ?,
                    dispatch_lease_token = lower(hex(randomblob(16))),
                    dispatch_lease_expires_at = ?,
                    updated_at = ?
                WHERE id IN (
                    SELECT id
                    FROM sitemap_sites
                    WHERE status = 'active'
                      AND next_check_at <= ?
                      AND (
                        dispatch_lease_expires_at IS NULL
                        OR dispatch_lease_expires_at <= ?
                      )
                    ORDER BY next_check_at, id
                    LIMIT ?
                )
                RETURNING id, homepage_url, check_interval_sec, schedule_version,
                          next_check_at, dispatch_lease_token
                """,
                (lease_owner, expires_at, now_ms, now_ms, now_ms, limit),
            ).fetchall()
        return tuple(
            DueSite(
                check_interval_sec=int(row["check_interval_sec"]),
                dispatch_lease_token=str(row["dispatch_lease_token"]),
                homepage_url=str(row["homepage_url"]),
                schedule_version=int(row["schedule_version"]),
                scheduled_for=int(row["next_check_at"]),
                site_id=str(row["id"]),
            )
            for row in rows
        )

    async def ensure_job(
        self,
        due_site: DueSite,
        *,
        max_attempts: int,
        now_ms: int,
    ) -> SitemapJob | None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive.")
        job_id = job_id_for(
            due_site.site_id,
            due_site.scheduled_for,
            due_site.schedule_version,
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sitemap_jobs (
                    id, idempotency_key, site_id, scheduled_for,
                    schedule_version, status, attempts, max_attempts,
                    base_error_streak, available_at, created_at, updated_at
                )
                SELECT ?, ?, id, ?, ?, 'pending', 0, ?, error_streak, ?, ?, ?
                FROM sitemap_sites
                WHERE id = ? AND schedule_version = ?
                  AND dispatch_lease_token = ?
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    job_id,
                    job_id,
                    due_site.scheduled_for,
                    due_site.schedule_version,
                    max_attempts,
                    now_ms,
                    now_ms,
                    now_ms,
                    due_site.site_id,
                    due_site.schedule_version,
                    due_site.dispatch_lease_token,
                ),
            )
            row = self.connection.execute(
                """
                SELECT job.*, site.homepage_url, site.check_interval_sec
                FROM sitemap_jobs job
                JOIN sitemap_sites site ON site.id = job.site_id
                WHERE job.idempotency_key = ?
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return self._job_from_row(row) if row is not None else None

    async def claim_jobs(
        self,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        limit: int,
        now_ms: int,
    ) -> tuple[SitemapJob, ...]:
        if lease_duration_ms <= 0 or limit <= 0 or not lease_owner:
            raise ValueError("A positive lease, limit, and lease owner are required.")
        expires_at = now_ms + lease_duration_ms
        with self.connection:
            claimed = self.connection.execute(
                """
                UPDATE sitemap_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    lease_token = lower(hex(randomblob(16))),
                    lease_expires_at = ?,
                    started_at = ?,
                    finished_at = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE id IN (
                    SELECT job.id
                    FROM sitemap_jobs job
                    JOIN sitemap_sites site ON site.id = job.site_id
                    WHERE site.status = 'active'
                      AND site.schedule_version = job.schedule_version
                      AND (
                        (
                          job.status IN ('pending', 'retry')
                          AND job.attempts < job.max_attempts
                          AND job.available_at <= ?
                        )
                        OR (
                          job.status = 'running'
                          AND job.lease_expires_at IS NOT NULL
                          AND job.lease_expires_at <= ?
                        )
                      )
                    ORDER BY job.available_at, job.scheduled_for, job.id
                    LIMIT ?
                )
                RETURNING *
                """,
                (
                    lease_owner,
                    expires_at,
                    now_ms,
                    now_ms,
                    now_ms,
                    now_ms,
                    limit,
                ),
            ).fetchall()
            rows: list[sqlite3.Row] = []
            for job in claimed:
                row = self.connection.execute(
                    """
                    SELECT job.*, site.homepage_url, site.check_interval_sec
                    FROM sitemap_jobs job
                    JOIN sitemap_sites site ON site.id = job.site_id
                    WHERE job.id = ? AND job.lease_token = ?
                    """,
                    (job["id"], job["lease_token"]),
                ).fetchone()
                if row is not None:
                    rows.append(row)
        return tuple(self._job_from_row(row) for row in rows)

    async def renew_job_lease(
        self,
        job: SitemapJob,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        now_ms: int,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE sitemap_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token = ?
                """,
                (
                    now_ms + lease_duration_ms,
                    now_ms,
                    job.job_id,
                    lease_owner,
                    job.lease_token,
                ),
            )
        return cursor.rowcount == 1

    def _insert_site_scan(
        self,
        job: SitemapJob,
        site_scan: SiteScanSnapshot,
        comparability: ComparabilityResult,
        *,
        committed_at_ms: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO sitemap_site_scans (
                id, job_id, site_id, attempt, baseline_scan_id,
                started_at, finished_at, discovery_mode, traversal_complete,
                traversal_reason_codes_json, attempted_resource_count,
                successful_resource_count, successful_resource_ratio,
                resource_count_before, resource_count_after,
                url_count_before, url_count_after,
                raw_resource_set_hash_before, raw_resource_set_hash_after,
                normalized_resource_set_hash_before,
                normalized_resource_set_hash_after,
                resource_family_counts_json, resource_manifest_json,
                coverage_ratio, comparability_status, is_comparable,
                reason_codes_json, policy_version, is_committed,
                promoted_semantic_baseline, committed_at, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?
            )
            ON CONFLICT(id) DO NOTHING
            """,
            (
                site_scan.scan_id,
                job.job_id,
                job.site_id,
                job.attempts,
                comparability.baseline_scan_id,
                site_scan.started_at_ms,
                site_scan.finished_at_ms,
                site_scan.discovery_mode,
                int(site_scan.traversal_complete),
                json.dumps(site_scan.traversal_reason_codes, separators=(",", ":")),
                site_scan.attempted_resource_count,
                site_scan.successful_resource_count,
                site_scan.successful_resource_ratio,
                comparability.resource_count_before,
                comparability.resource_count_after,
                comparability.url_count_before,
                comparability.url_count_after,
                comparability.raw_resource_set_hash_before,
                comparability.raw_resource_set_hash_after,
                comparability.normalized_resource_set_hash_before,
                comparability.normalized_resource_set_hash_after,
                resource_family_counts_json(site_scan),
                resource_manifest_json(site_scan),
                comparability.coverage_ratio,
                comparability.status,
                int(comparability.is_comparable),
                json.dumps(comparability.reason_codes, separators=(",", ":")),
                comparability.policy_version,
                int(comparability.promote_semantic_baseline),
                committed_at_ms,
                committed_at_ms,
            ),
        )

    async def get_site_scan_context(
        self,
        site_id: str,
    ) -> tuple[StoredSiteScan | None, StoredSiteScan | None]:
        baseline_row = self.connection.execute(
            """
            SELECT scan.*
            FROM sitemap_sites site
            JOIN sitemap_site_scans scan
              ON scan.id = site.semantic_baseline_scan_id
            WHERE site.id = ? AND scan.is_committed = 1
            LIMIT 1
            """,
            (site_id,),
        ).fetchone()
        previous_row = self.connection.execute(
            """
            SELECT *
            FROM sitemap_site_scans
            WHERE site_id = ? AND is_committed = 1
            ORDER BY committed_at DESC, finished_at DESC, id DESC
            LIMIT 1
            """,
            (site_id,),
        ).fetchone()
        baseline = (
            stored_site_scan_from_row(dict(baseline_row))
            if baseline_row is not None
            else None
        )
        previous = (
            stored_site_scan_from_row(dict(previous_row))
            if previous_row is not None
            else None
        )
        return baseline, previous

    async def finish_job_success(
        self,
        job: SitemapJob,
        *,
        finished_at_ms: int,
        next_check_at_ms: int,
        comparability: ComparabilityResult | None = None,
        site_scan: SiteScanSnapshot | None = None,
    ) -> bool:
        if (site_scan is None) != (comparability is None):
            raise ValueError("site_scan and comparability must be provided together.")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE sitemap_jobs
                SET status = 'succeeded', finished_at = ?, last_error = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (finished_at_ms, finished_at_ms, job.job_id, job.lease_token),
            )
            if cursor.rowcount != 1:
                return False
            if site_scan is not None and comparability is not None:
                self._insert_site_scan(
                    job,
                    site_scan,
                    comparability,
                    committed_at_ms=finished_at_ms,
                )
            self.connection.execute(
                """
                UPDATE sitemap_sites
                SET next_check_at = ?, last_attempt_at = ?, last_success_at = ?,
                    error_streak = 0, dispatch_lease_owner = NULL,
                    dispatch_lease_token = NULL,
                    dispatch_lease_expires_at = NULL,
                    semantic_baseline_scan_id = CASE
                        WHEN ? THEN ? ELSE semantic_baseline_scan_id
                    END,
                    updated_at = ?
                WHERE id = ? AND schedule_version = ?
                """,
                (
                    next_check_at_ms,
                    finished_at_ms,
                    finished_at_ms,
                    int(bool(comparability and comparability.promote_semantic_baseline)),
                    site_scan.scan_id if site_scan is not None else None,
                    finished_at_ms,
                    job.site_id,
                    job.schedule_version,
                ),
            )
        return True

    async def finish_job_failure(
        self,
        job: SitemapJob,
        *,
        available_at_ms: int,
        dead: bool,
        error: str,
        finished_at_ms: int,
        next_check_at_ms: int | None,
        comparability: ComparabilityResult | None = None,
        site_scan: SiteScanSnapshot | None = None,
    ) -> bool:
        if (site_scan is None) != (comparability is None):
            raise ValueError("site_scan and comparability must be provided together.")
        status = "dead" if dead else "retry"
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE sitemap_jobs
                SET status = ?, available_at = ?, finished_at = ?,
                    last_error = ?, dead_letter_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (
                    status,
                    available_at_ms,
                    finished_at_ms,
                    error[:2_000],
                    finished_at_ms if dead else None,
                    finished_at_ms,
                    job.job_id,
                    job.lease_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if site_scan is not None and comparability is not None:
                self._insert_site_scan(
                    job,
                    site_scan,
                    comparability,
                    committed_at_ms=finished_at_ms,
                )
            if dead:
                if next_check_at_ms is None:
                    raise ValueError("A dead job requires a future site check time.")
                self.connection.execute(
                    """
                    UPDATE sitemap_sites
                    SET next_check_at = ?, last_attempt_at = ?,
                        error_streak = MAX(
                            error_streak,
                            COALESCE((
                                SELECT base_error_streak + attempts
                                FROM sitemap_jobs WHERE id = ?
                            ), error_streak)
                        ),
                        dispatch_lease_owner = NULL,
                        dispatch_lease_token = NULL,
                        dispatch_lease_expires_at = NULL, updated_at = ?
                    WHERE id = ? AND schedule_version = ?
                    """,
                    (
                        next_check_at_ms,
                        finished_at_ms,
                        job.job_id,
                        finished_at_ms,
                        job.site_id,
                        job.schedule_version,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE sitemap_sites
                    SET last_attempt_at = ?,
                        error_streak = MAX(
                            error_streak,
                            COALESCE((
                                SELECT base_error_streak + attempts
                                FROM sitemap_jobs WHERE id = ?
                            ), error_streak)
                        ),
                        dispatch_lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND schedule_version = ?
                    """,
                    (
                        finished_at_ms,
                        job.job_id,
                        available_at_ms,
                        finished_at_ms,
                        job.site_id,
                        job.schedule_version,
                    ),
                )
        return True

    async def get_resource(self, site_id: str, url: str) -> ResourceState | None:
        row = self.connection.execute(
            """
            SELECT id, url, type, etag, http_last_modified, content_hash,
                   urlset_hash, metadata_hash, url_count, current_state_key
            FROM sitemap_resources
            WHERE site_id = ? AND url = ? AND current_state_key IS NOT NULL
            """,
            (site_id, url),
        ).fetchone()
        if row is None:
            return None
        return ResourceState(
            content_hash=str(row["content_hash"]),
            current_state_key=str(row["current_state_key"]),
            etag=row["etag"],
            last_modified=row["http_last_modified"],
            metadata_hash=str(row["metadata_hash"]),
            resource_id=str(row["id"]),
            sitemap_kind=row["type"],
            url=str(row["url"]),
            url_count=int(row["url_count"]),
            urlset_hash=str(row["urlset_hash"]),
        )

    async def save_resource(
        self,
        site_id: str,
        state: ResourceState,
        *,
        canonical_fetch_url: str,
        parent_id: str | None,
        checked_at_ms: int,
        changed: bool,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sitemap_resources (
                    id, site_id, parent_id, url, canonical_fetch_url, type,
                    etag, http_last_modified, content_hash, urlset_hash,
                    metadata_hash, url_count, current_state_key,
                    last_checked_at, last_changed_at, missing_streak,
                    error_streak, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(site_id, url) DO UPDATE SET
                    parent_id = COALESCE(excluded.parent_id, sitemap_resources.parent_id),
                    canonical_fetch_url = excluded.canonical_fetch_url,
                    type = excluded.type,
                    etag = excluded.etag,
                    http_last_modified = excluded.http_last_modified,
                    content_hash = excluded.content_hash,
                    urlset_hash = excluded.urlset_hash,
                    metadata_hash = excluded.metadata_hash,
                    url_count = excluded.url_count,
                    current_state_key = excluded.current_state_key,
                    last_checked_at = excluded.last_checked_at,
                    last_changed_at = CASE
                        WHEN ? THEN excluded.last_changed_at
                        ELSE sitemap_resources.last_changed_at
                    END,
                    missing_streak = 0,
                    error_streak = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    state.resource_id,
                    site_id,
                    parent_id,
                    state.url,
                    canonical_fetch_url,
                    state.sitemap_kind,
                    state.etag,
                    state.last_modified,
                    state.content_hash,
                    state.urlset_hash,
                    state.metadata_hash,
                    state.url_count,
                    state.current_state_key,
                    checked_at_ms,
                    checked_at_ms if changed else None,
                    checked_at_ms,
                    checked_at_ms,
                    int(changed),
                ),
            )
            self.connection.execute(
                """
                UPDATE sitemap_sites
                SET last_checked_at = ?,
                    last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (checked_at_ms, int(changed), checked_at_ms, checked_at_ms, site_id),
            )

    async def mark_not_modified(
        self,
        site_id: str,
        resource_id: str,
        *,
        canonical_fetch_url: str,
        checked_at_ms: int,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE sitemap_resources
                SET canonical_fetch_url = ?, etag = COALESCE(?, etag),
                    http_last_modified = COALESCE(?, http_last_modified),
                    last_checked_at = ?, missing_streak = 0, error_streak = 0,
                    updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (
                    canonical_fetch_url,
                    etag,
                    last_modified,
                    checked_at_ms,
                    checked_at_ms,
                    resource_id,
                    site_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE sitemap_sites
                SET last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (checked_at_ms, checked_at_ms, site_id),
            )

    async def mark_failure(
        self,
        site_id: str,
        resource_id: str,
        url: str,
        *,
        canonical_fetch_url: str,
        parent_id: str | None,
        checked_at_ms: int,
        missing: bool,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sitemap_resources (
                    id, site_id, parent_id, url, canonical_fetch_url,
                    missing_streak, error_streak, created_at, updated_at,
                    last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id, url) DO UPDATE SET
                    parent_id = COALESCE(excluded.parent_id, sitemap_resources.parent_id),
                    canonical_fetch_url = excluded.canonical_fetch_url,
                    missing_streak = sitemap_resources.missing_streak + ?,
                    error_streak = sitemap_resources.error_streak + ?,
                    last_checked_at = excluded.last_checked_at,
                    updated_at = excluded.updated_at
                """,
                (
                    resource_id,
                    site_id,
                    parent_id,
                    url,
                    canonical_fetch_url,
                    int(missing),
                    int(not missing),
                    checked_at_ms,
                    checked_at_ms,
                    checked_at_ms,
                    int(missing),
                    int(not missing),
                ),
            )
            self.connection.execute(
                """
                UPDATE sitemap_sites
                SET last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (checked_at_ms, checked_at_ms, site_id),
            )

    async def record_run(
        self,
        site_id: str,
        outcome: CheckOutcome,
        *,
        finished_at_ms: int,
        run_key: str,
        started_at_ms: int,
        site_scan_id: str | None = None,
    ) -> None:
        run_id = stable_id("smrun", run_key)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sitemap_runs (
                    id, run_key, site_id, resource_id, started_at, finished_at,
                    http_status, bytes_downloaded, url_count, result, error_code,
                    added_count, removed_count, modified_count, diff_key,
                    state_key, site_scan_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO NOTHING
                """,
                (
                    run_id,
                    run_key,
                    site_id,
                    outcome.resource_id,
                    started_at_ms,
                    finished_at_ms,
                    outcome.http_status,
                    outcome.bytes_downloaded,
                    outcome.url_count,
                    outcome.result,
                    outcome.error_code,
                    outcome.added_count,
                    outcome.removed_count,
                    outcome.modified_count,
                    outcome.diff_key,
                    outcome.state_key,
                    site_scan_id,
                    finished_at_ms,
                ),
            )
