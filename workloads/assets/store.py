from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import ipaddress
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from anti_bot_signatures import detect_anti_bot_page
from shared.readiness import (
    accepted_primary_taxonomy_predicate,
    tool_auto_publish_admission_predicate,
    tool_live_ready_predicate,
)
from workloads.assets.constants import (
    ASSET_DB_STORAGE_BUCKET,
    ASSET_DEAD_LETTER_REVIVE_HOURS,
    ASSET_REQUIREMENT_ORDER,
    ASSET_SOURCE,
    AUTO_APPROVE_TOOL_NAME_CONFIDENCE,
    CATALOG_AUTO_PUBLISH_MAX_LIMIT,
    CATALOG_AUTO_PUBLISH_POLICY_VERSION,
    DEFAULT_R2_BUCKET,
    REVIEW_TOOL_NAME_CONFIDENCE,
)
from workloads.assets.content import (
    clean_asset_text,
    domain_fallback_tool_name,
    invalid_tool_name_reason,
    numbered_public_slug,
    public_tool_slug_base,
)
from workloads.assets.types import (
    AssetFetchResult,
    AssetTask,
    ContentSafetyAssessment,
    PageQualityAssessment,
)
from runner import (
    DOMAIN_STATE_SOURCE,
    TRAFFIC_SOURCE,
    D1Client,
    iso_delta,
    log_debug,
    log_info,
    utc_now_iso,
)

