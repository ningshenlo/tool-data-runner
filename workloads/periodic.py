"""Combined profiles: periodic-facts-worker and deprecated --all."""

from __future__ import annotations

import asyncio
from typing import Any


async def run_periodic_facts_once(
    config: Any,
    traffic_limit: int | None = None,
    domain_state_limit: int | None = None,
) -> dict[str, int]:
    import runner as r

    traffic_counts, domain_counts = await asyncio.gather(
        r.run_once(config, traffic_limit or config.limit),
        r.run_domain_state_once(config, domain_state_limit or config.domain_state_limit),
    )
    return {
        **{f"traffic_{key}": value for key, value in traffic_counts.items()},
        **{f"domain_{key}": value for key, value in domain_counts.items()},
    }


async def run_periodic_facts_loop(
    config: Any,
    traffic_limit: int | None,
    interval_seconds: int,
) -> None:
    import runner as r

    traffic_interval = interval_seconds or config.poll_interval_seconds
    domain_state_interval = getattr(config, "domain_state_poll_interval_seconds", 1)
    r.log_info(
        "periodic_facts_runner.loop.start",
        traffic_interval_seconds=traffic_interval,
        domain_state_interval_seconds=domain_state_interval,
        traffic_concurrency=getattr(config, "traffic_concurrency", config.concurrency),
        domain_concurrency=getattr(config, "domain_state_concurrency", config.concurrency),
    )
    await asyncio.gather(
        r.run_loop(config, traffic_limit or config.limit, traffic_interval),
        r.run_domain_state_loop(config, config.domain_state_limit, domain_state_interval),
    )


async def run_all_loop(
    config: Any,
    limit: int | None,
    interval_seconds: int,
    service_telemetry: Any | None = None,
) -> None:
    # Dispatch through the runner module so tests can patch runner.run_*_loop.
    import runner as r

    shared_interval = interval_seconds or 300
    assets_interval = max(60, shared_interval // 2)
    traffic_interval = shared_interval
    domain_state_interval = getattr(config, "domain_state_poll_interval_seconds", 1)
    r.log_info(
        "all_runner.loop.start",
        assets_interval_seconds=assets_interval,
        traffic_interval_seconds=traffic_interval,
        domain_state_interval_seconds=domain_state_interval,
        taxonomy_enabled=config.taxonomy_auto_enabled,
        taxonomy_interval_seconds=config.taxonomy_interval_seconds,
    )
    loops = [
        r.run_assets_loop(config, config.asset_limit, assets_interval),
        r.run_loop(config, limit or config.limit, traffic_interval),
        r.run_domain_state_loop(config, config.domain_state_limit, domain_state_interval),
    ]
    if config.taxonomy_auto_enabled:
        loops.append(r.run_taxonomy_loop(config, service_telemetry))
    await asyncio.gather(*loops)
