from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

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
from .storage import _safe_object_key, job_id_for, stable_id


class CloudflareApiError(RuntimeError):
    pass


class CloudflareD1Client:
    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        database_id: str,
        timeout_seconds: float = 30.0,
    ):
        if not account_id or not api_token or not database_id:
            raise ValueError("Cloudflare D1 account, database, and token are required.")
        self.url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _response_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:500].strip() or response.reason_phrase
        if not isinstance(payload, dict):
            return str(payload)[:500]
        errors = payload.get("errors")
        if isinstance(errors, list):
            messages = [
                str(error.get("message") or error.get("code") or "unknown error")
                for error in errors
                if isinstance(error, dict)
            ]
            if messages:
                return "; ".join(messages)[:500]
        return str(payload.get("message") or payload)[:500]

    async def _request(self, body: object) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.post(self.url, headers=self.headers, json=body)
                if response.status_code not in {429, 502, 503, 504}:
                    if response.is_error:
                        raise CloudflareApiError(
                            f"Cloudflare D1 HTTP {response.status_code}: "
                            f"{self._response_error_detail(response)}"
                        )
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("success") is not True:
                        raise CloudflareApiError(
                            "Cloudflare D1 request was unsuccessful: "
                            f"{self._response_error_detail(response)}"
                        )
                    results = payload.get("result")
                    if not isinstance(results, list) or any(
                        not isinstance(result, dict) or result.get("success") is not True
                        for result in results
                    ):
                        raise CloudflareApiError("Cloudflare D1 returned an invalid query result.")
                    return results
                last_error = CloudflareApiError(f"Cloudflare D1 transient HTTP {response.status_code}.")
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError, CloudflareApiError) as error:
                last_error = error
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code not in {
                    429,
                    502,
                    503,
                    504,
                }:
                    break
                if isinstance(error, CloudflareApiError) and "transient" not in str(error):
                    break
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise CloudflareApiError(f"Cloudflare D1 request failed: {last_error}.") from last_error

    async def query(self, sql: str, params: list[object] | None = None) -> dict[str, Any]:
        results = await self._request({"sql": sql, "params": params or []})
        return results[0]

    async def batch(self, statements: list[tuple[str, list[object]]]) -> list[dict[str, Any]]:
        if not statements:
            return []
        return await self._request(
            {"batch": [{"sql": sql, "params": params} for sql, params in statements]}
        )


