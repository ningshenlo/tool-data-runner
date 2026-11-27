"""Non-blocking Similarweb screenshot sync into R2 (`sitesimgs`) and `tool_assets`."""

from __future__ import annotations

import asyncio
import os
import struct
import time
from typing import Any

import httpx

from libs.logging_util import log_error, log_info
from shared.db.client import PostgresClient, PostgresStatement
from workloads.assets.constants import ASSET_DB_STORAGE_BUCKET

MIN_SCREENSHOT_BYTES = 4000


def _asset_public_url(public_base_url: str, key: str) -> str | None:
    base = (public_base_url or "").strip().rstrip("/")
    return f"{base}/{key.lstrip('/')}" if base else None
MAX_SCREENSHOT_BYTES = 2_000_000
# Similarweb returns a 160x120 (2,229-byte) generic placeholder when no site preview exists; require >= 200x150.
MIN_SCREENSHOT_WIDTH = 200
MIN_SCREENSHOT_HEIGHT = 150


def _extension_for_mime(mime_type: str) -> str:
    clean = (mime_type or "").split(";", 1)[0].strip().lower()
    if clean == "image/webp":
        return ".webp"
    if clean in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return ".png"


def parse_image_dimensions(body: bytes, content_type: str = "") -> tuple[int, int] | None:
    """Validate screenshot bytes and extract (width, height) from PNG, WebP, or JPEG."""
    if not body or len(body) < MIN_SCREENSHOT_BYTES or len(body) > MAX_SCREENSHOT_BYTES:
        return None

    # PNG: 8-byte signature + IHDR chunk (width/height at bytes 16..24)
    if len(body) >= 24 and body[:8] == b"\x89PNG\r\n\x1a\n" and body[12:16] == b"IHDR":
        width, height = struct.unpack(">II", body[16:24])
        return (width, height) if width >= MIN_SCREENSHOT_WIDTH and height >= MIN_SCREENSHOT_HEIGHT else None

    # WebP: RIFF....WEBP followed by VP8 / VP8L / VP8X
    if len(body) >= 30 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        chunk = body[12:16]
        if chunk == b"VP8X" and len(body) >= 30:
            w = int.from_bytes(body[24:27], "little") + 1
            h = int.from_bytes(body[27:30], "little") + 1
            return (w, h) if w >= MIN_SCREENSHOT_WIDTH and h >= MIN_SCREENSHOT_HEIGHT else None
        if chunk == b"VP8L" and len(body) >= 25:
            bits = int.from_bytes(body[21:25], "little")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return (w, h) if w >= MIN_SCREENSHOT_WIDTH and h >= MIN_SCREENSHOT_HEIGHT else None
        if chunk == b"VP8 " and len(body) >= 30 and body[23:26] == b"\x9d\x01\x2a":
            w = int.from_bytes(body[26:28], "little") & 0x3FFF
            h = int.from_bytes(body[28:30], "little") & 0x3FFF
            return (w, h) if w >= MIN_SCREENSHOT_WIDTH and h >= MIN_SCREENSHOT_HEIGHT else None

    # JPEG SOF0/SOF2
    if len(body) >= 4 and body[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(body):
            if body[i] != 0xFF:
                i += 1
                continue
            marker = body[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", body[i + 5 : i + 9])
                return (w, h) if w >= MIN_SCREENSHOT_WIDTH and h >= MIN_SCREENSHOT_HEIGHT else None
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(body[i + 2 : i + 4], "big")
            if seg_len < 2:
                break
            i += 2 + seg_len

    # Fallback: reject non-image payloads even if content-type claims image/
    if b"html" in body[:256].lower():
        return None
    return None


async def select_missing_sw_screenshot_candidates(
    pg: PostgresClient,
    limit: int,
    *,
    tool_ids: list[int] | None = None,
    after_tool_id: int = 0,
) -> list[dict[str, Any]]:
    """Find tools missing a current screenshot that already have LargeScreenshot in snapshots."""
    effective_limit = max(1, min(2000, int(limit)))
    id_filter = ""
    params: list[Any] = []
    if tool_ids:
        clean_ids = [int(tid) for tid in tool_ids if int(tid) > 0]
        if not clean_ids:
            return []
        id_filter = "AND t.id = ANY(%s)"
        params.append(clean_ids)
    elif after_tool_id > 0:
        id_filter = "AND t.id > %s"
        params.append(int(after_tool_id))
    params.append(effective_limit)

    sql = f"""
        WITH latest_snap AS (
          SELECT DISTINCT ON (normalized_domain)
            normalized_domain,
            raw_payload::jsonb ->> 'LargeScreenshot' AS large_screenshot
          FROM domain_traffic_snapshots
          WHERE raw_payload IS NOT NULL
            AND raw_payload LIKE '{{%%'
            AND length(raw_payload) > 50
          ORDER BY normalized_domain, id DESC
        )
        SELECT
          t.id AS tool_id,
          t.canonical_slug,
          t.normalized_domain,
          t.seo_index_status,
          ls.large_screenshot
        FROM tools t
        JOIN latest_snap ls ON ls.normalized_domain = t.normalized_domain
        WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
          AND t.duplicate_of_tool_id IS NULL
          AND ls.large_screenshot LIKE 'http%%'
          AND NOT EXISTS (
            SELECT 1
            FROM tool_assets ta
            WHERE ta.tool_id = t.id
              AND ta.asset_kind = 'screenshot'
              AND coalesce(ta.locale_code, '') = ''
              AND ta.is_current = 1
          )
          {id_filter}
        ORDER BY
          CASE WHEN t.seo_index_status = 'indexable' THEN 0 ELSE 1 END ASC,
          t.id ASC
        LIMIT %s
    """
    return await pg.query(PostgresStatement(sql=sql, params=tuple(params), label="sw_screenshots.select_candidates"))


async def download_similarweb_screenshot(
    url: str,
    client: httpx.AsyncClient,
) -> tuple[bytes, str, int, int]:
    response = await client.get(
        url,
        headers={"Accept": "image/png,image/webp,image/jpeg,image/*;q=0.8"},
        follow_redirects=True,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"sw_screenshot_http_{response.status_code}")

    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        raise RuntimeError(f"sw_screenshot_non_image:{content_type or 'missing'}")

    body = response.content
    dims = parse_image_dimensions(body, content_type)
    if dims is None:
        raise RuntimeError(f"sw_screenshot_invalid_dimensions:bytes={len(body)}")
    return body, content_type, dims[0], dims[1]


async def insert_screenshot_if_missing(
    pg: PostgresClient,
    *,
    tool_id: int,
    storage_object_path: str,
    public_url: str | None,
    mime_type: str,
    width: int,
    height: int,
) -> bool:
    """Insert a screenshot row ONLY if the tool still has no current screenshot."""
    meta = await pg.run(
        PostgresStatement(
            sql="""
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
                SELECT %s, NULL, 'screenshot', %s, %s, %s, %s, %s, %s, 1
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM tool_assets ta
                  WHERE ta.tool_id = %s
                    AND ta.asset_kind = 'screenshot'
                    AND coalesce(ta.locale_code, '') = ''
                    AND ta.is_current = 1
                )
            """,
            params=(
                tool_id,
                ASSET_DB_STORAGE_BUCKET,
                storage_object_path,
                public_url,
                mime_type,
                width,
                height,
                tool_id,
            ),
            label="sw_screenshots.insert_if_missing",
        )
    )
    return int(meta.get("changes") or 0) > 0


async def sync_similarweb_screenshots(
    config: Any,
    *,
    limit: int = 50,
    concurrency: int = 8,
    dry_run: bool = False,
    tool_ids: list[int] | None = None,
    after_tool_id: int = 0,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Download Similarweb LargeScreenshot URLs into R2 and `tool_assets` without blocking."""
    resolved_db_url = (database_url or os.getenv("DATABASE_URL") or "").strip()
    if not resolved_db_url:
        raise RuntimeError("Missing DATABASE_URL for Similarweb screenshot sync.")

    if dry_run:
        uploader = None
    else:
        from workloads.assets.r2 import R2AssetUploader

        uploader = R2AssetUploader(config)
    public_base_url = getattr(config, "r2_public_base_url", "") or ""
    semaphore = asyncio.Semaphore(max(1, min(16, int(concurrency))))

    summary: dict[str, Any] = {
        "selected": 0,
        "downloaded": 0,
        "uploaded": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
        "samples": [],
    }

    async with PostgresClient(resolved_db_url, max_connections=min(4, max(1, int(concurrency)))) as pg:
        candidates = await select_missing_sw_screenshot_candidates(
            pg,
            limit,
            tool_ids=tool_ids,
            after_tool_id=after_tool_id,
        )
        summary["selected"] = len(candidates)
        summary["last_tool_id"] = max((int(r["tool_id"]) for r in candidates), default=0)
        if not candidates:
            return summary

        async with httpx.AsyncClient(timeout=10.0) as http_client:
            async def process_one(row: dict[str, Any]) -> dict[str, Any]:
                tool_id = int(row["tool_id"])
                domain = str(row["normalized_domain"])
                source_url = str(row["large_screenshot"])
                async with semaphore:
                    try:
                        body, mime_type, width, height = await download_similarweb_screenshot(source_url, http_client)
                        ext = _extension_for_mime(mime_type)
                        key = f"sw/{domain}/{int(time.time() * 1000)}{ext}"
                        pub_url = _asset_public_url(public_base_url, key)
                        if dry_run:
                            return {
                                "status": "dry_run_ok",
                                "tool_id": tool_id,
                                "domain": domain,
                                "mime_type": mime_type,
                                "width": width,
                                "height": height,
                                "bytes": len(body),
                                "r2_key": key,
                                "source_url": source_url,
                            }

                        assert uploader is not None
                        await uploader.put_object(key, body, mime_type)
                        inserted = await insert_screenshot_if_missing(
                            pg,
                            tool_id=tool_id,
                            storage_object_path=key,
                            public_url=pub_url,
                            mime_type=mime_type,
                            width=width,
                            height=height,
                        )
                        return {
                            "status": "inserted" if inserted else "skipped_existing",
                            "tool_id": tool_id,
                            "domain": domain,
                            "mime_type": mime_type,
                            "width": width,
                            "height": height,
                            "bytes": len(body),
                            "r2_key": key,
                            "public_url": pub_url,
                        }
                    except Exception as error:
                        log_error(
                            "sw_screenshot.item_failed",
                            tool_id=tool_id,
                            domain=domain,
                            error=str(error)[:300],
                        )
                        return {
                            "status": "failed",
                            "tool_id": tool_id,
                            "domain": domain,
                            "error": str(error)[:200],
                        }

            results = await asyncio.gather(*(process_one(row) for row in candidates))

    for item in results:
        status = item.get("status")
        if status == "dry_run_ok":
            summary["downloaded"] += 1
        elif status == "inserted":
            summary["downloaded"] += 1
            summary["uploaded"] += 1
            summary["inserted"] += 1
        elif status == "skipped_existing":
            summary["downloaded"] += 1
            summary["uploaded"] += 1
            summary["skipped_existing"] += 1
        else:
            summary["failed"] += 1
        if len(summary["samples"]) < 20:
            summary["samples"].append(item)

    log_info(
        "sw_screenshot.sync.done",
        selected=summary["selected"],
        downloaded=summary["downloaded"],
        inserted=summary["inserted"],
        skipped_existing=summary["skipped_existing"],
        failed=summary["failed"],
        dry_run=summary["dry_run"],
    )
    return summary