class D1AssetStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def revive_incomplete_dead_letter_tasks(self, limit: int) -> int:
        """Re-open incomplete enrichment after a cooldown so deploy fixes heal the backlog."""
        now = utc_now_iso()
        one_day_ago = iso_delta(hours=-ASSET_DEAD_LETTER_REVIVE_HOURS)
        one_week_ago = iso_delta(days=-7)
        one_month_ago = iso_delta(days=-30)
        rows = await self.d1.query(
            """
            SELECT task.tool_id
            FROM asset_tasks task
            JOIN tools t ON t.id = task.tool_id
            WHERE task.source = ?
              AND task.status = 'failed'
              AND task.dead_letter_at IS NOT NULL
              AND t.status IN ('pending_enrich', 'pending_review')
              AND coalesce(task.last_error, '') NOT LIKE 'content_safety_blocked:%'
              AND (
                (task.generation <= 1 AND task.dead_letter_at <= ?)
                OR (task.generation BETWEEN 2 AND 3 AND task.dead_letter_at <= ?)
                OR (task.generation >= 4 AND task.dead_letter_at <= ?)
              )
            ORDER BY task.dead_letter_at, task.tool_id
            LIMIT ?
            """,
            [ASSET_SOURCE, one_day_ago, one_week_ago, one_month_ago, max(1, limit)],
        )
        revived = 0
        for row in rows:
            tool_id = int(row.get("tool_id") or 0)
            if tool_id <= 0:
                continue
            meta = await self.d1.run(
                """
                UPDATE asset_tasks
                SET status = 'queued',
                    attempts = 0,
                    generation = generation + 1,
                    last_queued_at = ?,
                    next_retry_at = NULL,
                    last_error = 'Automatically revived after enrichment cooldown',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    dead_letter_at = NULL,
                    updated_at = ?
                WHERE tool_id = ?
                  AND source = ?
                  AND status = 'failed'
                  AND dead_letter_at IS NOT NULL
                """,
                [now, now, tool_id, ASSET_SOURCE],
            )
            changed = int(meta.get("changes") or 0)
            revived += changed
        return revived

    async def queue_missing_asset_tasks(self, limit: int) -> int:
        now = utc_now_iso()
        stale_done_before = iso_delta(hours=-24)
        rows = await self.d1.query(
            """
            SELECT
              t.id AS tool_id,
              t.canonical_slug,
              t.normalized_domain,
              t.official_url
            FROM tools t
            LEFT JOIN asset_tasks task
              ON task.tool_id = t.id
             AND task.source = ?
            WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
              AND t.duplicate_of_tool_id IS NULL
              AND trim(t.normalized_domain) <> ''
              AND (
                (t.status IN ('pending_enrich', 'pending_review') AND t.content_safety_status <> 'safe')
                OR NOT EXISTS (
                  SELECT 1
                  FROM tool_assets ta
                  WHERE ta.tool_id = t.id
                    AND ta.asset_kind = 'favicon'
                    AND ta.is_current = 1
                )
                OR NOT EXISTS (
                  SELECT 1
                  FROM tool_localizations tl
                  WHERE tl.tool_id = t.id
                    AND trim(tl.name) <> ''
                )
              )
              AND (
                task.tool_id IS NULL
                OR (
                  task.dead_letter_at IS NULL
                  AND
                  task.status IN ('failed', 'sync_failed')
                  AND task.attempts < task.max_attempts
                  AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?)
                )
                OR (task.status = 'queued' AND task.dead_letter_at IS NULL)
                OR (
                  task.status = 'processing'
                  AND task.dead_letter_at IS NULL
                  AND task.lease_expires_at IS NOT NULL
                  AND task.lease_expires_at <= ?
                )
                OR (
                  task.status = 'done'
                  AND task.updated_at < ?
                )
              )
            ORDER BY t.id ASC
            LIMIT ?
            """,
            [ASSET_SOURCE, now, now, stale_done_before, limit],
        )
        queued = 0
        for row in rows:
            tool_id = int(row.get("tool_id") or 0)
            domain = str(row.get("normalized_domain") or "")
            if tool_id <= 0 or not domain:
                continue
            meta = await self.d1.run(
                """
                INSERT INTO asset_tasks (
                  tool_id, normalized_domain, source, status, last_queued_at, next_retry_at, last_error
                )
                VALUES (?, ?, ?, 'queued', ?, NULL, NULL)
                ON CONFLICT (tool_id, source) DO UPDATE
                SET normalized_domain = excluded.normalized_domain,
                    status = excluded.status,
                    attempts = 0,
                    generation = asset_tasks.generation + 1,
                    last_queued_at = excluded.last_queued_at,
                    next_retry_at = NULL,
                    last_error = NULL,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    dead_letter_at = NULL,
                    updated_at = excluded.last_queued_at
                WHERE asset_tasks.status = 'done'
                """,
                [tool_id, domain, ASSET_SOURCE, now],
            )
            queued += int(meta.get("changes") or 0)
        return queued

    async def claim_due_tasks(self, limit: int, lease_owner: str) -> list[AssetTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(hours=1)
        rows = await self.d1.query(
            """
            WITH ready_tasks AS MATERIALIZED (SELECT task.tool_id, task.normalized_domain, task.attempts, task.max_attempts, task.generation, t.canonical_slug, t.official_url, coalesce(task.next_retry_at, '') AS __due_at, task.updated_at AS __updated_at FROM asset_tasks task JOIN tools t ON t.id = task.tool_id WHERE task.source = ?1 AND task.dead_letter_at IS NULL AND task.attempts < task.max_attempts AND (task.status IN ('queued', 'failed', 'sync_failed') AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?2)) ORDER BY coalesce(task.next_retry_at, ''), task.updated_at LIMIT ?4), expired_tasks AS MATERIALIZED (SELECT task.tool_id, task.normalized_domain, task.attempts, task.max_attempts, task.generation, t.canonical_slug, t.official_url, coalesce(task.next_retry_at, '') AS __due_at, task.updated_at AS __updated_at FROM asset_tasks task JOIN tools t ON t.id = task.tool_id WHERE task.source = ?1 AND task.dead_letter_at IS NULL AND task.attempts < task.max_attempts AND (task.status = 'processing' AND task.lease_expires_at IS NOT NULL AND task.lease_expires_at <= ?3) ORDER BY coalesce(task.next_retry_at, ''), task.updated_at LIMIT ?4)
             SELECT tool_id, normalized_domain, attempts, max_attempts, generation, canonical_slug, official_url FROM (SELECT * FROM ready_tasks UNION ALL SELECT * FROM expired_tasks)
             ORDER BY __due_at, __updated_at LIMIT ?4
        """,
            [ASSET_SOURCE, now, now, limit],
        )

        claimed: list[AssetTask] = []
        for row in rows:
            tool_id = int(row.get("tool_id") or 0)
            domain = str(row.get("normalized_domain") or "")
            if tool_id <= 0 or not domain:
                continue
            claimed_rows = await self.d1.query(
                """
                UPDATE asset_tasks
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_started_at = ?,
                    next_retry_at = NULL,
                    last_error = NULL,
                    lease_owner = ?,
                    lease_token = lower(hex(randomblob(16))),
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE tool_id = ?
                  AND source = ?
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
                [now, lease_owner, lease_expires_at, now, tool_id, ASSET_SOURCE, now, now],
            )
            if claimed_rows:
                claimed_row = claimed_rows[0]
                claimed.append(
                    AssetTask(
                        tool_id=tool_id,
                        canonical_slug=str(row.get("canonical_slug") or ""),
                        normalized_domain=domain,
                        official_url=str(row.get("official_url") or ""),
                        attempts=int(claimed_row.get("attempts") or 0),
                        max_attempts=int(claimed_row.get("max_attempts") or 5),
                        generation=int(claimed_row.get("generation") or 1),
                        lease_token=str(claimed_row.get("lease_token") or ""),
                    )
                )
        return claimed

    async def upsert_tool_asset(
        self,
        task: AssetTask,
        asset_kind: str,
        storage_object_path: str,
        public_url: str | None,
        mime_type: str,
        width: int | None,
        height: int | None,
    ) -> None:
        rows = await self.d1.query(
            """
            SELECT id
            FROM tool_assets
            WHERE tool_id = ?
              AND asset_kind = ?
              AND coalesce(locale_code, '') = ''
              AND is_current = 1
            LIMIT 1
            """,
            [task.tool_id, asset_kind],
        )
        if rows:
            await self.d1.run(
                """
                UPDATE tool_assets
                SET storage_bucket = ?,
                    storage_object_path = ?,
                    public_url = ?,
                    mime_type = ?,
                    width = ?,
                    height = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                [ASSET_DB_STORAGE_BUCKET, storage_object_path, public_url, mime_type, width, height, rows[0]["id"]],
            )
            return

        await self.d1.run(
            """
            INSERT INTO tool_assets (
              tool_id,
              locale_code,
              asset_kind,
              storage_bucket,
              storage_object_path,
              public_url,
              mime_type,
              width,
              height,
              is_current
            )
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [task.tool_id, asset_kind, ASSET_DB_STORAGE_BUCKET, storage_object_path, public_url, mime_type, width, height],
        )

    async def save_tool_localization(self, task: AssetTask, result: AssetFetchResult) -> None:
        title = clean_asset_text(result.title, 120)
        description = clean_asset_text(result.description, 500)
        name_source = clean_asset_text(result.name_source, 80) or "unknown"
        name_confidence = max(0, min(100, int(result.name_confidence or 0)))
        name_evidence = clean_asset_text(result.name_evidence, 500)
        name_review_status = str(getattr(result, "name_review_status", None) or "auto_approved")
        invalid_reason = invalid_tool_name_reason(title)
        if invalid_reason:
            title = domain_fallback_tool_name(task.normalized_domain)
            name_source = "domain_fallback"
            name_confidence = 80
            name_evidence = f"Domain candidate after {invalid_reason}"
            # Domain fallback after an invalid title is still auto-approved at confidence 80.
            name_review_status = "auto_approved"
        elif name_confidence < AUTO_APPROVE_TOOL_NAME_CONFIDENCE:
            name_review_status = "needs_review"
        name_evidence = json.dumps({
            "ruleset": "tool-name-quality-2026-09-16.1",
            "page_title": clean_asset_text(result.page_title, 200),
            "source_url": result.final_url,
            "evidence": name_evidence,
        }, ensure_ascii=False, separators=(",", ":"))
        if not title and not description:
            return
        rows = await self.d1.query(
            """
            SELECT locale_code, name, name_review_status
            FROM tool_localizations
            WHERE tool_id = ?
            ORDER BY CASE WHEN locale_code = 'en' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            [task.tool_id],
        )
        locale = str(rows[0].get("locale_code") or "") if rows else ""
        current = rows[0] if rows else {}
        current_name = str(current.get("name") or "")
        legacy_name_to_repair = (
            current_name if current.get("name_review_status") == "legacy_unreviewed"
            and invalid_tool_name_reason(current_name) else None
        )
        if not locale:
            locale_rows = await self.d1.query(
                """
                SELECT code
                FROM app_locales
                WHERE code = 'en' OR is_primary = 1
                ORDER BY CASE WHEN code = 'en' THEN 0 ELSE 1 END
                LIMIT 1
                """,
            )
            locale = str(locale_rows[0].get("code") or "") if locale_rows else ""
            if not locale:
                return
            slug_base = public_tool_slug_base(task, title)
            for number in range(1, 1001):
                localized_slug = numbered_public_slug(slug_base, number)
                meta = await self.d1.run(
                    """
                    INSERT OR IGNORE INTO tool_localizations (
                      tool_id, locale_code, localized_slug, name, tagline, short_description, feature_highlights,
                      translation_status, published_at, name_source, name_confidence, name_evidence,
                      name_review_status, last_name_checked_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, '[]', 'published', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                            ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    [
                        task.tool_id,
                        locale,
                        localized_slug,
                        title or localized_slug,
                        description or None,
                        description or None,
                        name_source,
                        name_confidence,
                        name_evidence or None,
                        name_review_status,
                    ],
                )
                if int(meta.get("changes") or 0) > 0:
                    return
                existing_rows = await self.d1.query(
                    "SELECT localized_slug FROM tool_localizations WHERE tool_id = ? AND locale_code = ? LIMIT 1",
                    [task.tool_id, locale],
                )
                if existing_rows:
                    return
            raise RuntimeError(f"Unable to allocate public slug for tool {task.tool_id}")

        await self.d1.run(
            """
            UPDATE tool_localizations
            SET name = CASE
                  WHEN ? <> '' AND name_review_status <> 'human_approved' AND (
                    name IS NULL OR trim(name) = '' OR name = ?
                    OR name_review_status IN ('needs_review', 'rejected') OR (name_review_status = 'legacy_unreviewed' AND name = ?)
                  )
                  THEN ?
                  ELSE name
                END,
                name_source = CASE
                  WHEN ? <> '' AND name_review_status <> 'human_approved' AND (name IS NULL OR trim(name) = '' OR name = ? OR name_review_status IN ('needs_review', 'rejected') OR (name_review_status = 'legacy_unreviewed' AND name = ?))
                  THEN ? ELSE name_source END,
                name_confidence = CASE
                  WHEN ? <> '' AND name_review_status <> 'human_approved' AND (name IS NULL OR trim(name) = '' OR name = ? OR name_review_status IN ('needs_review', 'rejected') OR (name_review_status = 'legacy_unreviewed' AND name = ?))
                  THEN ? ELSE name_confidence END,
                name_evidence = CASE
                  WHEN ? <> '' AND name_review_status <> 'human_approved' AND (name IS NULL OR trim(name) = '' OR name = ? OR name_review_status IN ('needs_review', 'rejected') OR (name_review_status = 'legacy_unreviewed' AND name = ?))
                  THEN ? ELSE name_evidence END,
                name_review_status = CASE
                  WHEN ? <> '' AND name_review_status <> 'human_approved' AND (name IS NULL OR trim(name) = '' OR name = ? OR name_review_status IN ('needs_review', 'rejected') OR (name_review_status = 'legacy_unreviewed' AND name = ?))
                  THEN ? ELSE name_review_status END,
                last_name_checked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                tagline = CASE
                  WHEN ? <> '' AND (tagline IS NULL OR trim(tagline) = '')
                  THEN ?
                  ELSE tagline
                END,
                short_description = CASE
                  WHEN ? <> '' AND (short_description IS NULL OR trim(short_description) = '')
                  THEN ?
                  ELSE short_description
                END,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE tool_id = ?
              AND locale_code = ?
            """,
            [
                title,
                task.canonical_slug,
                legacy_name_to_repair,
                title,
                title,
                task.canonical_slug,
                legacy_name_to_repair,
                name_source,
                title,
                task.canonical_slug,
                legacy_name_to_repair,
                name_confidence,
                title,
                task.canonical_slug,
                legacy_name_to_repair,
                name_evidence or None,
                title,
                task.canonical_slug,
                legacy_name_to_repair,
                name_review_status,
                description,
                description,
                description,
                description,
                task.tool_id,
                locale,
            ],
        )

    async def missing_tool_enrichment_requirements(self, tool_id: int) -> list[str]:
        rows = await self.d1.query(
            f"""
            SELECT
              CASE WHEN EXISTS (
                SELECT 1 FROM tools t
                WHERE t.id = ? AND t.content_safety_status = 'safe'
              ) THEN 1 ELSE 0 END AS has_content_safety,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_localizations tl
                WHERE tl.tool_id = ?
                  AND trim(tl.name) <> ''
              ) THEN 1 ELSE 0 END AS has_description,
              CASE WHEN EXISTS (
                SELECT 1 FROM tools t
                WHERE t.id = ? AND {accepted_primary_taxonomy_predicate('t')}
              ) THEN 1 ELSE 0 END AS has_category
            """,
            [tool_id, tool_id, tool_id],
        )
        row = rows[0] if rows else {}
        return [
            name
            for name, column in (
                ("content_safety", "has_content_safety"),
                ("description", "has_description"),
                ("category", "has_category"),
            )
            if not int(row.get(column) or 0)
        ]

    async def missing_asset_requirements(self, tool_id: int) -> list[str]:
        rows = await self.d1.query(
            """
            SELECT
              CASE WHEN EXISTS (
                SELECT 1 FROM tools t
                WHERE t.id = ? AND t.content_safety_status = 'safe'
              ) THEN 1 ELSE 0 END AS has_content_safety,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_assets ta
                WHERE ta.tool_id = ?
                  AND ta.asset_kind = 'screenshot'
                  AND ta.storage_bucket = ?
                  AND ta.is_current = 1
              ) THEN 1 ELSE 0 END AS has_screenshot,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_assets ta
                WHERE ta.tool_id = ?
                  AND ta.asset_kind = 'favicon'
                  AND ta.is_current = 1
              ) THEN 1 ELSE 0 END AS has_favicon,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_localizations tl
                WHERE tl.tool_id = ?
                  AND trim(tl.name) <> ''
              ) THEN 1 ELSE 0 END AS has_description
            """,
            [
                tool_id,
                tool_id,
                ASSET_DB_STORAGE_BUCKET,
                tool_id,
                tool_id,
            ],
        )
        row = rows[0] if rows else {}
        columns = {
            "content_safety": "has_content_safety",
            "screenshot": "has_screenshot",
            "favicon": "has_favicon",
            "description": "has_description",
        }
        missing = [
            requirement
            for requirement in ASSET_REQUIREMENT_ORDER
            if not int(row.get(columns[requirement]) or 0)
        ]
        return missing

    async def save_content_safety(
        self,
        task: AssetTask,
        assessment: ContentSafetyAssessment,
    ) -> ContentSafetyAssessment:
        existing_rows = await self.d1.query(
            "SELECT status, content_safety_status FROM tools WHERE id = ? LIMIT 1",
            [task.tool_id],
        )
        existing = existing_rows[0] if existing_rows else {}
        previous_status = str(existing.get("content_safety_status") or "unknown")
        tool_status = str(existing.get("status") or "")
        stored = assessment
        if tool_status == "published" and assessment.status == "nsfw":
            stored = ContentSafetyAssessment(
                "needs_review",
                assessment.risk_score,
                assessment.confidence,
                "published_tool_requires_human_confirmation",
                (assessment.evidence + ["proposed:nsfw"])[:12],
                assessment.source,
            )

        now = utc_now_iso()
        evidence_json = json.dumps(stored.evidence, ensure_ascii=False, separators=(",", ":"))
        await self.d1.batch([
            (
                """
                UPDATE tools
                SET content_safety_status = ?,
                    content_safety_score = ?,
                    content_safety_confidence = ?,
                    content_safety_source = ?,
                    content_safety_reason = ?,
                    content_safety_evidence = ?,
                    content_safety_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                [
                    stored.status,
                    stored.risk_score,
                    stored.confidence,
                    stored.source,
                    stored.reason,
                    evidence_json,
                    now,
                    now,
                    task.tool_id,
                ],
            ),
            (
                """
                INSERT INTO tool_content_safety_events (
                  tool_id, previous_status, decision, risk_score, confidence,
                  source, reason, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    task.tool_id,
                    previous_status,
                    stored.status,
                    stored.risk_score,
                    stored.confidence,
                    stored.source,
                    stored.reason,
                    evidence_json,
                ],
            ),
        ])
        return stored

    async def has_tool_enrichment(self, tool_id: int) -> bool:
        return not await self.missing_tool_enrichment_requirements(tool_id)

    async def renew_lease(self, task: AssetTask) -> bool:
        meta = await self.d1.run(
            """
            UPDATE asset_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE tool_id = ? AND source = ? AND status = 'processing'
              AND generation = ? AND lease_token = ?
            """,
            [iso_delta(hours=1), utc_now_iso(), task.tool_id, ASSET_SOURCE, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def record_page_quality(
        self,
        task: AssetTask,
        assessment: PageQualityAssessment,
    ) -> bool:
        meta = await self.d1.run(
            """
            UPDATE asset_tasks
            SET page_state = ?,
                page_state_reason = ?,
                last_page_checked_at = ?,
                updated_at = ?
            WHERE tool_id = ?
              AND source = ?
              AND status = 'processing'
              AND generation = ?
              AND lease_token = ?
            """,
            [
                assessment.state,
                f"{assessment.reason}:{assessment.evidence}"[:500],
                utc_now_iso(),
                utc_now_iso(),
                task.tool_id,
                ASSET_SOURCE,
                task.generation,
                task.lease_token,
            ],
        )
        return int(meta.get("changes") or 0) > 0

    async def complete_task(
        self,
        task: AssetTask,
        status: str,
        error: str | None = None,
        *,
        retryable: bool = True,
        retry_after_hours: int | None = None,
    ) -> bool:
        now = utc_now_iso()
        exhausted = task.attempts >= task.max_attempts or not retryable
        meta = await self.d1.run(
            """
            UPDATE asset_tasks
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
            WHERE tool_id = ?
              AND source = ?
              AND status = 'processing'
              AND generation = ?
              AND lease_token = ?
            """,
            [
                status,
                now,
                None
                if status == "done" or exhausted
                else iso_delta(hours=retry_after_hours) if retry_after_hours is not None else iso_delta(days=1),
                (error or "")[:2000] or None,
                now if status != "done" and exhausted else None,
                now,
                now,
                task.tool_id,
                ASSET_SOURCE,
                task.generation,
                task.lease_token,
            ],
        )
        return int(meta.get("changes") or 0) > 0


