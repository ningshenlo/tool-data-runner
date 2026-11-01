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
from workloads.assets.browser import CloudflareBrowserRunAssetClient
from workloads.assets.enrichment import D1EnrichmentStore
from workloads.assets.publish import D1CatalogPublisher
from workloads.assets.r2 import R2AssetUploader
from workloads.assets.store import D1AssetStore
from workloads.assets.types import (
    AssetFetchResult,
    AssetPipelineError,
    AssetTask,
    ContentSafetyAssessment,
    ContentSafetyError,
    FaviconAsset,
    PageQualityError,
)
from workloads.assets.content import (
    asset_extension,
    asset_mime_type,
    asset_page_url,
    asset_public_url,
    clean_asset_text,
    extract_favicon_href,
    find_favicon_href,
)
from runner import (
    Config,
    D1Client,
    browser_request_headers,
    iso_delta,
    log_debug,
    log_error,
    log_info,
    log_task_result,
    random_browser_user_agent,
    run_with_telemetry,
    utc_now_iso,
)

def generate_letter_svg_icon(domain: str) -> bytes:
    clean = (domain or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    first_char = (clean[:1] or "A").upper()
    hue = sum(ord(c) for c in clean) % 360
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">'
        f'<rect width="128" height="128" rx="28" fill="hsl({hue}, 60%, 48%)"/>'
        f'<text x="50%" y="54%" font-size="68" font-family="sans-serif" font-weight="700" fill="#ffffff" dominant-baseline="middle" text-anchor="middle">{first_char}</text>'
        f'</svg>'
    )
    return svg.encode("utf-8")


