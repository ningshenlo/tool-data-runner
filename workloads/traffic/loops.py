"""Polling loop for Similarweb traffic collection."""

from __future__ import annotations

import asyncio
import time
from typing import Any


async def run_loop(config: Any, limit: int | None, interval_seconds: int) -> None:
    from runner import log_error, log_info, run_once

    log_info("traffic_runner.loop.start", interval_seconds=interval_seconds)
    while True:
        started_at = time.monotonic()
        try:
            counts = await run_once(config, limit)
            log_info(
                "traffic_runner.batch.summary",
                duration_ms=round((time.monotonic() - started_at) * 1000),
                **counts,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_error("traffic_runner.batch.failed", error=str(error)[:500])
        await asyncio.sleep(interval_seconds)
