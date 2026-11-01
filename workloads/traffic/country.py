from __future__ import annotations

from typing import Any

from config.constants import (
    DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL,
    DOMAIN_TRAFFIC_COUNTRY_REFRESH_ESTIMATES_SQL,
    DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL,
    TRAFFIC_SOURCE,
)
from libs.numbers import estimate_visits_from_bps, normalize_country_code, to_integer
from workloads.traffic.parse import traffic_share_to_bps

def build_domain_traffic_country_upsert_statements(
    domain: str,
    rows: list[dict[str, Any]],
    *,
    captured_at: str,
) -> list[tuple[str, list[Any]]]:
    statements: list[tuple[str, list[Any]]] = []
    for row in rows:
        traffic_month = str(row.get("traffic_month") or "").strip()
        if not traffic_month:
            continue

        observed_countries: list[tuple[int, str, int]] = []
        seen_country_codes: set[str] = set()
        for country_position in range(1, 6):
            country_code = normalize_country_code(row.get(f"top_country_{country_position}"))
            traffic_share_bps = traffic_share_to_bps(
                row.get(f"top_country_{country_position}_traffic_share")
            )
            if country_code is None or traffic_share_bps is None or country_code in seen_country_codes:
                continue
            seen_country_codes.add(country_code)
            observed_countries.append((country_position, country_code, traffic_share_bps))

        if not observed_countries:
            visits = to_integer(row.get("visits"))
            if visits is not None:
                # Compact historical rows carry visits but no TopCountryShares.
                # Preserve the last observed shares and keep their estimate in sync.
                statements.append(
                    (
                        DOMAIN_TRAFFIC_COUNTRY_REFRESH_ESTIMATES_SQL,
                        [visits, captured_at, domain, TRAFFIC_SOURCE, traffic_month],
                    )
                )
            continue

        # Reconcile a newly observed Top 5 as a set. This removes countries that
        # fell out of a later complete provider response.
        statements.append(
            (
                DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL,
                [domain, TRAFFIC_SOURCE, traffic_month],
            )
        )
        source_snapshot_id = to_integer(row.get("_source_snapshot_id"))
        for country_position, country_code, traffic_share_bps in observed_countries:
            statements.append(
                (
                    DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL,
                    [
                        domain,
                        TRAFFIC_SOURCE,
                        traffic_month,
                        country_code,
                        country_position,
                        traffic_share_bps,
                        estimate_visits_from_bps(row.get("visits"), traffic_share_bps),
                        source_snapshot_id,
                        captured_at,
                    ],
                )
            )
    return statements

def is_missing_market_country_schema_error(error: Exception) -> bool:
    message = str(error).lower()
    return "domain_traffic_country_monthly" in message and (
        "no such table" in message or "table does not exist" in message
    )
