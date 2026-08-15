from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .comparability import (
    ComparabilityPolicy,
    assess_comparability,
    build_site_scan_snapshot,
)
from .engine import SitemapMonitor
from .models import (
    ComparabilityResult,
    MaintenanceResult,
    SiteScanResult,
    SitemapJob,
    SiteScanSnapshot,
)
from .normalize import normalize_sitemap_url
from .storage import SchedulerStore, site_id_for, stable_id


def _now_ms() -> int:
    return int(time.time() * 1_000)


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    check_interval_sec: int = 21_600
    dispatch_lease_sec: int = 300
    job_lease_sec: int = 900
    max_attempts: int = 5
    retry_base_sec: int = 60
    retry_max_sec: int = 3_600
    failure_cooldown_initial_sec: int = 86_400
    failure_cooldown_extended_sec: int = 259_200
    failure_cooldown_max_sec: int = 604_800
    maintenance_interval_sec: int = 21_600
    run_detail_retention_sec: int = 604_800
    scan_detail_retention_sec: int = 2_592_000
    job_retention_sec: int = 2_592_000
    maintenance_batch_size: int = 500
    registration_refresh_sec: int = 3_600
    jitter_ratio: float = 0.10

    def __post_init__(self) -> None:
        integer_values = (
            self.check_interval_sec,
            self.dispatch_lease_sec,
            self.job_lease_sec,
            self.max_attempts,
            self.retry_base_sec,
            self.retry_max_sec,
            self.failure_cooldown_initial_sec,
            self.failure_cooldown_extended_sec,
            self.failure_cooldown_max_sec,
            self.maintenance_interval_sec,
            self.run_detail_retention_sec,
            self.scan_detail_retention_sec,
            self.job_retention_sec,
            self.maintenance_batch_size,
            self.registration_refresh_sec,
        )
        if any(value <= 0 for value in integer_values):
            raise ValueError("Scheduler intervals, leases, and attempts must be positive.")
        if self.retry_base_sec > self.retry_max_sec:
            raise ValueError("retry_base_sec cannot exceed retry_max_sec.")
        if not (
            self.failure_cooldown_initial_sec
            <= self.failure_cooldown_extended_sec
            <= self.failure_cooldown_max_sec
        ):
            raise ValueError("Failure cooldowns must be monotonically increasing.")
        if not 0 <= self.jitter_ratio <= 0.5:
            raise ValueError("jitter_ratio must be between 0 and 0.5.")


@dataclass(frozen=True, slots=True)
class JobExecution:
    completion_applied: bool
    comparability: ComparabilityResult | None
    error: str | None
    job_id: str
    result: SiteScanResult | None
    status: str


@dataclass(frozen=True, slots=True)
class SchedulerTick:
    due_sites: int
    executions: tuple[JobExecution, ...]
    jobs_created: int
    jobs_claimed: int
    maintenance: MaintenanceResult