async def fetch_favicon_asset(
    page_url: str,
    domain: str,
    html_body: str,
    favicon_href: str = "",
) -> FaviconAsset | None:
    candidate_urls: list[str] = []

    # 1. 优先采用高可用 CDN (Google 128px v2 CDN + DuckDuckGo)
    # 实测响应仅 20-50ms，且能彻底绕过原站 Cloudflare 5秒盾与 WAF 反爬阻断
    if domain:
        candidate_urls.append(
            f"https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://{domain}&size=128"
        )
        candidate_urls.append(f"https://icons.duckduckgo.com/ip3/{domain}.ico")

    # 2. 原站回退 (用于第三方 CDN 尚未收录的全新站或冷门站)
    origin_favicon = urljoin(page_url, favicon_href) if favicon_href else extract_favicon_href(html_body, page_url)
    if origin_favicon:
        candidate_urls.append(origin_favicon)
    candidate_urls.append(urljoin(page_url, "/favicon.ico"))

    seen_urls: set[str] = set()
    ordered_urls: list[str] = []
    for u in candidate_urls:
        if u and u not in seen_urls:
            seen_urls.add(u)
            ordered_urls.append(u)

    for target_url in ordered_urls:
        is_third_party = "gstatic.com" in target_url or "duckduckgo.com" in target_url
        timeout = 6.0 if is_third_party else 10.0
        headers = {
            "User-Agent": random_browser_user_agent(),
            "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.8",
        }
        if not is_third_party:
            headers["Referer"] = page_url

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(target_url, headers=headers)
        except Exception as error:
            log_info("assets.favicon.fetch_failed", domain=domain, url=target_url, error=str(error)[:300])
            continue

        if response.status_code < 200 or response.status_code >= 300:
            log_info("assets.favicon.http_error", domain=domain, url=target_url, status=response.status_code)
            continue

        try:
            content_length = int(response.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        if content_length > 1024 * 1024:
            continue

        body = response.content
        if not body or len(body) <= 50 or len(body) > 1024 * 1024:
            continue

        mime_type = asset_mime_type(target_url, response.headers.get("content-type", ""))
        extension = asset_extension(target_url, mime_type)
        return FaviconAsset(
            body=body,
            key=f"{domain}/favicon-{int(time.time() * 1000)}{extension}",
            mime_type=mime_type,
        )

    # 3. 若以上均失败（CDN 未收录且原站 /favicon.ico 404），最后做一次原站动态 HTML 获取与 link 标签发现
    if not html_body and page_url:
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                page_response = await client.get(
                    page_url,
                    headers={
                        "User-Agent": random_browser_user_agent(),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
            if 200 <= page_response.status_code < 300:
                discovered_url = extract_favicon_href(page_response.text[:200000], str(page_response.url))
                if discovered_url:
                    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                        resp = await client.get(
                            discovered_url,
                            headers={
                                "User-Agent": random_browser_user_agent(),
                                "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.8",
                                "Referer": page_url,
                            },
                        )
                        if 200 <= resp.status_code < 300 and 50 < len(resp.content) <= 1024 * 1024:
                            mime_type = asset_mime_type(discovered_url, resp.headers.get("content-type", ""))
                            extension = asset_extension(discovered_url, mime_type)
                            return FaviconAsset(
                                body=resp.content,
                                key=f"{domain}/favicon-{int(time.time() * 1000)}{extension}",
                                mime_type=mime_type,
                            )
        except Exception as error:
            log_info("assets.favicon.discover_failed", domain=domain, url=page_url, error=str(error)[:300])

    # 4. 终极兜底：当原站和第三方服务均未能返回有效图片时，生成品牌首字母矢量 SVG
    log_info("assets.favicon.fallback_to_svg", domain=domain, page_url=page_url)
    svg_body = generate_letter_svg_icon(domain)
    return FaviconAsset(
        body=svg_body,
        key=f"{domain}/favicon-{int(time.time() * 1000)}.svg",
        mime_type="image/svg+xml",
    )


async def process_asset_task(
    task: AssetTask,
    browser_client: CloudflareBrowserRunAssetClient,
    uploader: R2AssetUploader,
    store: D1AssetStore,
    public_base_url: str,
    max_retries: int,
) -> str:
    last_error = "not_started"
    last_retryable = True
    last_retry_after_hours: int | None = None
    for attempt in range(max_retries + 1):
        missing_before = await store.missing_asset_requirements(task.tool_id)
        if not missing_before:
            if not await store.complete_task(task, "done"):
                return "stale"
            log_debug(
                "asset_task.fetch_attempt.skipped",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                reason="requirements_already_complete",
            )
            return "done"

        stage_errors: list[str] = []
        stage_retryable: list[bool] = []
        core_result: AssetFetchResult | None = None

        def record_stage_error(stage: str, error: Any, *, retryable: bool | None = None) -> None:
            message = str(error).strip()[:500] or type(error).__name__
            stage_errors.append(f"{stage}={message}")
            stage_retryable.append(
                bool(getattr(error, "retryable", True))
                if retryable is None
                else retryable
            )

        try:
            log_debug(
                "asset_task.fetch_attempt.start",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                attempt=attempt + 1,
                max_attempts=max_retries + 1,
                requirements=missing_before,
            )

            preflight_method = getattr(browser_client, "preflight_homepage", None)
            if callable(preflight_method):
                page_assessment = await preflight_method(task)
                if not await store.renew_lease(task):
                    return "stale"
                await store.record_page_quality(task, page_assessment)
                if not page_assessment.is_valid:
                    retry_hours = 6 if task.attempts <= 1 else 24 if task.attempts == 2 else 72
                    raise PageQualityError(page_assessment, retry_after_hours=retry_hours)

            if "description" in missing_before or "content_safety" in missing_before:
                try:
                    core_result = await browser_client.fetch_homepage_core_metadata(task)
                    if core_result.page_state != "valid_product_page":
                        assessment = PageQualityAssessment(
                            core_result.page_state,
                            core_result.page_state_reason or "core_metadata_page_invalid",
                            core_result.page_title,
                        )
                        retry_hours = 6 if task.attempts <= 1 else 24 if task.attempts == 2 else 72
                        raise PageQualityError(assessment, retry_after_hours=retry_hours)
                    if not await store.renew_lease(task):
                        return "stale"
                    content_safety = ContentSafetyAssessment(
                        core_result.content_safety_status,
                        core_result.content_safety_score,
                        core_result.content_safety_confidence,
                        core_result.content_safety_reason or "missing_content_safety_reason",
                        core_result.content_safety_evidence or [],
                        core_result.content_safety_source or "homepage_core_metadata",
                    )
                    stored_safety = await store.save_content_safety(task, content_safety)
                    if stored_safety.status != "safe":
                        raise ContentSafetyError(stored_safety)
                    await store.save_tool_localization(task, core_result)
                    if core_result.metadata_error:
                        record_stage_error(
                            "description",
                            core_result.metadata_error,
                            retryable=core_result.metadata_retryable,
                        )
                except Exception as error:
                    if isinstance(error, (PageQualityError, ContentSafetyError)):
                        raise
                    record_stage_error("description", error)

            if "screenshot" in missing_before:
                try:
                    screenshot_result = await browser_client.capture_homepage_screenshot(task)
                    if not await store.renew_lease(task):
                        return "stale"
                    screenshot_key = f"{task.normalized_domain}/{int(time.time() * 1000)}.png"
                    await uploader.put_object(screenshot_key, screenshot_result.screenshot, "image/png")
                    await store.upsert_tool_asset(
                        task,
                        "screenshot",
                        screenshot_key,
                        asset_public_url(public_base_url, screenshot_key),
                        "image/png",
                        1280,
                        720,
                    )
                except Exception as error:
                    record_stage_error("screenshot", error)

            favicon: FaviconAsset | None = None
            if "favicon" in missing_before:
                try:
                    # Resolve through runner so unit tests can patch runner.fetch_favicon_asset.
                    import runner as _runner

                    favicon = await _runner.fetch_favicon_asset(
                        core_result.final_url if core_result else asset_page_url(task),
                        task.normalized_domain,
                        core_result.html if core_result else "",
                        core_result.favicon_href if core_result else "",
                    )
                    if favicon is None:
                        raise AssetPipelineError("favicon_empty", retryable=True)
                    if not await store.renew_lease(task):
                        return "stale"
                    await uploader.put_object(favicon.key, favicon.body, favicon.mime_type)
                    await store.upsert_tool_asset(
                        task,
                        "favicon",
                        favicon.key,
                        asset_public_url(public_base_url, favicon.key),
                        favicon.mime_type,
                        None,
                        None,
                    )
                except Exception as error:
                    record_stage_error("favicon", error)

            missing_requirements = await store.missing_asset_requirements(task.tool_id)
            # Screenshot and favicon are both durable catalog assets. A missing
            # favicon must keep the task in the bounded retry state machine;
            # marking the task done here would let the 24-hour missing-data scan
            # revive it forever. Successful stages are still skipped on every
            # retry because missing_before is recomputed at the top of the loop.
            blocking_requirements = list(missing_requirements)
            if blocking_requirements:
                details = [f"missing={','.join(blocking_requirements)}"]
                if stage_errors:
                    details.append("stages=" + "; ".join(stage_errors))
                last_error = "asset_enrichment_incomplete: " + "; ".join(details)
                last_retryable = any(stage_retryable) if stage_retryable else True
                raise AssetPipelineError(last_error, retryable=last_retryable)

            if not await store.complete_task(task, "done"):
                return "stale"
            log_debug(
                "asset_task.fetch_attempt.done",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                status="done",
                favicon=bool(favicon),
                requirements_processed=missing_before,
                remaining_warnings=missing_requirements,
            )
            return "done"
        except Exception as error:
            last_error = str(error)[:900]
            last_retryable = bool(getattr(error, "retryable", True))
            last_retry_after_hours = getattr(error, "retry_after_hours", None)
            log_error(
                "asset_task.fetch_attempt.failed",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                attempt=attempt + 1,
                error=last_error,
            )
            if attempt < max_retries and last_retryable and not isinstance(error, PageQualityError):
                await asyncio.sleep(random.uniform(1.0, 3.0))
            else:
                break

    completed = await store.complete_task(
        task,
        "failed",
        last_error,
        retryable=last_retryable,
        retry_after_hours=last_retry_after_hours,
    )
    return "failed" if completed else "stale"


async def _run_assets_once(config: Config, d1: D1Client, limit: int | None = None) -> dict[str, int]:
    effective_limit = limit or config.asset_limit
    concurrency = getattr(config, "asset_concurrency", config.concurrency)
    reconciliation_limit = getattr(config, "enrichment_reconcile_limit", 100)
    reconciliation_concurrency = getattr(
        config,
        "enrichment_reconcile_concurrency",
        min(5, concurrency),
    )
    log_info(
        "assets_runner.batch.start",
        limit=effective_limit,
        concurrency=concurrency,
        enrichment_reconcile_limit=reconciliation_limit,
        enrichment_reconcile_concurrency=reconciliation_concurrency,
        category_owner="taxonomy-worker",
    )
    publication = {"selected": 0, "published": 0, "skipped": 0, "failed": 0}
    if getattr(config, "catalog_auto_publish_enabled", True):
        try:
            publication.update(
                await D1CatalogPublisher(
                    d1,
                    config.runner_instance_id,
                ).publish_ready(
                    getattr(config, "catalog_auto_publish_limit", 25)
                )
            )
            log_info("catalog_auto_publish.batch.done", **publication)
        except Exception as error:
            publication["failed"] = 1
            log_error("catalog_auto_publish.batch.failed", error=str(error)[:500])
    else:
        log_debug("catalog_auto_publish.disabled", kill_switch="CATALOG_AUTO_PUBLISH_ENABLED=0")
    browser_client = CloudflareBrowserRunAssetClient(config)
    uploader = R2AssetUploader(config)
    await uploader.check_access()
    store = D1AssetStore(d1)
    revived = await store.revive_incomplete_dead_letter_tasks(effective_limit)
    log_info("assets_runner.revive_incomplete_dead_letters.done", revived=revived)
    queued = await store.queue_missing_asset_tasks(effective_limit)
    log_info("assets_runner.queue_missing_asset_tasks.done", queued=queued)
    tasks = await store.claim_due_tasks(effective_limit, config.runner_instance_id)
    log_info("assets_runner.claim_due_tasks.done", claimed=len(tasks))

    semaphore = asyncio.Semaphore(concurrency)
    counts = {
        "asset_revived": revived,
        "asset_queued": queued,
        "claimed": len(tasks),
        "done": 0,
        "failed": 0,
        "stale": 0,
        "enrichment_ready": 0,
        "enrichment_blocked": 0,
        "enrichment_evaluated": 0,
        "enrichment_has_more": 0,
        "auto_publish_selected": publication["selected"],
        "auto_publish_published": publication["published"],
        "auto_publish_skipped": publication["skipped"],
        "auto_publish_failed": publication["failed"],
    }

    async def guarded(task: AssetTask) -> None:
        async with semaphore:
            try:
                status = await process_asset_task(
                    task,
                    browser_client,
                    uploader,
                    store,
                    config.r2_public_base_url,
                    config.max_retries,
                )
            except Exception as error:
                status = "failed"
                log_error(
                    "asset_task.failed_with_exception",
                    tool_id=task.tool_id,
                    slug=task.canonical_slug,
                    domain=task.normalized_domain,
                    error=str(error)[:300],
                )
                if not await store.complete_task(task, "failed", str(error)[:900]):
                    status = "stale"
            counts[status] = counts.get(status, 0) + 1
            log_task_result(
                "asset_task.done",
                status,
                tool_id=task.tool_id,
                slug=task.canonical_slug,
            )

    enrichment = D1EnrichmentStore(d1)
    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))
        task_reconciliation = await enrichment.evaluate_tools(
            list(dict.fromkeys(task.tool_id for task in tasks)),
            reconciliation_concurrency,
        )
        counts["enrichment_evaluated"] += task_reconciliation["evaluated"]
        counts["enrichment_ready"] += task_reconciliation["ready"]
        counts["enrichment_blocked"] += task_reconciliation["blocked"]

    reconciliation = await enrichment.reconcile_active_tools(
        reconciliation_limit,
        reconciliation_concurrency,
    )
    counts["enrichment_evaluated"] += reconciliation["evaluated"]
    counts["enrichment_ready"] += reconciliation["ready"]
    counts["enrichment_blocked"] += reconciliation["blocked"]
    counts["enrichment_has_more"] = reconciliation["has_more"]

    return counts


async def run_assets_once(config: Config, limit: int | None = None) -> dict[str, int]:
    async with D1Client(config) as d1:
        return await run_with_telemetry(
            config,
            d1,
            "assets",
            lambda: _run_assets_once(config, d1, limit),
        )

