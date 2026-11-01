from __future__ import annotations

import json
from typing import Any

from runner import (
    TRAFFIC_SOURCE,
    D1Client,
    iso_delta,
    log_info,
    utc_now_iso,
)
from workloads.traffic.types import FetchResult, TrafficTask
from workloads.traffic.country import (
    build_domain_traffic_country_upsert_statements,
    is_missing_market_country_schema_error,
)
from workloads.traffic.parse import build_domain_traffic_monthly_upsert

class D1TaskStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def queue_missing_traffic_tasks(self, limit: int, traffic_month: str) -> int:
        now = utc_now_iso()
        candidates = await self.d1.query(
            """
            WITH monitored_domains AS (
              SELECT normalized_domain
              FROM tools
              WHERE status IN ('published', 'pending_enrich', 'pending_review')
                AND duplicate_of_tool_id IS NULL
                AND trim(normalized_domain) <> ''
              UNION
              SELECT normalized_domain
              FROM ai_directory_sites
              WHERE status = 'active'
                AND trim(normalized_domain) <> ''
            )
            SELECT monitored.normalized_domain
            FROM monitored_domains monitored
            LEFT JOIN domain_traffic_monthly tm
              ON tm.normalized_domain = monitored.normalized_domain
             AND tm.source = ?
             AND tm.traffic_month = ?
            LEFT JOIN traffic_tasks task
              ON task.normalized_domain = monitored.normalized_domain
             AND task.source = ?
             AND task.traffic_month = ?
            WHERE tm.traffic_month IS NULL
              AND (
                task.normalized_domain IS NULL
                OR task.status = 'done'
              )
            GROUP BY monitored.normalized_domain
            ORDER BY min(coalesce(task.updated_at, '')) ASC, monitored.normalized_domain ASC
            LIMIT ?
            """,
            [TRAFFIC_SOURCE, traffic_month, TRAFFIC_SOURCE, traffic_month, limit],
        )

        queued = 0
        for candidate in candidates:
            domain = str(candidate.get("normalized_domain") or "")
            if not domain:
                continue
            meta = await self.d1.run(
                """
                INSERT INTO traffic_tasks (
                  normalized_domain, source, traffic_month, status, last_queued_at, next_retry_at, last_error
                )
                VALUES (?, ?, ?, 'queued', ?, NULL, NULL)
                ON CONFLICT (normalized_domain, source, traffic_month) DO UPDATE SET
                  status = 'queued',
                  attempts = 0,
                  generation = traffic_tasks.generation + 1,
                  last_queued_at = excluded.last_queued_at,
                  last_started_at = NULL,
                  last_fetched_at = NULL,
                  next_retry_at = NULL,
                  last_error = 'Requeued because the monthly materialization is missing',
                  lease_owner = NULL,
                  lease_token = NULL,
                  lease_expires_at = NULL,
                  dead_letter_at = NULL,
                  updated_at = excluded.last_queued_at
                WHERE traffic_tasks.status = 'done'
                """,
                [domain, TRAFFIC_SOURCE, traffic_month, now],
            )
            queued += int(meta.get("changes") or 0)

        return queued

    async def claim_due_tasks(self, limit: int, lease_owner: str) -> list[TrafficTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(hours=1)
        rows = await self.d1.query(
            """
            WITH ready_tasks AS MATERIALIZED (SELECT normalized_domain, source, traffic_month, attempts, max_attempts, generation, coalesce(next_retry_at, '') AS __due_at, updated_at AS __updated_at FROM traffic_tasks WHERE source = ?1 AND dead_letter_at IS NULL AND attempts < max_attempts AND (status IN ('queued', 'failed', 'sync_failed') AND (next_retry_at IS NULL OR next_retry_at <= ?2)) ORDER BY coalesce(next_retry_at, ''), updated_at LIMIT ?4), expired_tasks AS MATERIALIZED (SELECT normalized_domain, source, traffic_month, attempts, max_attempts, generation, coalesce(next_retry_at, '') AS __due_at, updated_at AS __updated_at FROM traffic_tasks WHERE source = ?1 AND dead_letter_at IS NULL AND attempts < max_attempts AND (status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?3) ORDER BY coalesce(next_retry_at, ''), updated_at LIMIT ?4)
             SELECT normalized_domain, source, traffic_month, attempts, max_attempts, generation FROM (SELECT * FROM ready_tasks UNION ALL SELECT * FROM expired_tasks)
             ORDER BY __due_at, __updated_at LIMIT ?4
        """,
            [TRAFFIC_SOURCE, now, now, limit],
        )

        claimed: list[TrafficTask] = []
        for row in rows:
            domain = str(row.get("normalized_domain") or "")
            traffic_month = str(row.get("traffic_month") or "")
            if not domain or not traffic_month:
                continue

            claimed_rows = await self.d1.query(
                """
                UPDATE traffic_tasks
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_started_at = ?,
                    next_retry_at = NULL,
                    last_error = NULL,
                    lease_owner = ?,
                    lease_token = lower(hex(randomblob(16))),
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE normalized_domain = ?
                  AND source = ?
                  AND traffic_month = ?
                  AND dead_letter_at IS NULL
                  AND attempts < max_attempts
                  AND (
                    (
                      status IN ('queued', 'failed', 'sync_failed')
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    )
                    OR (
                      status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    )
                  )
                RETURNING attempts, max_attempts, generation, lease_token
                """,
                [
                    now,
                    lease_owner,
                    lease_expires_at,
                    now,
                    domain,
                    TRAFFIC_SOURCE,
                    traffic_month,
                    now,
                    now,
                ],
            )
            if claimed_rows:
                claimed_row = claimed_rows[0]
                claimed.append(
                    TrafficTask(
                        normalized_domain=domain,
                        traffic_month=traffic_month,
                        attempts=int(claimed_row.get("attempts") or 0),
                        max_attempts=int(claimed_row.get("max_attempts") or 5),
                        generation=int(claimed_row.get("generation") or 1),
                        lease_token=str(claimed_row.get("lease_token") or ""),
                    )
                )

        return claimed

    async def renew_lease(self, task: TrafficTask) -> bool:
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            UPDATE traffic_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE normalized_domain = ? AND source = ? AND traffic_month = ?
              AND status = 'processing' AND generation = ? AND lease_token = ?
            """,
            [iso_delta(hours=1), now, task.normalized_domain, TRAFFIC_SOURCE, task.traffic_month, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def complete_task(self, task: TrafficTask, result: FetchResult) -> bool:
        retry_days = 1 if result.status == "failed" else None
        now = utc_now_iso()
        exhausted = task.attempts >= task.max_attempts
        log_info(
            "d1.complete_task.start",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            status=result.status,
        )
        meta = await self.d1.run(
            """
            UPDATE traffic_tasks
            SET status = ?,
                last_fetched_at = ?,
                next_retry_at = ?,
                last_error = ?,
                dead_letter_at = ?,
                last_completed_at = ?,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE normalized_domain = ?
              AND source = ?
              AND traffic_month = ?
              AND status = 'processing'
              AND generation = ?
              AND lease_token = ?
            """,
            [
                result.status,
                now,
                iso_delta(days=retry_days) if retry_days is not None and not exhausted else None,
                (result.error or "")[:2000] or None,
                now if result.status == "failed" and exhausted else None,
                now,
                now,
                task.normalized_domain,
                TRAFFIC_SOURCE,
                task.traffic_month,
                task.generation,
                task.lease_token,
            ],
        )
        if int(meta.get("changes") or 0) == 0:
            log_info("d1.complete_task.stale", domain=task.normalized_domain, traffic_month=task.traffic_month)
            return False
        await self.update_tool_status(task.normalized_domain, result)
        log_info(
            "d1.complete_task.done",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            status=result.status,
        )
        return True

    async def update_tool_status(self, domain: str, result: FetchResult) -> None:
        retry_days = 30
        if result.status in ("no_data", "forbidden"):
            retry_days = 7
        if result.status == "failed":
            retry_days = 1

        now = utc_now_iso()
        await self.d1.run(
            """
            INSERT INTO tool_traffic_fetch_status (
              tool_id, normalized_domain, source, last_checked_at, last_status, last_error, next_retry_at
            )
            SELECT
              id,
              normalized_domain,
              ?,
              ?,
              ?,
              ?,
              ?
            FROM tools
            WHERE normalized_domain = ?
              AND status IN ('published', 'pending_enrich', 'pending_review')
              AND duplicate_of_tool_id IS NULL
            ON CONFLICT (tool_id, source) DO UPDATE
            SET normalized_domain = excluded.normalized_domain,
                last_checked_at = excluded.last_checked_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                next_retry_at = excluded.next_retry_at,
                updated_at = ?
            """,
            [
                TRAFFIC_SOURCE,
                now,
                result.status,
                (result.error or "")[:2000] or None,
                iso_delta(days=retry_days),
                domain,
                now,
            ],
        )