class SitemapScheduler:
    """D1-backed due-site scheduler and idempotent durable job consumer.

    The job table is the system of record. A future Cloudflare Queue message only
    needs to carry ``job_id``; duplicate delivery remains harmless because the
    consumer must first acquire the same fenced database lease.
    """

    def __init__(
        self,
        store: SchedulerStore,
        monitor: SitemapMonitor,
        *,
        batch_size: int = 25,
        clock_ms: Callable[[], int] = _now_ms,
        explicit_sitemaps: Iterable[str] = (),
        lease_owner: str = "sitemap-monitor",
        policy: SchedulerPolicy | None = None,
        comparability_policy: ComparabilityPolicy | None = None,
    ):
        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        if not lease_owner.strip():
            raise ValueError("lease_owner is required.")
        self.batch_size = batch_size
        self.clock_ms = clock_ms
        self.explicit_sitemaps = tuple(explicit_sitemaps)
        self.lease_owner = lease_owner
        self.monitor = monitor
        self.policy = policy or SchedulerPolicy()
        self.comparability_policy = comparability_policy or ComparabilityPolicy()
        self.store = store
        self._last_maintenance_at_ms: int | None = None
        self._registered_sites: dict[str, tuple[str, int, int]] = {}

    def _jittered_delay_ms(self, key: str, seconds: int) -> int:
        base_ms = seconds * 1_000
        window = int(base_ms * self.policy.jitter_ratio)
        if window == 0:
            return base_ms
        sample = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
        offset = (sample % (2 * window + 1)) - window
        return max(1_000, base_ms + offset)

    def _retry_delay_ms(self, job: SitemapJob) -> int:
        exponent = min(30, max(0, job.attempts - 1))
        seconds = min(
            self.policy.retry_base_sec * (2**exponent),
            self.policy.retry_max_sec,
        )
        return self._jittered_delay_ms(f"retry:{job.job_id}:{job.attempts}", seconds)

    def _success_next_check_ms(self, job: SitemapJob, finished_at_ms: int) -> int:
        interval = self._jittered_delay_ms(
            f"success:{job.job_id}",
            self._site_interval(job),
        )
        return finished_at_ms + interval

    def _terminal_failure_next_check_ms(
        self,
        job: SitemapJob,
        finished_at_ms: int,
    ) -> int:
        next_error_streak = job.base_error_streak + 1
        if next_error_streak <= 1:
            seconds = self.policy.failure_cooldown_initial_sec
        elif next_error_streak == 2:
            seconds = self.policy.failure_cooldown_extended_sec
        else:
            seconds = self.policy.failure_cooldown_max_sec
        return finished_at_ms + self._jittered_delay_ms(
            f"dead:{job.job_id}",
            seconds,
        )

    def _site_interval(self, job: SitemapJob) -> int:
        return job.check_interval_sec

    async def register_sites(self, sites: Iterable[str]) -> tuple[str, ...]:
        registered: list[str] = []
        now_ms = self.clock_ms()
        for homepage_url in dict.fromkeys(sites):
            normalized = normalize_sitemap_url(
                homepage_url,
                max_length=self.monitor.limits.max_url_length,
            )
            site_id = site_id_for(normalized)
            previous = self._registered_sites.get(site_id)
            if (
                previous is not None
                and previous[:2] == (normalized, self.policy.check_interval_sec)
                and now_ms - previous[2]
                    < self.policy.registration_refresh_sec * 1_000
            ):
                registered.append(site_id)
                continue
            await self.store.ensure_site(
                site_id,
                normalized,
                now_ms,
                check_interval_sec=self.policy.check_interval_sec,
            )
            self._registered_sites[site_id] = (
                normalized,
                self.policy.check_interval_sec,
                now_ms,
            )
            registered.append(site_id)
        return tuple(registered)

    async def perform_maintenance(self) -> MaintenanceResult:
        now_ms = self.clock_ms()
        if (
            self._last_maintenance_at_ms is not None
            and now_ms - self._last_maintenance_at_ms
            < self.policy.maintenance_interval_sec * 1_000
        ):
            return MaintenanceResult()
        result = await self.store.perform_maintenance(
            job_cutoff_ms=max(
                0, now_ms - self.policy.job_retention_sec * 1_000
            ),
            limit=self.policy.maintenance_batch_size,
            now_ms=now_ms,
            run_cutoff_ms=max(
                0, now_ms - self.policy.run_detail_retention_sec * 1_000
            ),
            scan_cutoff_ms=max(
                0, now_ms - self.policy.scan_detail_retention_sec * 1_000
            ),
        )
        self._last_maintenance_at_ms = now_ms
        return result

    async def enqueue_due(self) -> tuple[int, int]:
        now_ms = self.clock_ms()
        due_sites = await self.store.claim_due_sites(
            lease_duration_ms=self.policy.dispatch_lease_sec * 1_000,
            lease_owner=self.lease_owner,
            limit=self.batch_size,
            now_ms=now_ms,
        )
        created = 0
        for due_site in due_sites:
            job = await self.store.ensure_job(
                due_site,
                max_attempts=self.policy.max_attempts,
                now_ms=now_ms,
            )
            created += int(job is not None and job.attempts == 0 and job.status == "pending")
        return len(due_sites), created

    async def _lease_heartbeat(self, job: SitemapJob) -> bool:
        interval_seconds = max(1.0, self.policy.job_lease_sec / 3)
        while True:
            await asyncio.sleep(interval_seconds)
            renewed = await self.store.renew_job_lease(
                job,
                lease_duration_ms=self.policy.job_lease_sec * 1_000,
                lease_owner=self.lease_owner,
                now_ms=self.clock_ms(),
            )
            if not renewed:
                return False

    async def _scan_with_heartbeat(self, job: SitemapJob) -> SiteScanResult:
        namespace = stable_id("smattempt", job.job_id, str(job.attempts))
        scan = asyncio.create_task(
            self.monitor.scan_site(
                job.homepage_url,
                explicit_sitemaps=self.explicit_sitemaps,
                run_namespace=namespace,
            )
        )
        heartbeat = asyncio.create_task(self._lease_heartbeat(job))
        done, _ = await asyncio.wait({scan, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done and heartbeat.result() is False:
            scan.cancel()
            await asyncio.gather(scan, return_exceptions=True)
            raise RuntimeError("job_lease_lost")
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        return await scan

    @staticmethod
    def _scan_error(result: SiteScanResult) -> str:
        codes = sorted(
            {
                outcome.error_code or "unknown"
                for outcome in result.outcomes
                if outcome.result == "failed"
            }
        )
        if not codes:
            return "no_usable_sitemap"
        return "no_usable_sitemap:" + ",".join(codes)

    @staticmethod
    def _is_transient_error_code(code: str) -> bool:
        if code in {"dns_error", "request_error", "timeout"}:
            return True
        if not code.startswith("http_"):
            return False
        try:
            status = int(code.removeprefix("http_"))
        except ValueError:
            return False
        return status in {408, 425, 429} or 500 <= status <= 599

    @classmethod
    def _scan_is_retryable(cls, result: SiteScanResult | None) -> bool:
        if result is None:
            return True
        error_codes = {
            outcome.error_code
            for outcome in result.outcomes
            if outcome.result == "failed" and outcome.error_code
        }
        return any(cls._is_transient_error_code(code) for code in error_codes)

    async def _execute(self, job: SitemapJob) -> JobExecution:
        result: SiteScanResult | None = None
        site_scan: SiteScanSnapshot | None = None
        comparability: ComparabilityResult | None = None
        error: str | None = None
        try:
            result = await self._scan_with_heartbeat(job)
            site_scan = build_site_scan_snapshot(result)
            baseline, previous = await self.store.get_site_scan_context(job.site_id)
            comparability = assess_comparability(
                site_scan,
                baseline=baseline,
                previous=previous,
                policy=self.comparability_policy,
            )
            if not result.successful:
                error = self._scan_error(result)
        except Exception as exc:  # The job ledger owns bounded retries.
            error = f"{type(exc).__name__}:{exc}"[:2_000]

        finished_at_ms = self.clock_ms()
        if error is None and result is not None:
            applied = await self.store.finish_job_success(
                job,
                finished_at_ms=finished_at_ms,
                next_check_at_ms=self._success_next_check_ms(job, finished_at_ms),
                comparability=comparability,
                site_scan=site_scan,
            )
            return JobExecution(
                completion_applied=applied,
                comparability=comparability,
                error=None,
                job_id=job.job_id,
                result=result,
                status="succeeded" if applied else "stale",
            )

        dead = (
            job.attempts >= job.max_attempts
            or not self._scan_is_retryable(result)
        )
        available_at_ms = finished_at_ms + self._retry_delay_ms(job)
        applied = await self.store.finish_job_failure(
            job,
            available_at_ms=available_at_ms,
            dead=dead,
            error=error or "unknown_error",
            finished_at_ms=finished_at_ms,
            next_check_at_ms=(
                self._terminal_failure_next_check_ms(job, finished_at_ms)
                if dead
                else None
            ),
            comparability=comparability,
            site_scan=site_scan,
        )
        return JobExecution(
            completion_applied=applied,
            comparability=comparability,
            error=error,
            job_id=job.job_id,
            result=result,
            status=("dead" if dead else "retry") if applied else "stale",
        )

    async def process_available(self) -> tuple[int, tuple[JobExecution, ...]]:
        jobs = await self.store.claim_jobs(
            lease_duration_ms=self.policy.job_lease_sec * 1_000,
            lease_owner=self.lease_owner,
            limit=self.batch_size,
            now_ms=self.clock_ms(),
        )
        executions = []
        for job in jobs:
            executions.append(await self._execute(job))
        return len(jobs), tuple(executions)

    async def run_once(self, sites: Iterable[str]) -> SchedulerTick:
        await self.register_sites(sites)
        maintenance = await self.perform_maintenance()
        due_sites, jobs_created = await self.enqueue_due()
        jobs_claimed, executions = await self.process_available()
        return SchedulerTick(
            due_sites=due_sites,
            executions=executions,
            jobs_created=jobs_created,
            jobs_claimed=jobs_claimed,
            maintenance=maintenance,
        )
