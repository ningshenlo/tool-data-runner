from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from .cloudflare import (
    CloudflareD1Client,
    CloudflareD1MetadataStore,
    CloudflareR2ObjectStore,
)
from .config import MonitorLimits
from .engine import SitemapMonitor
from .fetch import SitemapHttpFetcher
from .models import ComparabilityResult, SiteScanResult
from .normalize import normalize_sitemap_url
from .scheduler import SchedulerPolicy, SitemapScheduler
from .storage import FileObjectStore, SqliteMetadataStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sitemap Change Intelligence Engine Phase 1 monitor"
    )
    parser.add_argument(
        "--site",
        action="append",
        default=[],
        help="homepage URL to monitor; repeat, or set SITEMAP_MONITOR_SITES",
    )
    parser.add_argument(
        "--site-file",
        action="append",
        default=[],
        help="UTF-8 file with one homepage URL per line; blank lines and # comments are ignored",
    )
    parser.add_argument(
        "--paused-site-file",
        action="append",
        default=[],
        help="UTF-8 file with sites to pause; blank lines and # comments are ignored",
    )
    parser.add_argument(
        "--sitemap",
        action="append",
        default=[],
        help="explicit sitemap URL; repeat as needed (applies to each --site)",
    )
    parser.add_argument(
        "--state-dir",
        default=".sitemap-monitor",
        help="local D1/R2-compatible state root (default: .sitemap-monitor)",
    )
    parser.add_argument(
        "--backend",
        choices=("cloudflare", "local"),
        default="local",
        help="metadata/object backend (default: local)",
    )
    parser.add_argument(
        "--require-enabled",
        action="store_true",
        help="exit safely unless SITEMAP_MONITOR_ENABLED=1 (for managed services)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="run one due-scheduler tick and exit (default)",
    )
    mode.add_argument("--loop", action="store_true", help="poll the due scheduler continuously")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=30,
        help="due-scheduler poll interval in seconds (minimum 5)",
    )
    parser.add_argument(
        "--check-interval-seconds",
        type=int,
        default=21_600,
        help="successful per-site check cadence in seconds (minimum 60)",
    )
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--job-lease-seconds", type=int, default=900)
    parser.add_argument("--maintenance-interval-seconds", type=int, default=21_600)
    parser.add_argument("--run-detail-retention-days", type=int, default=7)
    parser.add_argument("--scan-detail-retention-days", type=int, default=30)
    parser.add_argument("--job-retention-days", type=int, default=30)
    parser.add_argument("--maintenance-batch-size", type=int, default=500)
    parser.add_argument("--json", action="store_true", help="emit one JSON result per site")
    args = parser.parse_args(argv)
    if args.interval_seconds < 5:
        parser.error("--interval-seconds must be at least 5")
    if args.check_interval_seconds < 60:
        parser.error("--check-interval-seconds must be at least 60")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.max_attempts <= 0:
        parser.error("--max-attempts must be positive")
    if args.job_lease_seconds < 30:
        parser.error("--job-lease-seconds must be at least 30")
    if args.maintenance_interval_seconds < 300:
        parser.error("--maintenance-interval-seconds must be at least 300")
    if min(
        args.run_detail_retention_days,
        args.scan_detail_retention_days,
        args.job_retention_days,
        args.maintenance_batch_size,
    ) <= 0:
        parser.error("retention days and maintenance batch size must be positive")
    return args


def _print_result(
    result: SiteScanResult,
    json_output: bool,
    comparability: ComparabilityResult | None = None,
) -> None:
    payload = asdict(result)
    if comparability is not None:
        payload["comparability"] = asdict(comparability)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return
    outcomes = payload["outcomes"]
    print(
        f"{payload['homepage_url']}: resources={len(outcomes)} "
        f"changed={sum(item['result'] == 'changed' for item in outcomes)}"
        + (
            f" comparability={comparability.status}"
            if comparability is not None
            else ""
        )
    )
    for item in outcomes:
        counts = (
            f"+{item['added_count']} -{item['removed_count']} "
            f"~{item['modified_count']}"
        )
        suffix = f" error={item['error_code']}" if item["error_code"] else ""
        print(f"  {item['result']:20} {counts} {item['url']}{suffix}")


