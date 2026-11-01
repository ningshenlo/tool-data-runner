from __future__ import annotations

import asyncio
import random
from typing import Any

from runner import (
    AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
    Config,
    D1Client,
    log_debug,
    log_error,
    log_info,
    log_task_result,
    run_with_telemetry,
)
from workloads.domain_state.store import D1DomainStateStore
from workloads.domain_state.types import DomainStateResult, DomainStateTask

async def process_domain_state(
    task: DomainStateTask,
    client: Any,
    store: D1DomainStateStore,
    max_retries: int,
) -> str:
    result = DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error="not_started")
    rdap_attempted = False
    rdap_status: str | None = None
    rdap_created_at: str | None = None
    rdap_error: str | None = None
    for attempt in range(max_retries + 1):
        log_debug(
            "domain_state.fetch_attempt.start",
            domain=task.normalized_domain,
            attempt=attempt + 1,
            max_attempts=max_retries + 1,
        )
        fetch_rdap = task.fetch_rdap and not rdap_attempted
        rdap_attempted = rdap_attempted or fetch_rdap
        try:
            current_result = await client.fetch(
                task.normalized_domain,
                fetch_domain_rating=task.fetch_domain_rating,
                fetch_rdap=fetch_rdap,
            )
            if current_result.rdap_status is not None:
                rdap_status = current_result.rdap_status
                rdap_created_at = current_result.domain_created_at
                rdap_error = current_result.rdap_error
            result = DomainStateResult(
                status=current_result.status,
                domain_rating=current_result.domain_rating,
                domain_created_at=rdap_created_at,
                error=current_result.error,
                rdap_status=rdap_status,
                rdap_error=rdap_error,
                retry_after_seconds=current_result.retry_after_seconds,
            )
        except Exception as error:
            if fetch_rdap:
                rdap_status = "failed"
                rdap_error = str(error)[:300]
            result = DomainStateResult(
                status="failed",
                domain_rating=None,
                domain_created_at=rdap_created_at,
                error=str(error)[:300],
                rdap_status=rdap_status,
                rdap_error=rdap_error,
            )
        log_task_result(
            "domain_state.fetch_attempt.done",
            result.status,
            domain=task.normalized_domain,
            attempt=attempt + 1,
        )
        if result.status != "failed":
            break
        if attempt < max_retries:
            await asyncio.sleep(
                max(
                    random.uniform(1.0, 3.0),
                    float(result.retry_after_seconds or 0),
                )
            )

    if not await store.renew_lease(task):
        return "stale"
    completed = await store.complete_task(task, result)
    return result.status if completed else "stale"

async def _run_domain_state_once(config: Config, d1: D1Client, limit: int | None = None, *, discover_due: bool = True) -> dict[str, int]:
    import runner as _runner

    effective_limit = limit or config.domain_state_limit
    concurrency = getattr(config, "domain_state_concurrency", config.concurrency)
    log_info("domain_state_runner.batch.start", limit=effective_limit, concurrency=concurrency)
    store = _runner.D1DomainStateStore(d1)
    requests_per_minute = getattr(
        config,
        "ahrefs_requests_per_minute",
        AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
    )
    client = _runner.DomainStateClient(
        config.ahref_api_key,
        requests_per_minute=requests_per_minute,
    )
    credential_requeued = (
        await store.requeue_missing_credential_tasks(effective_limit)
        if config.ahref_api_key
        else 0
    )
    log_info(
        "domain_state_runner.requeue_missing_credential_tasks.done",
        requeued=credential_requeued,
    )
    # Claim known due/retry work first. Only discover across the catalog when
    # this batch has room and the loop's idle discovery interval has elapsed.
    tasks = await store.claim_due_tasks(effective_limit, config.runner_instance_id)
    queued = 0
    discovery_ran = discover_due and len(tasks) < effective_limit
    if discovery_ran:
        queued = await store.queue_due_tasks(effective_limit - len(tasks), config.domain_state_max_age_days)
        if queued:
            tasks.extend(await store.claim_due_tasks(effective_limit - len(tasks), config.runner_instance_id))
    log_info("domain_state_runner.queue_due_tasks.done", queued=queued, discovery_ran=discovery_ran)
    log_info("domain_state_runner.claim_due_tasks.done", claimed=len(tasks))

    semaphore = asyncio.Semaphore(concurrency)
    counts = {
        "discovery_ran": int(discovery_ran),
        "credential_requeued": credential_requeued,
        "queued": queued,
        "claimed": len(tasks),
        "done": 0,
        "no_data": 0,
        "failed": 0,
        "stale": 0,
    }

    async def guarded(task: DomainStateTask) -> None:
        async with semaphore:
            try:
                status = await _runner.process_domain_state(task, client, store, config.max_retries)
            except Exception as error:
                status = "failed"
                log_error(
                    "domain_state.failed_with_exception",
                    domain=task.normalized_domain,
                    error=str(error)[:300],
                )
                try:
                    completed = await store.complete_task(
                        task,
                        DomainStateResult(
                            status="failed",
                            domain_rating=None,
                            domain_created_at=None,
                            error=str(error)[:300],
                        ),
                    )
                    if not completed:
                        status = "stale"
                except Exception as completion_error:
                    log_error(
                        "domain_state.complete_failed",
                        domain=task.normalized_domain,
                        error=str(completion_error)[:300],
                    )
            counts[status] = counts.get(status, 0) + 1
            log_task_result("domain_state.done", status, domain=task.normalized_domain)

    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))

    return counts

async def run_domain_state_once(config: Config, limit: int | None = None, *, discover_due: bool = True) -> dict[str, int]:
    async with D1Client(config) as d1:
        return await run_with_telemetry(
            config,
            d1,
            "domain_state",
            lambda: _run_domain_state_once(config, d1, limit, discover_due=discover_due),
        )
