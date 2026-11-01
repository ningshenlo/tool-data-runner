from __future__ import annotations

import asyncio
import random
from typing import Any

from runner import (
    TRAFFIC_SOURCE,
    Config,
    D1Client,
    log_debug,
    log_error,
    log_info,
    log_task_result,
)
from workloads.traffic.store import D1TaskStore
from workloads.traffic.types import FetchResult, TrafficTask

async def process_task(
    task: TrafficTask,
    similarweb: Any,
    d1: D1Client,
    store: D1TaskStore,
    max_retries: int,
) -> str:
    result = FetchResult(status="failed", monthly_rows=[], error="not_started")
    for attempt in range(max_retries + 1):
        log_debug(
            "traffic_task.fetch_attempt.start",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            attempt=attempt + 1,
            max_attempts=max_retries + 1,
        )
        result = await similarweb.fetch(task.normalized_domain, task.traffic_month)
        log_task_result(
            "traffic_task.fetch_attempt.done",
            result.status,
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            attempt=attempt + 1,
        )
        if result.status != "failed":
            break
        if attempt < max_retries:
            await asyncio.sleep(random.uniform(1.0, 3.0))

    if not await store.renew_lease(task):
        return "stale"
    await d1.insert_result(task, result)
    completed = await store.complete_task(task, result)
    return result.status if completed else "stale"

async def _run_once(config: Config, d1: D1Client, limit: int | None = None) -> dict[str, int]:
    # Resolve patchable collaborators through runner so unit tests can patch runner.*.
    import runner as _runner

    effective_limit = limit or config.limit
    concurrency = getattr(config, "traffic_concurrency", config.concurrency)
    log_info("traffic_runner.batch.start", limit=effective_limit, concurrency=concurrency)
    store = D1TaskStore(d1)
    traffic_month = _runner.previous_traffic_month()
    if not _runner.traffic_release_probe_window_open(config):
        log_info(
            "traffic_release_gate.not_scheduled",
            traffic_month=traffic_month,
            probe_start_day=getattr(config, "traffic_release_probe_start_day", None),
        )
        return {
            "traffic_queued": 0,
            "claimed": 0,
            "done": 0,
            "no_data": 0,
            "forbidden": 0,
            "failed": 0,
            "stale": 0,
            "release_available": 0,
            "release_probe_attempted": 0,
            "release_not_scheduled": 1,
            "release_waiting": 0,
        }

    similarweb = _runner.SimilarWebClient(config)
    release = await _runner.D1TrafficReleaseStore(d1).check_or_probe(
        traffic_month,
        config.traffic_release_probe_domain,
        config.traffic_release_probe_interval_seconds,
        similarweb,
    )
    if not release.available:
        log_info(
            "traffic_release_gate.blocked",
            traffic_month=traffic_month,
            status=release.status,
            observed_latest_month=release.observed_latest_month,
        )
        return {
            "traffic_queued": 0,
            "claimed": 0,
            "done": 0,
            "no_data": 0,
            "forbidden": 0,
            "failed": 0,
            "stale": 0,
            "release_available": 0,
            "release_probe_attempted": 1 if release.probe_attempted else 0,
            "release_not_scheduled": 0,
            "release_waiting": 0 if release.probe_attempted else 1,
        }

    queued = await store.queue_missing_traffic_tasks(config.traffic_release_queue_limit, traffic_month)
    log_info("traffic_runner.queue_missing_traffic_tasks.done", queued=queued, traffic_month=traffic_month)
    tasks = await store.claim_due_tasks(effective_limit, config.runner_instance_id)
    log_info("traffic_runner.claim_due_tasks.done", claimed=len(tasks))

    semaphore = asyncio.Semaphore(concurrency)
    counts = {
        "traffic_queued": queued,
        "claimed": len(tasks),
        "done": 0,
        "no_data": 0,
        "forbidden": 0,
        "failed": 0,
        "stale": 0,
        "release_available": 1,
        "release_probe_attempted": 1 if release.probe_attempted else 0,
        "release_not_scheduled": 0,
        "release_waiting": 0,
    }

    async def guarded(task: TrafficTask) -> None:
        async with semaphore:
            try:
                log_debug("traffic_task.start", domain=task.normalized_domain, traffic_month=task.traffic_month)
                status = await process_task(task, similarweb, d1, store, config.max_retries)
            except Exception as error:
                status = "failed"
                error_message = str(error)[:2000]
                log_error(
                    "traffic_task.failed_with_exception",
                    domain=task.normalized_domain,
                    traffic_month=task.traffic_month,
                    error_type=type(error).__name__,
                    error=error_message,
                )
                if not await store.complete_task(
                    task,
                    FetchResult(status="failed", monthly_rows=[], error=error_message),
                ):
                    status = "stale"
            counts[status] = counts.get(status, 0) + 1
            log_task_result(
                "traffic_task.done",
                status,
                domain=task.normalized_domain,
                traffic_month=task.traffic_month,
            )

    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))

    return counts

async def run_once(config: Config, limit: int | None = None) -> dict[str, int]:
    # Collaborators resolved through runner so tests can patch runner.D1Client / hooks.
    import runner as _runner

    async with _runner.D1Client(config) as d1:
        counts = await _runner.run_with_telemetry(
            config,
            d1,
            "traffic",
            lambda: _run_once(config, d1, limit),
        )
        # Publication is independent of report exports and also runs on idle batches.
        try:
            publication = await _runner.refresh_completed_market_snapshot(
                d1,
                source=TRAFFIC_SOURCE,
                visible_tools_ctes=_runner.MARKET_VISIBLE_TOOLS_CTES,
                build=_runner.build_market_snapshot_from_d1,
                activate=_runner.activate_market_snapshot_from_d1,
            )
            for status in ("active", "waiting", "blocked"):
                counts["market_snapshot_" + status] = int(publication["status"] == status)
            if publication["status"] not in ("disabled", "throttled", "locked"):
                _runner.log_info("market_snapshot.auto_publish", **publication)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            counts["market_snapshot_blocked"] = 1
            _runner.log_error("market_snapshot.auto_publish.failed", error_type=type(error).__name__)
        # Catch up keyword projections even when the market month was published earlier.
        try:
            demand = await _runner.refresh_completed_search_demand(d1, source=TRAFFIC_SOURCE)
            for status in ("active", "blocked"):
                counts["search_demand_" + status] = int(demand["status"] == status)
            if demand["status"] not in ("disabled", "throttled", "locked"):
                _runner.log_info("search_demand.auto_publish", **demand)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            counts["search_demand_blocked"] = 1
            _runner.log_error("search_demand.auto_publish.failed", error_type=type(error).__name__)
        # Runs after collection, including idle batches. Rendering is independent;
        # export failures never undo or misreport the traffic collection batch.
        try:
            counts.update(await _runner.export_ready_reports(d1, _runner.previous_traffic_month()))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            counts["report_exports_blocked"] = 1
            _runner.log_error("report_export.failed", error_type=type(error).__name__)
        return counts