async def run(args: argparse.Namespace) -> None:
    load_dotenv()
    if args.require_enabled and os.getenv("SITEMAP_MONITOR_ENABLED", "0").strip() != "1":
        print("sitemap-monitor disabled: set SITEMAP_MONITOR_ENABLED=1 to run")
        if args.loop:
            while True:
                await asyncio.sleep(args.interval_seconds)
        return
    configured_sites = [
        value.strip()
        for value in os.getenv("SITEMAP_MONITOR_SITES", "").split(",")
        if value.strip()
    ]
    configured_site_file = os.getenv("SITEMAP_MONITOR_SITE_FILE", "").strip()
    site_files = [*args.site_file, *([configured_site_file] if configured_site_file else [])]
    file_sites: list[str] = []
    for site_file in site_files:
        path = Path(site_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"Sitemap site file does not exist: {path}")
        file_sites.extend(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    sites = [*args.site, *file_sites, *configured_sites]
    sites = list(dict.fromkeys(sites))
    if not sites:
        raise SystemExit("At least one --site or SITEMAP_MONITOR_SITES value is required.")

    configured_paused_site_file = os.getenv(
        "SITEMAP_MONITOR_PAUSED_SITE_FILE", ""
    ).strip()
    paused_site_files = [
        *args.paused_site_file,
        *([configured_paused_site_file] if configured_paused_site_file else []),
    ]
    paused_sites: list[str] = []
    for site_file in paused_site_files:
        path = Path(site_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"Paused sitemap site file does not exist: {path}")
        paused_sites.extend(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    paused_sites = list(dict.fromkeys(paused_sites))
    normalized_active_sites = {normalize_sitemap_url(site) for site in sites}
    normalized_paused_sites = {normalize_sitemap_url(site) for site in paused_sites}
    overlap = normalized_active_sites.intersection(normalized_paused_sites)
    if overlap:
        raise SystemExit(
            "Sitemap sites cannot be both active and paused: "
            + ", ".join(sorted(overlap))
        )

    cloudflare_metadata: CloudflareD1MetadataStore | None = None
    cloudflare_objects: CloudflareR2ObjectStore | None = None
    local_metadata: SqliteMetadataStore | None = None
    if args.backend == "cloudflare":
        cloudflare_objects = CloudflareR2ObjectStore(
            access_key_id=os.environ["FOR_ALL_APP_R2_ACCESS_KEY_ID"],
            account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
            bucket=(
                os.getenv("SITEMAP_MONITOR_R2_BUCKET")
                or os.environ["CLOUDFLARE_R2_BUCKET"]
            ),
            secret_access_key=os.environ["FOR_ALL_APP_R2_SECRET_ACCESS_KEY"],
        )
        d1_client = CloudflareD1Client(
            account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
            api_token=os.environ["CLOUDFLARE_API_TOKEN"],
            database_id=os.environ["CLOUDFLARE_D1_DATABASE_ID"],
        )
        cloudflare_metadata = CloudflareD1MetadataStore(d1_client)
        metadata = cloudflare_metadata
        objects = cloudflare_objects
    else:
        state_root = Path(args.state_dir).resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        local_metadata = SqliteMetadataStore(state_root / "metadata.sqlite3")
        metadata = local_metadata
        objects = FileObjectStore(state_root / "objects")
    limits = MonitorLimits()
    allow_synthetic_dns_doh = (
        os.getenv("SITEMAP_MONITOR_SYNTHETIC_DNS_DOH_FALLBACK", "0").strip() == "1"
    )
    fetcher = SitemapHttpFetcher(
        limits,
        allow_synthetic_dns_doh=allow_synthetic_dns_doh,
    )
    monitor = SitemapMonitor(metadata, objects, limits=limits, fetcher=fetcher)
    lease_owner = (
        f"{os.getenv('RUNNER_INSTANCE_ID', 'sitemap-monitor')}"
        f":{os.getpid()}:{uuid.uuid4().hex[:8]}"
    )
    scheduler = SitemapScheduler(
        metadata,
        monitor,
        batch_size=args.batch_size,
        explicit_sitemaps=args.sitemap,
        lease_owner=lease_owner,
        policy=SchedulerPolicy(
            check_interval_sec=args.check_interval_seconds,
            job_retention_sec=args.job_retention_days * 86_400,
            job_lease_sec=args.job_lease_seconds,
            maintenance_batch_size=args.maintenance_batch_size,
            maintenance_interval_sec=args.maintenance_interval_seconds,
            max_attempts=args.max_attempts,
            run_detail_retention_sec=args.run_detail_retention_days * 86_400,
            scan_detail_retention_sec=args.scan_detail_retention_days * 86_400,
        ),
    )
    try:
        await scheduler.pause_sites(paused_sites)
        while True:
            tick = await scheduler.run_once(sites)
            if tick.maintenance.changed and not args.json:
                print(
                    "sitemap maintenance: "
                    f"expired_jobs={tick.maintenance.expired_jobs} "
                    f"pruned_jobs={tick.maintenance.pruned_jobs} "
                    f"pruned_runs={tick.maintenance.pruned_runs} "
                    f"pruned_scans={tick.maintenance.pruned_scans}"
                )
            for execution in tick.executions:
                if execution.result is not None:
                    _print_result(
                        execution.result,
                        args.json,
                        execution.comparability,
                    )
                elif args.json:
                    print(
                        json.dumps(
                            {
                                "error": execution.error,
                                "job_id": execution.job_id,
                                "status": execution.status,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                else:
                    print(
                        f"{execution.job_id}: {execution.status}"
                        f" error={execution.error or 'unknown'}"
                    )
            if not tick.executions and not args.json:
                print(
                    "sitemap scheduler idle: "
                    f"due={tick.due_sites} claimed={tick.jobs_claimed}"
                )
            if not args.loop:
                return
            await asyncio.sleep(args.interval_seconds)
    finally:
        await monitor.close()
        if cloudflare_metadata is not None and cloudflare_objects is not None:
            try:
                await cloudflare_metadata.close()
            finally:
                await cloudflare_objects.close()
        elif local_metadata is not None:
            local_metadata.close()


def main() -> None:
    asyncio.run(run(parse_args()))
