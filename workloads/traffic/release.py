from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from runner import (
    TRAFFIC_SOURCE,
    Config,
    D1Client,
    iso_delta,
    log_info,
    utc_now_iso,
)
from workloads.traffic.client import SimilarWebClient
from workloads.traffic.parse import requested_month_has_traffic_data
from workloads.traffic.types import FetchResult, TrafficReleaseGateResult

def traffic_release_probe_window_open(config: Config, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    return current.day >= config.traffic_release_probe_start_day

class D1TrafficReleaseStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def check_or_probe(
        self,
        traffic_month: str,
        probe_domain: str,
        probe_interval_seconds: int,
        client: SimilarWebClient,
    ) -> TrafficReleaseGateResult:
        now = utc_now_iso()
        rows = await self.d1.query(
            """
            SELECT status, observed_latest_month, next_check_at
            FROM traffic_month_release_checks
            WHERE source = ? AND traffic_month = ?
            LIMIT 1
            """,
            [TRAFFIC_SOURCE, traffic_month],
        )
        existing = rows[0] if rows else None
        if existing and existing.get("status") == "available":
            return TrafficReleaseGateResult(
                available=True,
                status="available",
                probe_attempted=False,
                observed_latest_month=existing.get("observed_latest_month"),
            )

        next_check_at = str(existing.get("next_check_at") or "") if existing else ""
        if next_check_at and next_check_at > now:
            return TrafficReleaseGateResult(
                available=False,
                status=str(existing.get("status") or "unavailable"),
                probe_attempted=False,
                observed_latest_month=existing.get("observed_latest_month"),
            )

        result = await client.fetch(probe_domain, traffic_month)
        available = result.status == "done" and requested_month_has_traffic_data(result.monthly_rows, traffic_month)
        status = "available" if available else ("unavailable" if result.status == "no_data" else "error")
        next_check = None if available else iso_delta(seconds=probe_interval_seconds)
        error = None if available else (result.error or f"probe_status:{result.status}")
        response_meta = json.dumps(
            {
                "probe_status": result.status,
                "monthly_rows": len(result.monthly_rows),
                "requested_month_present": requested_month_has_traffic_data(result.monthly_rows, traffic_month),
            },
            sort_keys=True,
        )
        await self.d1.run(
            """
            INSERT INTO traffic_month_release_checks (
              source, traffic_month, status, probe_domain, observed_latest_month,
              attempts, last_checked_at, next_check_at, available_at, last_error,
              response_meta_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, traffic_month) DO UPDATE SET
              status = excluded.status,
              probe_domain = excluded.probe_domain,
              observed_latest_month = excluded.observed_latest_month,
              attempts = traffic_month_release_checks.attempts + 1,
              last_checked_at = excluded.last_checked_at,
              next_check_at = excluded.next_check_at,
              available_at = CASE
                WHEN excluded.status = 'available' THEN coalesce(traffic_month_release_checks.available_at, excluded.available_at)
                ELSE traffic_month_release_checks.available_at
              END,
              last_error = excluded.last_error,
              response_meta_json = excluded.response_meta_json,
              updated_at = excluded.updated_at
            """,
            [
                TRAFFIC_SOURCE,
                traffic_month,
                status,
                probe_domain,
                result.observed_latest_month,
                now,
                next_check,
                now if available else None,
                error[:2000] if error else None,
                response_meta,
                now,
            ],
        )
        log_info(
            "traffic_release_gate.probe",
            traffic_month=traffic_month,
            probe_domain=probe_domain,
            status=status,
            observed_latest_month=result.observed_latest_month,
            next_check_at=next_check,
        )
        return TrafficReleaseGateResult(
            available=available,
            status=status,
            probe_attempted=True,
            observed_latest_month=result.observed_latest_month,
        )
