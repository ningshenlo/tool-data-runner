"""Polling loop and scheduling helpers for taxonomy-worker."""

from __future__ import annotations

import asyncio
import time
from typing import Any


async def run_taxonomy_once(config: Any) -> dict[str, int]:
    from runner import D1Client, run_with_telemetry

    if not config.taxonomy_auto_enabled:
        return {"disabled": 1}

    async def operation() -> dict[str, int]:
        # Older tests and maintenance callers may pass a lightweight config
        # object without this field; only the real loaded Config opts in.
        if getattr(config, "taxonomy_batch_enabled", False):
            from taxonomy_batch import run_openai_taxonomy_batch_once

            return await run_openai_taxonomy_batch_once(
                config,
                config.taxonomy_limit,
            )
        # Import inside the telemetered operation so packaging/import failures
        # are visible as failed taxonomy runs instead of disappearing in logs.
        from taxonomy_shadow import run_shadow_taxonomy

        return await run_shadow_taxonomy(
            config,
            config.taxonomy_limit,
            allow_unresolved_entity=True,
            include_auto_non_product_recheck=getattr(
                config, "taxonomy_recheck_auto_non_product", True
            ),
            include_capabilities=getattr(
                config, "taxonomy_capabilities_enabled", True
            ),
            include_capability_backfill=getattr(
                config, "taxonomy_capability_backfill_enabled", True
            ),
            capability_candidate_limit=getattr(
                config, "taxonomy_capability_candidate_limit", 96
            ),
            concurrency=config.taxonomy_concurrency,
            auto_accept_threshold=config.taxonomy_auto_accept_confidence,
        )

    async with D1Client(config) as telemetry_d1:
        return await run_with_telemetry(
            config,
            telemetry_d1,
            "taxonomy",
            operation,
        )


def taxonomy_next_delay_seconds(config: Any, counts: dict[str, int]) -> int:
    """Poll only when drained; immediately continue while a full batch signals backlog."""
    if int(counts.get("provider_blocked") or 0) > 0:
        return int(config.taxonomy_provider_backoff_seconds)
    if int(counts.get("auto_non_product_recheck_selected") or 0) > 0:
        # Incident repair is intentionally paced so operators can inspect each
        # batch and trip the kill switch before another provider-cost burst.
        return int(config.taxonomy_interval_seconds)
    if (
        int(counts.get("capability_backfill_selected") or 0) > 0
        or int(counts.get("capability_only_selected") or 0) > 0
    ):
        # Backfill is deliberately paced: one bounded model call per stored
        # profile, with an operator-visible interval between paid batches.
        return int(config.taxonomy_interval_seconds)
    if int(counts.get("terminal_has_more") or 0) > 0:
        # Lifecycle-only reconciliation spends no provider budget. Drain it
        # immediately instead of leaving hundreds of terminal tools behind a
        # five-minute taxonomy polling interval.
        return 0
    if int(counts.get("selected") or 0) >= int(config.taxonomy_limit):
        return 0
    return int(config.taxonomy_interval_seconds)


def taxonomy_batch_has_activity(counts: dict[str, int]) -> bool:
    """Return whether a taxonomy pass produced operator-relevant activity."""
    activity_fields = (
        "selected",
        "succeeded",
        "partial",
        "failed",
        "skipped",
        "provider_blocked",
        "deferred",
        "anomaly_candidates",
        "anomaly_scan_failed",
        "reclassification_selected",
        "reclassification_succeeded",
        "reclassification_needs_manual",
        "reclassification_failed",
        "auto_non_product_recheck_selected",
        "auto_non_product_recheck_succeeded",
        "auto_non_product_recheck_partial",
        "auto_non_product_recheck_failed",
        "auto_non_product_recheck_skipped",
        "auto_non_product_recheck_deferred",
        "batches_submitted",
        "batches_completed",
        "batch_requests_completed",
        "batch_requests_failed",
        "submit_failed",
        "submit_retries_scheduled",
        "model_retries_resumed",
        "source_retry_scheduled",
        "source_retry_exhausted",
        "terminal_selected",
        "terminal_pending_review",
        "terminal_rejected",
    )
    return any(int(counts.get(field) or 0) > 0 for field in activity_fields)


