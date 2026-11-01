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
from runner import D1Client, log_info, utc_now_iso

class D1CatalogPublisher:
    def __init__(
        self,
        d1: D1Client,
        runner_instance_id: str,
        policy_version: str = CATALOG_AUTO_PUBLISH_POLICY_VERSION,
    ):
        self.d1 = d1
        self.runner_instance_id = runner_instance_id
        self.policy_version = policy_version

    async def publish_ready(self, limit: int) -> dict[str, int]:
        effective_limit = min(CATALOG_AUTO_PUBLISH_MAX_LIMIT, max(1, int(limit)))
        predicate = f"({tool_live_ready_predicate('t')}) AND ({tool_auto_publish_admission_predicate('t')})"
        rows = await self.d1.query(
            f"""
            SELECT t.id
            FROM tools t
            WHERE t.status = 'pending_review'
              AND t.duplicate_of_tool_id IS NULL
              AND ({predicate})
            ORDER BY t.id
            LIMIT ?
            """,
            [effective_limit],
            operation="catalog.auto_publish.select",
        )
        tool_ids = [int(row["id"]) for row in rows if int(row.get("id") or 0) > 0]
        if not tool_ids:
            return {"selected": 0, "published": 0, "skipped": 0}

        placeholders = ", ".join("?" for _ in tool_ids)
        old_value = json.dumps({"status": "pending_review"}, separators=(",", ":"))
        new_value = json.dumps({"status": "published"}, separators=(",", ":"))
        notes = (
            "Automated catalog publish"
            f"; policy={self.policy_version}"
            f"; instance={self.runner_instance_id}"
        )
        results = await self.d1.batch(
            [
                (
                    f"""
                    INSERT INTO tool_change_log (
                      tool_id, change_type, old_value, new_value, verified_at, notes
                    )
                    SELECT
                      t.id,
                      'status_changed',
                      ?,
                      ?,
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                      ?
                    FROM tools t
                    WHERE t.id IN ({placeholders})
                      AND t.status = 'pending_review'
                      AND t.duplicate_of_tool_id IS NULL
                      AND ({predicate})
                    """,
                    [old_value, new_value, notes, *tool_ids],
                ),
                (
                    f"""
                    UPDATE tools
                    SET status = 'published',
                        first_published_at = coalesce(
                          first_published_at,
                          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id IN ({placeholders})
                      AND status = 'pending_review'
                      AND duplicate_of_tool_id IS NULL
                      AND ({tool_live_ready_predicate('tools')})
                      AND ({tool_auto_publish_admission_predicate('tools')})
                    RETURNING id
                    """,
                    tool_ids,
                ),
            ],
            operation="catalog.auto_publish.commit",
        )
        published = 0
        if len(results) >= 2:
            published_rows = results[1].get("results") or []
            published = len(published_rows)
        return {
            "selected": len(tool_ids),
            "published": published,
            "skipped": max(0, len(tool_ids) - published),
        }


