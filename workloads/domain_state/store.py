from __future__ import annotations

from typing import Any

from runner import (
    DOMAIN_STATE_SOURCE,
    D1Client,
    iso_delta,
    utc_now_iso,
)
from workloads.domain_state.types import DomainStateResult, DomainStateTask

class D1DomainStateStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def requeue_missing_credential_tasks(self, limit: int) -> int:
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            UPDATE domain_state_tasks
            SET status = 'queued',
                attempts = 0,
                generation = generation + 1,
                last_queued_at = ?,
                next_retry_at = NULL,
                last_error = NULL,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                dead_letter_at = NULL,
                updated_at = ?
            WHERE rowid IN (
              SELECT rowid
              FROM domain_state_tasks
              WHERE source = ?
                AND status = 'failed'
                AND attempts >= max_attempts
                AND dead_letter_at IS NOT NULL
                AND last_error = 'AHREF_API_KEY is required for Ahrefs Domain Rating requests'
              ORDER BY updated_at, normalized_domain
              LIMIT ?
            )
            """,
            [now, now, DOMAIN_STATE_SOURCE, limit],
        )
        return int(meta.get("changes") or 0)

    async def queue_due_tasks(self, limit: int, max_age_days: int) -> int:
        stale_before = iso_delta(days=-max_age_days)
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            INSERT INTO domain_state_tasks (
              normalized_domain, source, status, attempts, last_queued_at,
              next_retry_at, last_error, created_at, updated_at,
              fetch_domain_rating, fetch_rdap
            )
            SELECT due.normalized_domain, ?, 'queued', 0, ?, NULL, NULL, ?, ?,
                   due.fetch_domain_rating, due.fetch_rdap
            FROM (
              SELECT
                t.normalized_domain,
                CASE
                  WHEN ds.last_crawled_at IS NULL OR ds.last_crawled_at < ? THEN 1
                  ELSE 0
                END AS fetch_domain_rating,
                CASE WHEN ds.rdap_checked_at IS NULL THEN 1 ELSE 0 END AS fetch_rdap
              FROM tools t
              LEFT JOIN domain_states ds
                ON ds.normalized_domain = t.normalized_domain
               AND ds.source = ?
              LEFT JOIN domain_state_tasks existing_task
                ON existing_task.normalized_domain = t.normalized_domain
               AND existing_task.source = ?
              WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
                AND t.duplicate_of_tool_id IS NULL
                AND trim(t.normalized_domain) <> ''
                AND (
                  existing_task.normalized_domain IS NULL
                  OR existing_task.status IN ('done', 'no_data')
                )
                AND (
                  ds.last_crawled_at IS NULL
                  OR ds.last_crawled_at < ?
                  OR ds.rdap_checked_at IS NULL
                )
              GROUP BY t.normalized_domain, ds.last_crawled_at, ds.rdap_checked_at
              ORDER BY CASE WHEN min(ds.last_crawled_at) IS NULL THEN 0 ELSE 1 END,
                       min(ds.last_crawled_at) ASC,
                       t.normalized_domain ASC
              LIMIT ?
            ) AS due
            WHERE 1 = 1
            ON CONFLICT(normalized_domain, source) DO UPDATE SET
              status = 'queued',
              attempts = 0,
              generation = domain_state_tasks.generation + 1,
              fetch_domain_rating = excluded.fetch_domain_rating,
              fetch_rdap = excluded.fetch_rdap,
              last_queued_at = excluded.last_queued_at,
              next_retry_at = NULL,
              last_error = NULL,
              lease_owner = NULL,
              lease_token = NULL,
              lease_expires_at = NULL,
              dead_letter_at = NULL,
              updated_at = excluded.updated_at
            WHERE domain_state_tasks.status IN ('done', 'no_data')
            """,
            [
                DOMAIN_STATE_SOURCE,
                now,
                now,
                now,
                stale_before,
                DOMAIN_STATE_SOURCE,
                DOMAIN_STATE_SOURCE,
                stale_before,
                limit,
            ],
        )
        return int(meta.get("changes") or 0)

    async def claim_due_tasks(self, limit: int, lease_owner: str) -> list[DomainStateTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(minutes=15)
        rows = await self.d1.query(
            """
            UPDATE domain_state_tasks SET status = 'processing', attempts = attempts + 1, last_started_at = ?1, next_retry_at = NULL, last_error = NULL, lease_owner = ?2, lease_token = lower(hex(randomblob(16))), lease_expires_at = ?3, updated_at = ?4 WHERE rowid IN ( WITH ready_tasks AS MATERIALIZED (SELECT rowid AS __rid, coalesce(next_retry_at, '') AS __due_at, updated_at AS __updated_at, normalized_domain AS __domain FROM domain_state_tasks WHERE source = ?5 AND dead_letter_at IS NULL AND attempts < max_attempts AND (status IN ('queued', 'failed', 'sync_failed') AND (next_retry_at IS NULL OR next_retry_at <= ?6)) ORDER BY coalesce(next_retry_at, ''), updated_at, normalized_domain LIMIT ?8), expired_tasks AS MATERIALIZED (SELECT rowid AS __rid, coalesce(next_retry_at, '') AS __due_at, updated_at AS __updated_at, normalized_domain AS __domain FROM domain_state_tasks WHERE source = ?5 AND dead_letter_at IS NULL AND attempts < max_attempts AND (status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?7) ORDER BY coalesce(next_retry_at, ''), updated_at, normalized_domain LIMIT ?8)
             SELECT __rid FROM (SELECT * FROM ready_tasks UNION ALL SELECT * FROM expired_tasks)
             ORDER BY __due_at, __updated_at, __domain LIMIT ?8 ) RETURNING normalized_domain, attempts, max_attempts, generation, lease_token, fetch_domain_rating, fetch_rdap
        """,
            [now, lease_owner, lease_expires_at, now, DOMAIN_STATE_SOURCE, now, now, limit],
        )
        return [
            DomainStateTask(
                normalized_domain=str(row.get("normalized_domain") or ""),
                attempts=int(row.get("attempts") or 0),
                max_attempts=int(row.get("max_attempts") or 5),
                generation=int(row.get("generation") or 1),
                lease_token=str(row.get("lease_token") or ""),
                fetch_domain_rating=bool(row.get("fetch_domain_rating")),
                fetch_rdap=bool(row.get("fetch_rdap")),
            )
            for row in rows
            if row.get("normalized_domain") and row.get("lease_token")
        ]

    async def renew_lease(self, task: DomainStateTask) -> bool:
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            UPDATE domain_state_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE normalized_domain = ? AND source = ? AND status = 'processing'
              AND generation = ? AND lease_token = ?
            """,
            [iso_delta(minutes=15), now, task.normalized_domain, DOMAIN_STATE_SOURCE, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def complete_task(self, task: DomainStateTask, result: DomainStateResult) -> bool:
        now = utc_now_iso()
        domain_rating_checked_at = now if task.fetch_domain_rating and result.status in ("done", "no_data") else None
        rdap_checked_at = now if result.rdap_status is not None else None

        state_statement = (
            """
            INSERT INTO domain_states (
              normalized_domain, source, domain_rating, last_crawled_at, domain_created_at,
              rdap_status, rdap_checked_at, rdap_last_error
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?
            WHERE EXISTS (
              SELECT 1 FROM domain_state_tasks
              WHERE normalized_domain = ? AND source = ?
                AND status = 'processing' AND generation = ? AND lease_token = ?
            )
            ON CONFLICT (normalized_domain, source) DO UPDATE SET
              domain_rating = coalesce(excluded.domain_rating, domain_states.domain_rating),
              last_crawled_at = coalesce(excluded.last_crawled_at, domain_states.last_crawled_at),
              domain_created_at = coalesce(excluded.domain_created_at, domain_states.domain_created_at),
              rdap_status = case
                when excluded.rdap_checked_at is not null then excluded.rdap_status
                else domain_states.rdap_status
              end,
              rdap_checked_at = coalesce(excluded.rdap_checked_at, domain_states.rdap_checked_at),
              rdap_last_error = case
                when excluded.rdap_checked_at is not null then excluded.rdap_last_error
                else domain_states.rdap_last_error
              end,
              updated_at = ?
            """,
            [
                task.normalized_domain,
                DOMAIN_STATE_SOURCE,
                result.domain_rating,
                domain_rating_checked_at,
                result.domain_created_at,
                result.rdap_status,
                rdap_checked_at,
                (result.rdap_error or "")[:2000] or None,
                task.normalized_domain,
                DOMAIN_STATE_SOURCE,
                task.generation,
                task.lease_token,
                now,
            ],
        )

        if result.status in ("done", "no_data"):
            statements: list[tuple[str, list[Any]]] = []
            if result.domain_rating is not None:
                statements.append(
                    (
                        """
                        INSERT INTO domain_rating_history (
                          normalized_domain, source, observed_date, domain_rating, observed_at
                        )
                        SELECT ?, ?, substr(?, 1, 10), ?, ?
                        WHERE EXISTS (
                          SELECT 1 FROM domain_state_tasks
                          WHERE normalized_domain = ? AND source = ?
                            AND status = 'processing' AND generation = ? AND lease_token = ?
                        )
                        ON CONFLICT (normalized_domain, source, observed_date) DO UPDATE SET
                          domain_rating = excluded.domain_rating,
                          observed_at = excluded.observed_at,
                          updated_at = excluded.observed_at
                        """,
                        [
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            now,
                            result.domain_rating,
                            now,
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            task.generation,
                            task.lease_token,
                        ],
                    )
                )
            statements.extend(
                [
                    state_statement,
                    (
                        """
                        UPDATE domain_state_tasks
                        SET status = ?, last_fetched_at = ?, last_completed_at = ?,
                            next_retry_at = NULL, last_error = NULL,
                            fetch_rdap = case when ? is not null then 0 else fetch_rdap end,
                            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                            updated_at = ?
                        WHERE normalized_domain = ? AND source = ?
                          AND status = 'processing' AND generation = ? AND lease_token = ?
                        """,
                        [
                            result.status,
                            now,
                            now,
                            rdap_checked_at,
                            now,
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            task.generation,
                            task.lease_token,
                        ],
                    ),
                ]
            )
            batch_results = await self.d1.batch(statements)
            task_meta = batch_results[-1].get("meta") if batch_results else {}
            return int((task_meta or {}).get("changes") or 0) > 0

        exhausted = task.attempts >= task.max_attempts
        retry_minutes = min(60, 2 ** min(task.attempts, 6))
        batch_results = await self.d1.batch(
            [
                state_statement,
                (
                    """
                    UPDATE domain_state_tasks
                    SET status = 'failed',
                        next_retry_at = ?,
                        last_error = ?,
                        dead_letter_at = ?,
                        fetch_rdap = case when ? is not null then 0 else fetch_rdap end,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE normalized_domain = ? AND source = ?
                      AND status = 'processing' AND generation = ? AND lease_token = ?
                    """,
                    [
                        None if exhausted else iso_delta(minutes=retry_minutes),
                        (result.error or "Domain state fetch failed")[:2000],
                        now if exhausted else None,
                        rdap_checked_at,
                        now,
                        task.normalized_domain,
                        DOMAIN_STATE_SOURCE,
                        task.generation,
                        task.lease_token,
                    ],
                ),
            ]
        )
        task_meta = batch_results[-1].get("meta") if batch_results else {}
        return int((task_meta or {}).get("changes") or 0) > 0
