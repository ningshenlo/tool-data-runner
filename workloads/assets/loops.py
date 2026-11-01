"""Polling loop for assets-worker."""

from __future__ import annotations

import asyncio
import time
from typing import Any


def assets_next_delay_seconds(
    counts: dict[str, int],
    batch_limit: int,
    idle_interval_seconds: int,
) -> int:
    """Drain known backlog immediately and poll briefly only after a partial batch."""
    claimed = max(0, int(counts.get("claimed") or 0))
    queued = max(0, int(counts.get("asset_queued") or 0))
    revived = max(0, int(counts.get("asset_revived") or 0))
    effective_limit = max(1, int(batch_limit))

    if claimed >= effective_limit or int(counts.get("enrichment_has_more") or 0):
        return 0
    if claimed > 0 or queued > 0 or revived > 0:
        return min(max(1, int(idle_interval_seconds)), 5)
    return max(1, int(idle_interval_seconds))


async def run_assets_loop(config: Any, limit: int | None, interval_seconds: int) -> None:
    # Batch helpers still live in runner.py; imported lazily to avoid import cycles.
    from runner import log_error, log_info, run_assets_once

    effective_limit = limit or config.asset_limit
    log_info(
        "assets_runner.loop.start",
        interval_seconds=interval_seconds,
        batch_limit=effective_limit,
        concurrency=getattr(config, "asset_concurrency", config.concurrency),
        enrichment_reconcile_limit=getattr(config, "enrichment_reconcile_limit", 100),
    )
    while True:
        started_at = time.monotonic()
        next_delay = interval_seconds
        try:
            counts = await run_assets_once(config, effective_limit)
            log_info(
                "assets_runner.batch.summary",
                duration_ms=round((time.monotonic() - started_at) * 1000),
                **counts,
            )
            next_delay = assets_next_delay_seconds(
                counts,
                effective_limit,
                interval_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_error("assets_runner.batch.failed", error=str(error)[:500])
        if next_delay == 0:
            await asyncio.sleep(0)
            continue
        await asyncio.sleep(next_delay)