def taxonomy_idle_heartbeat_due(
    last_emitted_at: float | None,
    now: float,
    interval_seconds: int,
) -> bool:
    """Emit the first idle heartbeat, then no more than once per interval."""
    return last_emitted_at is None or now - last_emitted_at >= max(1, interval_seconds)


async def run_taxonomy_loop(
    config: Any,
    service_telemetry: Any | None = None,
) -> None:
    from runner import log_debug, log_error, log_info, report_service_schedule

    idle_heartbeat_seconds = max(3600, int(config.taxonomy_interval_seconds))
    log_info(
        "taxonomy_runner.loop.start",
        interval_seconds=config.taxonomy_interval_seconds,
        idle_heartbeat_seconds=idle_heartbeat_seconds,
        limit=config.taxonomy_limit,
        concurrency=config.taxonomy_concurrency,
        auto_accept_confidence=config.taxonomy_auto_accept_confidence,
        recheck_auto_non_product=getattr(
            config, "taxonomy_recheck_auto_non_product", True
        ),
        primary_only=True,
    )
    if not config.taxonomy_auto_enabled:
        log_info("taxonomy_runner.disabled", kill_switch="TAXONOMY_AUTO_ENABLED=0")
        while True:
            await asyncio.sleep(max(60, config.taxonomy_interval_seconds))
    # Only the deprecated combined profile needs startup staggering. The
    # isolated taxonomy worker should begin immediately.
    if len(tuple(getattr(config, "runner_workloads", ()) or ())) > 1:
        await asyncio.sleep(10)
    consecutive_idle_batches = 0
    last_idle_log_at: float | None = None
    while True:
        delay = config.taxonomy_interval_seconds
        backoff_reason: str | None = None
        started_at = time.monotonic()
        try:
            counts = await run_taxonomy_once(config)
            duration_ms = round((time.monotonic() - started_at) * 1000)
            delay = taxonomy_next_delay_seconds(config, counts)
            if int(counts.get("provider_blocked") or 0) > 0:
                backoff_reason = "taxonomy_provider_blocked"
            if taxonomy_batch_has_activity(counts):
                consecutive_idle_batches = 0
                last_idle_log_at = None
                log_info(
                    "taxonomy_runner.batch.summary",
                    duration_ms=duration_ms,
                    **counts,
                )
            else:
                consecutive_idle_batches += 1
                now = time.monotonic()
                if taxonomy_idle_heartbeat_due(
                    last_idle_log_at,
                    now,
                    idle_heartbeat_seconds,
                ):
                    log_info(
                        "taxonomy_runner.idle",
                        duration_ms=duration_ms,
                        consecutive_empty_batches=consecutive_idle_batches,
                        next_poll_seconds=delay,
                    )
                    last_idle_log_at = now
                else:
                    log_debug(
                        "taxonomy_runner.batch.idle",
                        duration_ms=duration_ms,
                        consecutive_empty_batches=consecutive_idle_batches,
                        next_poll_seconds=delay,
                    )
            if delay == 0:
                log_info(
                    "taxonomy_runner.backlog.continue",
                    selected=counts.get("selected", 0),
                    limit=config.taxonomy_limit,
                )
            elif int(counts.get("provider_blocked") or 0) > 0:
                log_error(
                    "taxonomy_runner.provider_backoff",
                    delay_seconds=delay,
                    provider_blocked=counts.get("provider_blocked", 0),
                    deferred=counts.get("deferred", 0),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            consecutive_idle_batches = 0
            last_idle_log_at = None
            # Startup/telemetry failures are usually transient. Retrying soon
            # prevents a single D1 collision from hiding taxonomy for the full
            # normal 15-minute cadence.
            delay = min(60, config.taxonomy_interval_seconds)
            log_error("taxonomy_runner.batch.failed", error=str(error)[:500])
            log_info("taxonomy_runner.batch.retry_scheduled", delay_seconds=delay)
        await report_service_schedule(
            service_telemetry,
            delay,
            backoff_reason=backoff_reason,
        )
        await asyncio.sleep(delay)
