from __future__ import annotations

import json
from typing import Any

from config.constants import (
    DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL,
    DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL,
    TRAFFIC_BACKFILL_MAX_SNAPSHOT_STATEMENTS,
    TRAFFIC_SOURCE,
)
from libs.domains import normalize_domain
from libs.logging_util import log_error, log_info
from libs.numbers import normalize_country_code, to_integer, to_month_start, to_number
from libs.timeutil import utc_now_iso
from runner import Config, D1Client
from workloads.traffic.parse import parse_monthly_rows
from workloads.traffic.parse import (
    build_domain_traffic_monthly_upsert,
    traffic_share_to_bps,
)
from workloads.traffic.country import build_domain_traffic_country_upsert_statements

def add_legacy_snapshot_countries(
    rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    requested_month: str,
) -> list[dict[str, Any]]:
    legacy_country_fields: dict[str, Any] = {}
    for country_position in range(1, 6):
        country_code = normalize_country_code(snapshot.get(f"top_country_{country_position}"))
        country_share = to_number(snapshot.get(f"top_country_{country_position}_traffic_share"))
        if country_code is None or country_share is None or not 0 <= country_share <= 1:
            continue
        legacy_country_fields[f"top_country_{country_position}"] = country_code
        legacy_country_fields[f"top_country_{country_position}_traffic_share"] = country_share
    if not legacy_country_fields:
        return rows

    if not rows:
        return [
            {
                "traffic_month": requested_month,
                "visits": to_integer(snapshot.get("visits")),
                **legacy_country_fields,
            }
        ]

    target_row = next(
        (row for row in rows if row.get("traffic_month") == requested_month),
        rows[-1],
    )
    if target_row.get("visits") is None:
        target_row["visits"] = to_integer(snapshot.get("visits"))
    has_parsed_countries = any(
        normalize_country_code(target_row.get(f"top_country_{country_position}")) is not None
        and traffic_share_to_bps(
            target_row.get(f"top_country_{country_position}_traffic_share")
        ) is not None
        for country_position in range(1, 6)
    )
    if not has_parsed_countries:
        target_row.update(legacy_country_fields)
    return rows


