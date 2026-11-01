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
from runner import DOMAIN_STATE_SOURCE, TRAFFIC_SOURCE, D1Client, iso_delta, log_debug, utc_now_iso

class D1EnrichmentStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def evaluate_tool(self, tool_id: int) -> str:
        query_with_category_status = f"""
            SELECT
              t.id,
              state.readiness AS previous_readiness,
              CASE WHEN t.content_safety_status = 'safe' THEN 1 ELSE 0 END AS has_content_safety,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_assets a
                WHERE a.tool_id = t.id AND a.asset_kind = 'screenshot' AND a.is_current = 1
              ) THEN 1 ELSE 0 END AS has_screenshot,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_assets a
                WHERE a.tool_id = t.id AND a.asset_kind = 'favicon' AND a.is_current = 1
              ) THEN 1 ELSE 0 END AS has_favicon,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_localizations l
                WHERE l.tool_id = t.id AND trim(l.name) <> ''
              ) THEN 1 ELSE 0 END AS has_localization,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_localizations l
                WHERE l.tool_id = t.id AND trim(l.name) <> ''
                  AND coalesce(l.name_review_status, 'legacy_unreviewed') NOT IN ('needs_review', 'rejected')
                  AND (length(trim(l.name)) <= 80 OR (l.name_review_status = 'human_approved' AND length(trim(l.name)) <= 120))
                  AND instr(l.name, '<') = 0
                  AND instr(l.name, '>') = 0
                  AND instr(l.name, '`') = 0
                  AND instr(lower(l.name), 'json-ld') = 0
              ) THEN 1 ELSE 0 END AS has_name_quality,
              CASE WHEN {accepted_primary_taxonomy_predicate('t')}
                   THEN 1 ELSE 0 END AS has_category,
              CASE WHEN NOT {accepted_primary_taxonomy_predicate('t')} AND EXISTS (
                SELECT 1
                FROM product_taxonomy_assignments pending_assignment
                JOIN taxonomy_terms pending_term ON pending_term.id = pending_assignment.term_id
                WHERE pending_assignment.tool_id = t.id
                  AND pending_assignment.is_primary = 1
                  AND pending_assignment.decision_status IN ('provisional', 'unresolved')
                  AND pending_term.dimension = 'primary_category'
                  AND pending_term.status = 'active'
              ) THEN 1 ELSE 0 END AS category_needs_manual,
              CASE WHEN EXISTS (SELECT 1 FROM tool_sources s WHERE s.tool_id = t.id)
                   THEN 1 ELSE 0 END AS has_source,
              CASE WHEN EXISTS (
                SELECT 1
                FROM domain_traffic_monthly tm
                WHERE tm.normalized_domain = t.normalized_domain
                  AND tm.source = ?
              ) THEN 1 ELSE 0 END AS has_traffic,
              CASE WHEN EXISTS (
                SELECT 1 FROM domain_states ds
                WHERE ds.normalized_domain = t.normalized_domain AND ds.source = ?
                  AND ds.last_crawled_at IS NOT NULL
              ) THEN 1 ELSE 0 END AS has_domain_state
            FROM tools t
            LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
            WHERE t.id = ?
            LIMIT 1
            """
        query_legacy = f"""
            SELECT
              t.id,
              state.readiness AS previous_readiness,
              CASE WHEN t.content_safety_status = 'safe' THEN 1 ELSE 0 END AS has_content_safety,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_assets a
                WHERE a.tool_id = t.id AND a.asset_kind = 'screenshot' AND a.is_current = 1
              ) THEN 1 ELSE 0 END AS has_screenshot,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_assets a
                WHERE a.tool_id = t.id AND a.asset_kind = 'favicon' AND a.is_current = 1
              ) THEN 1 ELSE 0 END AS has_favicon,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_localizations l
                WHERE l.tool_id = t.id AND trim(l.name) <> ''
              ) THEN 1 ELSE 0 END AS has_localization,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_localizations l
                WHERE l.tool_id = t.id AND trim(l.name) <> ''
                  AND coalesce(l.name_review_status, 'legacy_unreviewed') NOT IN ('needs_review', 'rejected')
                  AND (length(trim(l.name)) <= 80 OR (l.name_review_status = 'human_approved' AND length(trim(l.name)) <= 120))
                  AND instr(l.name, '<') = 0
                  AND instr(l.name, '>') = 0
                  AND instr(l.name, '`') = 0
                  AND instr(lower(l.name), 'json-ld') = 0
              ) THEN 1 ELSE 0 END AS has_name_quality,
              CASE WHEN {accepted_primary_taxonomy_predicate('t')}
                   THEN 1 ELSE 0 END AS has_category,
              0 AS category_needs_manual,
              CASE WHEN EXISTS (SELECT 1 FROM tool_sources s WHERE s.tool_id = t.id)
                   THEN 1 ELSE 0 END AS has_source,
              CASE WHEN EXISTS (
                SELECT 1
                FROM domain_traffic_monthly tm
                WHERE tm.normalized_domain = t.normalized_domain
                  AND tm.source = ?
              ) THEN 1 ELSE 0 END AS has_traffic,
              CASE WHEN EXISTS (
                SELECT 1 FROM domain_states ds
                WHERE ds.normalized_domain = t.normalized_domain AND ds.source = ?
                  AND ds.last_crawled_at IS NOT NULL
              ) THEN 1 ELSE 0 END AS has_domain_state
            FROM tools t
            LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
            WHERE t.id = ?
            LIMIT 1
            """
        try:
            rows = await self.d1.query(
                query_with_category_status,
                [TRAFFIC_SOURCE, DOMAIN_STATE_SOURCE, tool_id],
            )
        except Exception:
            rows = await self.d1.query(
                query_legacy,
                [TRAFFIC_SOURCE, DOMAIN_STATE_SOURCE, tool_id],
            )
        if not rows:
            return "missing"
        row = rows[0]
        blocking = [
            name
            for name, column in (
                ("published_localization", "has_localization"),
                ("category", "has_category"),
                ("source_evidence", "has_source"),
            )
            if not int(row.get(column) or 0)
        ]
        warnings = [
            name
            for name, column in (
                ("content_safety", "has_content_safety"),
                ("name_quality", "has_name_quality"),
                ("screenshot", "has_screenshot"),
                ("favicon", "has_favicon"),
                ("traffic", "has_traffic"),
                ("domain_state", "has_domain_state"),
            )
            if not int(row.get(column) or 0)
        ]
        # Keep the manual-review marker visible while the missing category remains blocking.
        if int(row.get("category_needs_manual") or 0):
            if "category_needs_manual" not in warnings:
                warnings.append("category_needs_manual")
        readiness = "ready" if not blocking else "blocked"
        now = utc_now_iso()
        transitioned_at = now if row.get("previous_readiness") != readiness else None
        await self.d1.run(
            """
            INSERT INTO tool_enrichment_states (
              tool_id, readiness, blocking_json, warnings_json, evaluated_at, transitioned_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_id) DO UPDATE SET
              readiness = excluded.readiness,
              blocking_json = excluded.blocking_json,
              warnings_json = excluded.warnings_json,
              evaluated_at = excluded.evaluated_at,
              transitioned_at = coalesce(excluded.transitioned_at, tool_enrichment_states.transitioned_at),
              updated_at = excluded.updated_at
            """,
            [
                tool_id,
                readiness,
                json.dumps(blocking, separators=(",", ":")),
                json.dumps(warnings, separators=(",", ":")),
                now,
                transitioned_at,
                now,
            ],
        )
        if readiness == "ready":
            await self.d1.run(
                """
                UPDATE tools
                SET status = 'pending_review', updated_at = ?
                WHERE id = ? AND status = 'pending_enrich'
                """,
                [now, tool_id],
            )
        return readiness

    async def evaluate_tools(
        self,
        tool_ids: list[int],
        concurrency: int = 1,
    ) -> dict[str, int]:
        counts = {"evaluated": 0, "ready": 0, "blocked": 0, "missing": 0}
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def evaluate(tool_id: int) -> str:
            async with semaphore:
                return await self.evaluate_tool(tool_id)

        results = await asyncio.gather(*(evaluate(tool_id) for tool_id in tool_ids))
        for readiness in results:
            counts["evaluated"] += 1
            counts[readiness] = counts.get(readiness, 0) + 1
        return counts

    async def reconcile_active_tools(
        self,
        limit: int,
        concurrency: int = 1,
    ) -> dict[str, int]:
        effective_limit = max(1, limit)
        rows = await self.d1.query(
            f"""
            SELECT t.id AS tool_id
            FROM tools t
            LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
            WHERE t.status IN ('pending_enrich', 'pending_review', 'published')
              AND t.duplicate_of_tool_id IS NULL
              AND (
                state.evaluated_at IS NULL
                OR t.updated_at > state.evaluated_at
                OR instr(coalesce(state.blocking_json, ''), '"screenshot"') > 0
                OR instr(coalesce(state.blocking_json, ''), '"published_localization"') > 0
                OR instr(coalesce(state.blocking_json, ''), '"key_feature"') > 0
                OR instr(coalesce(state.blocking_json, ''), '"key_features"') > 0
                OR instr(coalesce(state.blocking_json, ''), '"name_quality"') > 0
                OR instr(coalesce(state.blocking_json, ''), '"content_safety"') > 0
                OR (
                  instr(coalesce(state.blocking_json, ''), '"category"') > 0
                  AND {accepted_primary_taxonomy_predicate('t')}
                )
              )
            ORDER BY CASE WHEN t.status = 'pending_enrich' THEN 0 ELSE 1 END,
                     CASE WHEN state.evaluated_at IS NULL THEN 0 ELSE 1 END,
                     state.evaluated_at,
                     t.id
            LIMIT ?
            """,
            [effective_limit + 1],
        )
        has_more = len(rows) > effective_limit
        counts = await self.evaluate_tools(
            [int(row.get("tool_id") or 0) for row in rows[:effective_limit]],
            concurrency,
        )
        counts["has_more"] = int(has_more)
        return counts

    async def reconcile_pending_tools(self, limit: int) -> dict[str, int]:
        """Backward-compatible entrypoint for callers predating active-catalog reconciliation."""
        return await self.reconcile_active_tools(limit)


