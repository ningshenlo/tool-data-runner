"""Polling loop for Ahrefs DR / RDAP domain facts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


def domain_state_next_delay_seconds(
    counts: dict[str, int],
    batch_limit: int,
    idle_interval_seconds: int,
) -> int:
    """Drain full batches quickly, then back off when the queue is light or idle."""
    configured_interval = max(1, int(idle_interval_seconds))
    claimed = max(0, int(counts.get("claimed") or 0))
    queued = max(0, int(counts.get("queued") or 0))
    effective_limit = max(1, int(batch_limit))

    if claimed >= effective_limit or queued >= effective_limit:
        return configured_interval
    if claimed > 0 or queued > 0:
        return max(configured_interval, 10)
    return max(configured_interval, 60)


@dataclass
class DomainDiscoveryBackoff:
    next_discovery_at: float = 0.0
    empty_scans: int = 0

    def due(self, now: float) -> bool:
        return now >= self.next_discovery_at

    def observe(self, counts: dict[str, int], now: float) -> None:
        if int(counts.get("queued") or 0) > 0 or int(counts.get("claimed") or 0) > 0:
            self.empty_scans = 0
            self.next_discovery_at = now
        elif counts.get("discovery_ran"):
            self.empty_scans = min(5, self.empty_scans + 1)
            self.next_discovery_at = now + min(900, 60 * 2 ** (self.empty_scans - 1))


async def run_domain_state_loop(config: Any, limit: int | None, interval_seconds: int) -> None:
    from runner import (
        AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
        log_error,
        log_info,
        run_domain_state_once,
    )

    effective_limit = limit or config.domain_state_limit
    log_info(
        "domain_state_runner.loop.start",
        interval_seconds=interval_seconds,
        batch_limit=effective_limit,
        max_age_days=config.domain_state_max_age_days,
        requests_per_minute=getattr(
            config,
            "ahrefs_requests_per_minute",
            AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
        ),
    )
    discovery = DomainDiscoveryBackoff()
    while True:
        started_at = time.monotonic()
        next_delay = interval_seconds
        try:
            counts = await run_domain_state_once(
                config, effective_limit, discover_due=discovery.due(started_at)
            )
            discovery.observe(counts, time.monotonic())
            log_info(
                "domain_state_runner.batch.summary",
                duration_ms=round((time.monotonic() - started_at) * 1000),
                **counts,
            )
            next_delay = domain_state_next_delay_seconds(
                counts,
                effective_limit,
                interval_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_error("domain_state_runner.batch.failed", error=str(error)[:500])
        await asyncio.sleep(next_delay)