class CloudflareD1MetadataStore:
    def __init__(self, client: CloudflareD1Client):
        self.client = client

    async def close(self) -> None:
        await self.client.close()

    @staticmethod
    def _site_scan_insert_statement(
        job: SitemapJob,
        site_scan: SiteScanSnapshot,
        comparability: ComparabilityResult,
        *,
        committed_at_ms: int,
        job_status: str,
    ) -> tuple[str, list[object]]:
        return (
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
            )
            SELECT
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?
            WHERE EXISTS (
                SELECT 1 FROM sitemap_jobs
                WHERE id = ? AND status = ? AND finished_at = ?
            )
            ON CONFLICT(id) DO NOTHING
            """,
            [
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
                job.job_id,
                job_status,
                committed_at_ms,
            ],
        )

    async def get_site_scan_context(
        self,
        site_id: str,
    ) -> tuple[StoredSiteScan | None, StoredSiteScan | None]:
        baseline_result, previous_result = await self.client.batch(
            [
                (
                    """
                    SELECT scan.*
                    FROM sitemap_sites site
                    JOIN sitemap_site_scans scan
                      ON scan.id = site.semantic_baseline_scan_id
                    WHERE site.id = ? AND scan.is_committed = 1
                    LIMIT 1
                    """,
                    [site_id],
                ),
                (
                    """
                    SELECT *
                    FROM sitemap_site_scans
                    WHERE site_id = ? AND is_committed = 1
                    ORDER BY committed_at DESC, finished_at DESC, id DESC
                    LIMIT 1
                    """,
                    [site_id],
                ),
            ]
        )
        baseline_rows = baseline_result.get("results") or []
        previous_rows = previous_result.get("results") or []
        return (
            stored_site_scan_from_row(baseline_rows[0]) if baseline_rows else None,
            stored_site_scan_from_row(previous_rows[0]) if previous_rows else None,
        )

    async def ensure_site(
        self,
        site_id: str,
        homepage_url: str,
        now_ms: int,
        *,
        check_interval_sec: int | None = None,
    ) -> None:
        from urllib.parse import urlsplit

        domain = (urlsplit(homepage_url).hostname or "").lower()
        interval = check_interval_sec or 3_600
        if interval <= 0:
            raise ValueError("check_interval_sec must be positive.")
        await self.client.query(
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
            [
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
            ],
        )

    @staticmethod
    def _job_from_row(row: dict[str, Any]) -> SitemapJob:
        return SitemapJob(
            attempts=int(row.get("attempts") or 0),
            check_interval_sec=int(row.get("check_interval_sec") or 0),
            homepage_url=str(row.get("homepage_url") or ""),
            idempotency_key=str(row.get("idempotency_key") or ""),
            job_id=str(row.get("id") or ""),
            lease_token=str(row.get("lease_token") or ""),
            max_attempts=int(row.get("max_attempts") or 0),
            schedule_version=int(row.get("schedule_version") or 0),
            scheduled_for=int(row.get("scheduled_for") or 0),
            site_id=str(row.get("site_id") or ""),
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
        result = await self.client.query(
            """
            UPDATE sitemap_sites
            SET dispatch_lease_owner = ?,
                dispatch_lease_token = lower(hex(randomblob(16))),
                dispatch_lease_expires_at = ?, updated_at = ?
            WHERE id IN (
                SELECT id
                FROM sitemap_sites
                WHERE status = 'active' AND next_check_at <= ?
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
            [
                lease_owner,
                now_ms + lease_duration_ms,
                now_ms,
                now_ms,
                now_ms,
                limit,
            ],
        )
        return tuple(
            DueSite(
                check_interval_sec=int(row["check_interval_sec"]),
                dispatch_lease_token=str(row["dispatch_lease_token"]),
                homepage_url=str(row["homepage_url"]),
                schedule_version=int(row["schedule_version"]),
                scheduled_for=int(row["next_check_at"]),
                site_id=str(row["id"]),
            )
            for row in (result.get("results") or [])
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
        results = await self.client.batch(
            [
                (
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
                    [
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
                    ],
                ),
                (
                    """
                    SELECT job.*, site.homepage_url, site.check_interval_sec
                    FROM sitemap_jobs job
                    JOIN sitemap_sites site ON site.id = job.site_id
                    WHERE job.idempotency_key = ?
                    LIMIT 1
                    """,
                    [job_id],
                ),
            ]
        )
        rows = results[1].get("results") or []
        return self._job_from_row(rows[0]) if rows else None

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
        result = await self.client.query(
            """
            UPDATE sitemap_jobs
            SET status = 'running', attempts = attempts + 1,
                lease_owner = ?, lease_token = lower(hex(randomblob(16))),
                lease_expires_at = ?, started_at = ?, finished_at = NULL,
                last_error = NULL, updated_at = ?
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
            RETURNING *, (
                SELECT homepage_url FROM sitemap_sites
                WHERE sitemap_sites.id = sitemap_jobs.site_id
            ) AS homepage_url, (
                SELECT check_interval_sec FROM sitemap_sites
                WHERE sitemap_sites.id = sitemap_jobs.site_id
            ) AS check_interval_sec
            """,
            [
                lease_owner,
                now_ms + lease_duration_ms,
                now_ms,
                now_ms,
                now_ms,
                now_ms,
                limit,
            ],
        )
        return tuple(
            self._job_from_row(row) for row in (result.get("results") or [])
        )

    async def renew_job_lease(
        self,
        job: SitemapJob,
        *,
        lease_duration_ms: int,
        lease_owner: str,
        now_ms: int,
    ) -> bool:
        result = await self.client.query(
            """
            UPDATE sitemap_jobs
            SET lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ?
            RETURNING id
            """,
            [
                now_ms + lease_duration_ms,
                now_ms,
                job.job_id,
                lease_owner,
                job.lease_token,
            ],
        )
        return bool(result.get("results"))

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
        statements: list[tuple[str, list[object]]] = [
            (
                """
                UPDATE sitemap_jobs
                SET status = 'succeeded', finished_at = ?, last_error = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                RETURNING id
                """,
                [finished_at_ms, finished_at_ms, job.job_id, job.lease_token],
            )
        ]
        if site_scan is not None and comparability is not None:
            statements.append(
                self._site_scan_insert_statement(
                    job,
                    site_scan,
                    comparability,
                    committed_at_ms=finished_at_ms,
                    job_status="succeeded",
                )
            )
        statements.append(
            (
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
                  AND EXISTS (
                    SELECT 1 FROM sitemap_jobs
                    WHERE id = ? AND status = 'succeeded' AND finished_at = ?
                  )
                """,
                [
                    next_check_at_ms,
                    finished_at_ms,
                    finished_at_ms,
                    int(bool(comparability and comparability.promote_semantic_baseline)),
                    site_scan.scan_id if site_scan is not None else None,
                    finished_at_ms,
                    job.site_id,
                    job.schedule_version,
                    job.job_id,
                    finished_at_ms,
                ],
            )
        )
        results = await self.client.batch(statements)
        return bool(results[0].get("results"))

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
        if dead and next_check_at_ms is None:
            raise ValueError("A dead job requires a future site check time.")
        status = "dead" if dead else "retry"
        if dead:
            site_sql = """
                UPDATE sitemap_sites
                SET next_check_at = ?, last_attempt_at = ?,
                    error_streak = MAX(
                        error_streak,
                        COALESCE((
                            SELECT base_error_streak + attempts
                            FROM sitemap_jobs WHERE id = ?
                        ), error_streak)
                    ),
                    dispatch_lease_owner = NULL, dispatch_lease_token = NULL,
                    dispatch_lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND schedule_version = ?
                  AND EXISTS (
                    SELECT 1 FROM sitemap_jobs
                    WHERE id = ? AND status = 'dead' AND finished_at = ?
                  )
            """
            site_params: list[object] = [
                next_check_at_ms,
                finished_at_ms,
                job.job_id,
                finished_at_ms,
                job.site_id,
                job.schedule_version,
                job.job_id,
                finished_at_ms,
            ]
        else:
            site_sql = """
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
                  AND EXISTS (
                    SELECT 1 FROM sitemap_jobs
                    WHERE id = ? AND status = 'retry' AND finished_at = ?
                  )
            """
            site_params = [
                finished_at_ms,
                job.job_id,
                available_at_ms,
                finished_at_ms,
                job.site_id,
                job.schedule_version,
                job.job_id,
                finished_at_ms,
            ]
        statements = [
            (
                """
                UPDATE sitemap_jobs
                SET status = ?, available_at = ?, finished_at = ?,
                    last_error = ?, dead_letter_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                RETURNING id
                """,
                [
                    status,
                    available_at_ms,
                    finished_at_ms,
                    error[:2_000],
                    finished_at_ms if dead else None,
                    finished_at_ms,
                    job.job_id,
                    job.lease_token,
                ],
            )
        ]
        if site_scan is not None and comparability is not None:
            statements.append(
                self._site_scan_insert_statement(
                    job,
                    site_scan,
                    comparability,
                    committed_at_ms=finished_at_ms,
                    job_status=status,
                )
            )
        statements.append((site_sql, site_params))
        results = await self.client.batch(statements)
        return bool(results[0].get("results"))

    async def get_resource(self, site_id: str, url: str) -> ResourceState | None:
        result = await self.client.query(
            """
            SELECT id, url, type, etag, http_last_modified, content_hash,
                   urlset_hash, metadata_hash, url_count, current_state_key
            FROM sitemap_resources
            WHERE site_id = ? AND url = ? AND current_state_key IS NOT NULL
            LIMIT 1
            """,
            [site_id, url],
        )
        rows = result.get("results") or []
        if not rows:
            return None
        row = rows[0]
        return ResourceState(
            content_hash=str(row["content_hash"]),
            current_state_key=str(row["current_state_key"]),
            etag=row.get("etag"),
            last_modified=row.get("http_last_modified"),
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
        await self.client.batch(
            [
                (
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
                    [
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
                    ],
                ),
                (
                    """
                    UPDATE sitemap_sites
                    SET last_checked_at = ?,
                        last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    [checked_at_ms, int(changed), checked_at_ms, checked_at_ms, site_id],
                ),
            ]
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
        await self.client.batch(
            [
                (
                    """
                    UPDATE sitemap_resources
                    SET canonical_fetch_url = ?, etag = COALESCE(?, etag),
                        http_last_modified = COALESCE(?, http_last_modified),
                        last_checked_at = ?, missing_streak = 0, error_streak = 0,
                        updated_at = ?
                    WHERE id = ? AND site_id = ?
                    """,
                    [
                        canonical_fetch_url,
                        etag,
                        last_modified,
                        checked_at_ms,
                        checked_at_ms,
                        resource_id,
                        site_id,
                    ],
                ),
                (
                    """
                    UPDATE sitemap_sites
                    SET last_checked_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    [checked_at_ms, checked_at_ms, site_id],
                ),
            ]
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
        await self.client.batch(
            [
                (
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
                    [
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
                    ],
                ),
                (
                    """
                    UPDATE sitemap_sites
                    SET last_checked_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    [checked_at_ms, checked_at_ms, site_id],
                ),
            ]
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
        await self.client.query(
            """
            INSERT INTO sitemap_runs (
                id, run_key, site_id, resource_id, started_at, finished_at,
                http_status, bytes_downloaded, url_count, result, error_code,
                added_count, removed_count, modified_count, diff_key,
                state_key, site_scan_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_key) DO NOTHING
            """,
            [
                stable_id("smrun", run_key),
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
            ],
        )


class CloudflareR2ObjectStore:
    def __init__(
        self,
        *,
        access_key_id: str,
        account_id: str,
        bucket: str,
        secret_access_key: str,
        timeout_seconds: float = 45.0,
    ):
        if not access_key_id or not account_id or not bucket or not secret_access_key:
            raise ValueError("Cloudflare R2 account, bucket, and S3 credentials are required.")
        self.access_key_id = access_key_id
        self.account_id = account_id
        self.bucket = bucket
        self.secret_access_key = secret_access_key
        self.client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    def _signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.secret_access_key).encode("utf-8")
        for value in (date_stamp, "auto", "s3", "aws4_request"):
            key = hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()
        return key

    def _signed_request(
        self,
        method: str,
        key: str,
        body: bytes,
        content_type: str | None,
    ) -> tuple[str, dict[str, str]]:
        safe_key = _safe_object_key(key)
        host = f"{self.account_id}.r2.cloudflarestorage.com"
        canonical_uri = f"/{quote(self.bucket, safe='')}/{quote(safe_key, safe='/-_.~')}"
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            headers["content-type"] = content_type
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
        )
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return f"https://{host}{canonical_uri}", headers

    async def get(self, key: str) -> bytes | None:
        url, headers = self._signed_request("GET", key, b"", None)
        response = await self.client.get(url, headers=headers)
        if response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise CloudflareApiError(f"Cloudflare R2 GET failed with HTTP {response.status_code}.")
        return response.content

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        url, headers = self._signed_request("PUT", key, body, content_type)
        response = await self.client.put(url, headers=headers, content=body)
        if response.status_code < 200 or response.status_code >= 300:
            raise CloudflareApiError(f"Cloudflare R2 PUT failed with HTTP {response.status_code}.")