async def backfill_domain_traffic_monthly_from_d1(
    d1: D1Client,
    limit: int | None = None,
    *,
    after_snapshot_id: int = 0,
    page_size: int = 100,
    write_batch_size: int = 40,
) -> dict[str, int]:
    """Rebuild the domain projection from append-only Similarweb snapshots.

    Snapshots are replayed from oldest to newest. The projection upsert is
    idempotent, so this command is safe to resume or run again after a parser
    change.
    """
    if after_snapshot_id < 0:
        raise ValueError("after_snapshot_id must be non-negative")
    if page_size <= 0 or write_batch_size <= 0:
        raise ValueError("page_size and write_batch_size must be positive")

    counts: dict[str, int] = {
        "snapshots_scanned": 0,
        "snapshots_projected": 0,
        "snapshots_invalid": 0,
        "monthly_rows_upserted": 0,
        "snapshots_with_country": 0,
        "country_rows_upserted": 0,
        "country_months_reconciled": 0,
        "country_estimates_refreshed": 0,
        "country_statements_applied": 0,
        "pages_completed": 0,
        "last_snapshot_id": after_snapshot_id,
    }
    last_snapshot_id = after_snapshot_id

    while limit is None or counts["snapshots_scanned"] < limit:
        remaining = (
            page_size
            if limit is None
            else min(page_size, limit - counts["snapshots_scanned"])
        )
        if remaining <= 0:
            break
        snapshots = await d1.query(
            """
            SELECT
              id, normalized_domain, traffic_month, fetched_at, visits, raw_payload,
              top_country_1, top_country_1_traffic_share,
              top_country_2, top_country_2_traffic_share,
              top_country_3, top_country_3_traffic_share,
              top_country_4, top_country_4_traffic_share,
              top_country_5, top_country_5_traffic_share
            FROM domain_traffic_snapshots
            WHERE source = ?
              AND status = 'done'
              AND id > ?
              AND (
                (json_valid(raw_payload) AND raw_payload <> '{}')
                OR top_country_1 IS NOT NULL
                OR top_country_2 IS NOT NULL
                OR top_country_3 IS NOT NULL
                OR top_country_4 IS NOT NULL
                OR top_country_5 IS NOT NULL
              )
            ORDER BY id ASC
            LIMIT ?
            """,
            [TRAFFIC_SOURCE, last_snapshot_id, remaining],
        )
        if not snapshots:
            break

        snapshot_statement_groups: list[list[tuple[str, str, list[Any]]]] = []
        page_last_snapshot_id = last_snapshot_id
        for snapshot in snapshots:
            snapshot_id = to_integer(snapshot.get("id")) or 0
            page_last_snapshot_id = max(page_last_snapshot_id, snapshot_id)
            counts["snapshots_scanned"] += 1
            raw_payload = snapshot.get("raw_payload")
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            except (TypeError, json.JSONDecodeError):
                payload = None

            domain = normalize_domain(str(snapshot.get("normalized_domain") or ""))
            requested_month = to_month_start(snapshot.get("traffic_month"))
            if not domain or not requested_month:
                counts["snapshots_invalid"] += 1
                continue
            try:
                rows = parse_monthly_rows(payload, domain, requested_month) if isinstance(payload, dict) else []
                rows = add_legacy_snapshot_countries(rows, snapshot, requested_month)
            except Exception as error:
                counts["snapshots_invalid"] += 1
                log_error(
                    "traffic_projection_backfill.parse_failed",
                    snapshot_id=snapshot_id,
                    domain=domain,
                    error=str(error)[:300],
                )
                continue

            captured_at = str(snapshot.get("fetched_at") or "").strip() or utc_now_iso()
            projection_rows = [
                {**row, "_source_snapshot_id": snapshot_id}
                for row in rows
                if row.get("traffic_month")
            ]
            if not projection_rows:
                counts["snapshots_invalid"] += 1
                continue

            snapshot_statements: list[tuple[str, str, list[Any]]] = []
            snapshot_statements.extend(
                (
                    "monthly",
                    *build_domain_traffic_monthly_upsert(
                        domain,
                        row,
                        captured_at=captured_at,
                        source_snapshot_id=snapshot_id,
                    ),
                )
                for row in projection_rows
            )
            country_statements = build_domain_traffic_country_upsert_statements(
                domain,
                projection_rows,
                captured_at=captured_at,
            )
            country_row_count = sum(
                1 for sql, _ in country_statements if sql == DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL
            )
            if country_row_count > 0:
                counts["snapshots_with_country"] += 1
            for sql, params in country_statements:
                if sql == DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL:
                    tag = "country_upsert"
                elif sql == DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL:
                    tag = "country_reconcile"
                else:
                    tag = "country_refresh"
                snapshot_statements.append((tag, sql, params))
            if len(snapshot_statements) > TRAFFIC_BACKFILL_MAX_SNAPSHOT_STATEMENTS:
                raise RuntimeError(
                    "traffic snapshot "
                    f"{snapshot_id} generated {len(snapshot_statements)} statements; "
                    "refusing to split its atomic projection group above "
                    f"{TRAFFIC_BACKFILL_MAX_SNAPSHOT_STATEMENTS}"
                )
            if len(snapshot_statements) > write_batch_size:
                raise ValueError(
                    f"write_batch_size={write_batch_size} is smaller than the "
                    f"{len(snapshot_statements)}-statement atomic projection group "
                    f"for traffic snapshot {snapshot_id}"
                )
            snapshot_statement_groups.append(snapshot_statements)
            counts["snapshots_projected"] += 1

        tagged_batches: list[list[tuple[str, str, list[Any]]]] = []
        tagged_batch: list[tuple[str, str, list[Any]]] = []
        for snapshot_statements in snapshot_statement_groups:
            # D1 batch() is the transaction boundary. Flush only between
            # snapshots so monthly + country reconciliation commits together.
            if (
                tagged_batch
                and len(tagged_batch) + len(snapshot_statements) > write_batch_size
            ):
                tagged_batches.append(tagged_batch)
                tagged_batch = []
            tagged_batch.extend(snapshot_statements)
        if tagged_batch:
            tagged_batches.append(tagged_batch)

        for tagged_batch in tagged_batches:
            await d1.batch([(sql, params) for _, sql, params in tagged_batch])
            for tag, _, _ in tagged_batch:
                if tag == "monthly":
                    counts["monthly_rows_upserted"] += 1
                    continue
                counts["country_statements_applied"] += 1
                if tag == "country_upsert":
                    counts["country_rows_upserted"] += 1
                elif tag == "country_reconcile":
                    counts["country_months_reconciled"] += 1
                else:
                    counts["country_estimates_refreshed"] += 1

        last_snapshot_id = page_last_snapshot_id
        counts["last_snapshot_id"] = last_snapshot_id
        counts["pages_completed"] += 1
        log_info(
            "traffic_projection_backfill.page",
            **counts,
        )

    return counts

async def backfill_domain_traffic_monthly(
    config: Config,
    limit: int | None = None,
    *,
    after_snapshot_id: int = 0,
    page_size: int = 100,
    write_batch_size: int = 40,
) -> dict[str, int]:
    async with D1Client(config) as d1:
        return await backfill_domain_traffic_monthly_from_d1(
            d1,
            limit,
            after_snapshot_id=after_snapshot_id,
            page_size=page_size,
            write_batch_size=write_batch_size,
        )
