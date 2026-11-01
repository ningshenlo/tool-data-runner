from __future__ import annotations

import json
from typing import Any

from report_exports import shift_month

from runner import (
    DOMAIN_STATE_SOURCE,
    TRAFFIC_SOURCE,
    Config,
    D1Client,
    log_error,
    log_info,
    to_integer,
    to_month_start,
    utc_now_iso,
)
from workloads.traffic.parse import previous_traffic_month

MARKET_DR_MIN_BASELINE_DAYS = 20

MARKET_DR_MAX_BASELINE_DAYS = 45

MARKET_DR_TARGET_BASELINE_DAYS = 30

MARKET_MIN_ABSOLUTE_COVERAGE_BPS = {
    "search_tools": 2500,
    "paid_tools": 2500,
    "ai_tools": 100,
    "dr_tools": 2500,
    "dr_comparable_tools": 100,
    "country_tools": 2500,
    "country_ai_tools": 100,
}

MARKET_SNAPSHOT_PUBLIC_TOOL_PREDICATE = """
  t.status = 'published'
  AND t.content_safety_status = 'safe'
  AND t.duplicate_of_tool_id IS NULL
  AND t.verification_status IN ('verified', 'pending')
  AND t.staleness_status IN ('fresh', 'aging')
"""

MARKET_VISIBLE_TOOLS_CTES = """
visible_tools_ranked AS (
  SELECT
    tool.id,
    lower(trim(tool.normalized_domain)) AS normalized_domain,
    primary_taxonomy.term_id AS primary_term_id,
    row_number() OVER (
      PARTITION BY lower(trim(tool.normalized_domain))
      ORDER BY coalesce(tool.first_published_at, tool.listed_at) ASC, tool.id ASC
    ) AS domain_row
  FROM tools tool
  LEFT JOIN current_tool_primary_taxonomy primary_taxonomy
    ON primary_taxonomy.tool_id = tool.id
  WHERE tool.status = 'published'
    AND tool.content_safety_status = 'safe'
    AND tool.duplicate_of_tool_id IS NULL
    AND tool.verification_status IN ('verified', 'pending')
    AND tool.staleness_status IN ('fresh', 'aging')
), visible_tools AS (
  SELECT id, normalized_domain, primary_term_id
  FROM visible_tools_ranked
  WHERE domain_row = 1
)
"""

async def resolve_market_snapshot_months(
    d1: D1Client,
    traffic_month: str | None = None,
) -> tuple[str, str | None]:
    selected_month = to_month_start(traffic_month) if traffic_month else None
    if traffic_month and selected_month is None:
        raise ValueError(f"Invalid market traffic month: {traffic_month}")
    if selected_month is None:
        latest_month_rows = await d1.query(
            """
            SELECT max(traffic.traffic_month) AS traffic_month
            FROM domain_traffic_monthly traffic
            JOIN traffic_month_release_checks release
              ON release.source = traffic.source
             AND release.traffic_month = traffic.traffic_month
             AND release.status = 'available'
            WHERE traffic.source = ? AND traffic.visits IS NOT NULL
            """,
            [TRAFFIC_SOURCE],
        )
        selected_month = (
            to_month_start(latest_month_rows[0].get("traffic_month"))
            if latest_month_rows
            else None
        )
    if selected_month is None:
        raise RuntimeError("No release-gated Similarweb traffic month is available for a market snapshot")

    baseline_rows = await d1.query(
        """
        SELECT max(traffic.traffic_month) AS traffic_month
        FROM domain_traffic_monthly traffic
        JOIN traffic_month_release_checks release
          ON release.source = traffic.source
         AND release.traffic_month = traffic.traffic_month
         AND release.status = 'available'
        WHERE traffic.source = ?
          AND traffic.traffic_month = ?
          AND traffic.visits IS NOT NULL
        """,
        [TRAFFIC_SOURCE, shift_month(selected_month[:7], -1) + '-01'],
    )
    baseline_month = (
        to_month_start(baseline_rows[0].get("traffic_month"))
        if baseline_rows
        else None
    )
    return selected_month, baseline_month

async def preview_market_snapshot_from_d1(
    d1: D1Client,
    traffic_month: str | None = None,
) -> dict[str, Any]:
    selected_month, baseline_month = await resolve_market_snapshot_months(d1, traffic_month)
    rows = await d1.query(
        f"""
        WITH {MARKET_VISIBLE_TOOLS_CTES}
        SELECT
          count(*) AS total_tools,
          coalesce(sum(CASE WHEN traffic.visits IS NOT NULL THEN 1 ELSE 0 END), 0) AS traffic_tools,
          (
            SELECT count(*)
            FROM domain_traffic_country_monthly country
            JOIN visible_tools country_tool
              ON country_tool.normalized_domain = country.normalized_domain
            WHERE country.source = ? AND country.traffic_month = ?
          ) AS country_rows,
          (
            SELECT count(DISTINCT country_tool.id)
            FROM domain_traffic_country_monthly country
            JOIN visible_tools country_tool
              ON country_tool.normalized_domain = country.normalized_domain
            WHERE country.source = ? AND country.traffic_month = ?
          ) AS country_tools
        FROM visible_tools tool
        LEFT JOIN domain_traffic_monthly traffic
          ON traffic.normalized_domain = tool.normalized_domain
         AND traffic.source = ?
         AND traffic.traffic_month = ?
        """,
        [
            TRAFFIC_SOURCE,
            selected_month,
            TRAFFIC_SOURCE,
            selected_month,
            TRAFFIC_SOURCE,
            selected_month,
        ],
    )
    row = rows[0] if rows else {}
    return {
        "status": "dry_run",
        "traffic_month": selected_month,
        "baseline_month": baseline_month,
        "coverage": {
            key: to_integer(row.get(key)) or 0
            for key in ("total_tools", "traffic_tools", "country_rows", "country_tools")
        },
    }

async def ensure_market_catalog_eligibility_revision(d1: D1Client) -> int:
    await d1.run(
        """
        INSERT OR IGNORE INTO market_catalog_eligibility_revision (id, revision)
        VALUES (1, 0)
        """
    )
    rows = await d1.query(
        "SELECT revision FROM market_catalog_eligibility_revision WHERE id = 1"
    )
    revision = to_integer(rows[0].get("revision")) if rows else None
    if revision is None or revision < 0:
        raise RuntimeError("Market catalog eligibility revision is unavailable")
    return revision

async def build_market_snapshot_facet_rollups_from_d1(
    d1: D1Client,
    snapshot_id: int,
    catalog_eligibility_revision: int,
    built_at: str,
) -> None:
    statements = [
            (
                "DELETE FROM taxonomy_market_snapshot_facet_rollups WHERE snapshot_id = ?",
                [snapshot_id],
            ),
            (
                f"""
                INSERT INTO taxonomy_market_snapshot_facet_rollups (
                  snapshot_id, primary_term_id, primary_term_value,
                  country_code, tool_count, built_at
                )
                SELECT ?, NULL, '', '', count(*), ?
                FROM tool_market_snapshots snapshot
                JOIN tools t ON t.id = snapshot.tool_id
                JOIN market_catalog_eligibility_revision revision
                  ON revision.id = 1 AND revision.revision = ?
                WHERE snapshot.snapshot_id = ?
                  AND {MARKET_SNAPSHOT_PUBLIC_TOOL_PREDICATE}
                HAVING count(*) > 0
                """,
                [snapshot_id, built_at, catalog_eligibility_revision, snapshot_id],
            ),
            (
                f"""
                INSERT INTO taxonomy_market_snapshot_facet_rollups (
                  snapshot_id, primary_term_id, primary_term_value,
                  country_code, tool_count, built_at
                )
                SELECT
                  snapshot.snapshot_id,
                  snapshot.primary_term_id,
                  term.slug,
                  '',
                  count(*),
                  ?
                FROM tool_market_snapshots snapshot
                JOIN tools t ON t.id = snapshot.tool_id
                JOIN taxonomy_terms term ON term.id = snapshot.primary_term_id
                JOIN market_catalog_eligibility_revision revision
                  ON revision.id = 1 AND revision.revision = ?
                WHERE snapshot.snapshot_id = ?
                  AND {MARKET_SNAPSHOT_PUBLIC_TOOL_PREDICATE}
                GROUP BY
                  snapshot.snapshot_id,
                  snapshot.primary_term_id,
                  term.slug
                """,
                [built_at, catalog_eligibility_revision, snapshot_id],
            ),
            (
                f"""
                WITH country_domains AS (
                  SELECT
                    country.snapshot_id,
                    snapshot.primary_term_id,
                    term.slug AS primary_term_value,
                    country.country_code,
                    country.normalized_domain,
                    max(country.estimated_visits) AS estimated_visits,
                    max(country.estimated_ai_visits) AS estimated_ai_visits
                  FROM tool_country_market_snapshots country
                  JOIN tool_market_snapshots snapshot
                    ON snapshot.snapshot_id = country.snapshot_id
                   AND snapshot.tool_id = country.tool_id
                  JOIN tools t ON t.id = snapshot.tool_id
                  LEFT JOIN taxonomy_terms term ON term.id = snapshot.primary_term_id
                  JOIN market_catalog_eligibility_revision revision
                    ON revision.id = 1 AND revision.revision = ?
                  WHERE country.snapshot_id = ?
                    AND country.country_code = ?
                    AND {MARKET_SNAPSHOT_PUBLIC_TOOL_PREDICATE}
                  GROUP BY
                    country.snapshot_id,
                    snapshot.primary_term_id,
                    term.slug,
                    country.country_code,
                    country.normalized_domain
                )
                INSERT INTO taxonomy_market_snapshot_facet_rollups (
                  snapshot_id, primary_term_id, primary_term_value,
                  country_code, tool_count,
                  country_estimated_visits, country_estimated_ai_visits,
                  country_estimated_visits_unknown_count,
                  country_estimated_ai_visits_unknown_count,
                  built_at
                )
                SELECT
                  snapshot_id,
                  NULL,
                  '',
                  country_code,
                  count(*),
                  sum(estimated_visits),
                  sum(estimated_ai_visits),
                  sum(CASE WHEN estimated_visits IS NULL THEN 1 ELSE 0 END),
                  sum(CASE WHEN estimated_ai_visits IS NULL THEN 1 ELSE 0 END),
                  ?
                FROM country_domains
                GROUP BY snapshot_id, country_code
                UNION ALL
                SELECT
                  snapshot_id,
                  primary_term_id,
                  primary_term_value,
                  country_code,
                  count(*),
                  sum(estimated_visits),
                  sum(estimated_ai_visits),
                  sum(CASE WHEN estimated_visits IS NULL THEN 1 ELSE 0 END),
                  sum(CASE WHEN estimated_ai_visits IS NULL THEN 1 ELSE 0 END),
                  ?
                FROM country_domains
                WHERE primary_term_id IS NOT NULL
                  AND primary_term_value IS NOT NULL
                GROUP BY
                  snapshot_id,
                  primary_term_id,
                  primary_term_value,
                  country_code
                """,
                [
                    catalog_eligibility_revision,
                    snapshot_id,
                    built_at,
                    built_at,
                ],
            ),
        ]
    await d1.batch(statements[:3])
    countries = await d1.query(
        "SELECT DISTINCT country_code FROM tool_country_market_snapshots WHERE snapshot_id = ? ORDER BY country_code",
        [snapshot_id],
    )
    country_sql, country_params = statements[3]
    for country in countries:
        await d1.run(country_sql, [*country_params[:2], country['country_code'], *country_params[2:]])

async def get_market_snapshot_coverage(d1: D1Client, snapshot_id: int) -> dict[str, int]:
    rows = await d1.query(
        """
        WITH target AS (SELECT CAST(? AS INTEGER) AS snapshot_id)
        SELECT
          count(snapshot.tool_id) AS total_tools,
          coalesce(sum(CASE WHEN visits IS NOT NULL THEN 1 ELSE 0 END), 0) AS traffic_tools,
          coalesce(sum(CASE WHEN search_share_bps IS NOT NULL THEN 1 ELSE 0 END), 0) AS search_tools,
          coalesce(sum(CASE WHEN paid_search_share_bps IS NOT NULL THEN 1 ELSE 0 END), 0) AS paid_tools,
          coalesce(sum(CASE WHEN ai_visits IS NOT NULL THEN 1 ELSE 0 END), 0) AS ai_tools,
          coalesce(sum(CASE WHEN domain_rating IS NOT NULL THEN 1 ELSE 0 END), 0) AS dr_tools,
          coalesce(sum(CASE WHEN domain_rating_change IS NOT NULL THEN 1 ELSE 0 END), 0) AS dr_comparable_tools,
          coalesce(sum(CASE WHEN primary_term_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS category_tools,
          (
            SELECT count(*)
            FROM tool_country_market_snapshots country
            WHERE country.snapshot_id = target.snapshot_id
          ) AS country_rows,
          (
            SELECT count(DISTINCT country.tool_id)
            FROM tool_country_market_snapshots country
            WHERE country.snapshot_id = target.snapshot_id
          ) AS country_tools,
          (
            SELECT count(DISTINCT country.country_code)
            FROM tool_country_market_snapshots country
            WHERE country.snapshot_id = target.snapshot_id
          ) AS countries,
          (
            SELECT count(*)
            FROM tool_country_market_snapshots country
            WHERE country.snapshot_id = target.snapshot_id
              AND country.estimated_visits IS NULL
          ) AS country_estimated_unknown_rows,
          (
            SELECT count(*)
            FROM tool_country_market_snapshots country
            WHERE country.snapshot_id = target.snapshot_id
              AND country.estimated_ai_visits IS NOT NULL
          ) AS country_ai_rows,
          (
            SELECT count(DISTINCT country.tool_id)
            FROM tool_country_market_snapshots country
            WHERE country.snapshot_id = target.snapshot_id
              AND country.estimated_ai_visits IS NOT NULL
          ) AS country_ai_tools,
          (
            SELECT count(*)
            FROM tool_country_market_snapshots country
            JOIN tool_market_snapshots country_snapshot
              ON country_snapshot.snapshot_id = country.snapshot_id
             AND country_snapshot.tool_id = country.tool_id
            WHERE country.snapshot_id = target.snapshot_id
              AND country_snapshot.primary_term_id IS NOT NULL
          ) AS country_category_rows,
          coalesce((
            SELECT version.catalog_eligibility_revision
            FROM market_snapshot_versions version
            WHERE version.id = target.snapshot_id
          ), -1) AS snapshot_catalog_eligibility_revision,
          coalesce((
            SELECT revision.revision
            FROM market_catalog_eligibility_revision revision
            WHERE revision.id = 1
          ), -1) AS current_catalog_eligibility_revision,
          (
            SELECT count(*)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
          ) AS facet_rollup_rows,
          (
            SELECT count(*)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NULL
              AND rollup.country_code = ''
          ) AS facet_rollup_global_rows,
          coalesce((
            SELECT rollup.tool_count
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NULL
              AND rollup.country_code = ''
          ), 0) AS facet_rollup_global_tools,
          (
            SELECT count(*)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NOT NULL
              AND rollup.country_code = ''
          ) AS facet_rollup_category_rows,
          coalesce((
            SELECT sum(rollup.tool_count)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NOT NULL
              AND rollup.country_code = ''
          ), 0) AS facet_rollup_category_tools,
          (
            SELECT count(*)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NULL
              AND rollup.country_code <> ''
          ) AS facet_rollup_country_rows,
          coalesce((
            SELECT sum(rollup.tool_count)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NULL
              AND rollup.country_code <> ''
          ), 0) AS facet_rollup_country_tools,
          coalesce((
            SELECT sum(rollup.country_estimated_visits_unknown_count)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NULL
              AND rollup.country_code <> ''
          ), 0) AS facet_rollup_country_estimated_unknown_rows,
          coalesce((
            SELECT sum(rollup.country_estimated_ai_visits_unknown_count)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NULL
              AND rollup.country_code <> ''
          ), 0) AS facet_rollup_country_ai_unknown_rows,
          (
            SELECT count(*)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NOT NULL
              AND rollup.country_code <> ''
          ) AS facet_rollup_category_country_rows,
          coalesce((
            SELECT sum(rollup.tool_count)
            FROM taxonomy_market_snapshot_facet_rollups rollup
            WHERE rollup.snapshot_id = target.snapshot_id
              AND rollup.primary_term_id IS NOT NULL
              AND rollup.country_code <> ''
          ), 0) AS facet_rollup_category_country_tools
        FROM target
        LEFT JOIN tool_market_snapshots snapshot
          ON snapshot.snapshot_id = target.snapshot_id
        GROUP BY target.snapshot_id
        """,
        [snapshot_id],
    )
    row = rows[0] if rows else {}
    return {
        key: to_integer(row.get(key)) or 0
        for key in (
            "total_tools",
            "traffic_tools",
            "search_tools",
            "paid_tools",
            "ai_tools",
            "dr_tools",
            "dr_comparable_tools",
            "category_tools",
            "country_rows",
            "country_tools",
            "countries",
            "country_estimated_unknown_rows",
            "country_ai_rows",
            "country_ai_tools",
            "country_category_rows",
            "snapshot_catalog_eligibility_revision",
            "current_catalog_eligibility_revision",
            "facet_rollup_rows",
            "facet_rollup_global_rows",
            "facet_rollup_global_tools",
            "facet_rollup_category_rows",
            "facet_rollup_category_tools",
            "facet_rollup_country_rows",
            "facet_rollup_country_tools",
            "facet_rollup_country_estimated_unknown_rows",
            "facet_rollup_country_ai_unknown_rows",
            "facet_rollup_category_country_rows",
            "facet_rollup_category_country_tools",
        )
    }

def market_snapshot_coverage_payload(coverage: dict[str, int]) -> dict[str, Any]:
    return {
        **coverage,
        "share_unit": "basis_points",
        "country_scope": "provider_top5_observed_only",
        "country_absence": "unknown",
        "country_estimate_method": "tool_visits_times_country_share",
        "country_estimated_visits_scope": "provider_top5_lower_bound",
        "country_ai_visits_provenance": "modeled_not_observed",
        "country_ai_visits_formula": "estimated_visits * gen_ai_share_bps / 10000",
        "country_ai_visits_scope": "provider_top5_lower_bound",
        "facet_rollup_version": "v1",
        "facet_rollup_grains": ["global", "category", "country", "category_country"],
        "facet_rollup_threshold_policy": "real_time",
        "facet_rollup_country_provenance": "provider_top5_modeled_lower_bound",
        "dr_baseline_target_days": MARKET_DR_TARGET_BASELINE_DAYS,
        "dr_baseline_window_days": [
            MARKET_DR_MIN_BASELINE_DAYS,
            MARKET_DR_MAX_BASELINE_DAYS,
        ],
    }

def validate_market_snapshot_facet_rollups(
    snapshot_id: int,
    coverage: dict[str, int],
) -> None:
    mismatches: list[str] = []
    expected = {
        "current_catalog_eligibility_revision": coverage[
            "snapshot_catalog_eligibility_revision"
        ],
        "facet_rollup_global_rows": 1,
        "facet_rollup_global_tools": coverage["total_tools"],
        "facet_rollup_category_tools": coverage["category_tools"],
        "facet_rollup_country_rows": coverage["countries"],
        "facet_rollup_country_tools": coverage["country_rows"],
        "facet_rollup_country_estimated_unknown_rows": coverage[
            "country_estimated_unknown_rows"
        ],
        "facet_rollup_country_ai_unknown_rows": (
            coverage["country_rows"] - coverage["country_ai_rows"]
        ),
        "facet_rollup_category_country_tools": coverage["country_category_rows"],
        "facet_rollup_rows": (
            1
            + coverage["facet_rollup_category_rows"]
            + coverage["facet_rollup_country_rows"]
            + coverage["facet_rollup_category_country_rows"]
        ),
    }
    for key, expected_value in expected.items():
        current_value = coverage[key]
        if current_value != expected_value:
            mismatches.append(f"{key}={current_value} expected={expected_value}")
    if mismatches:
        raise RuntimeError(
            f"Market snapshot {snapshot_id} has incomplete facet rollup read model: "
            + ", ".join(mismatches)
        )

async def activate_market_snapshot_from_d1(
    d1: D1Client,
    snapshot_id: int,
    *,
    minimum_coverage_ratio: float = 0.95,
) -> dict[str, Any]:
    versions = await d1.query(
        """
        SELECT id, status, traffic_source, traffic_month
        FROM market_snapshot_versions
        WHERE id = ?
        LIMIT 1
        """,
        [snapshot_id],
    )
    if not versions:
        raise RuntimeError(f"Market snapshot {snapshot_id} does not exist")
    version = versions[0]
    # Resolve through runner so unit tests can patch coverage / validation helpers.
    import runner as _runner

    if version.get("status") == "active":
        return {
            "snapshot_id": snapshot_id,
            "status": "active",
            "previous_snapshot_id": None,
            "coverage": await _runner.get_market_snapshot_coverage(d1, snapshot_id),
        }
    if version.get("status") != "candidate":
        raise RuntimeError(
            f"Market snapshot {snapshot_id} must be candidate before activation; "
            f"status={version.get('status')}"
        )

    active_versions = await d1.query(
        """
        SELECT id
        FROM market_snapshot_versions
        WHERE traffic_source = ? AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
        """,
        [version.get("traffic_source") or TRAFFIC_SOURCE],
    )
    previous_snapshot_id = to_integer(active_versions[0].get("id")) if active_versions else None

    coverage = await _runner.get_market_snapshot_coverage(d1, snapshot_id)
    if (
        coverage["total_tools"] <= 0
        or coverage["traffic_tools"] <= 0
        or coverage["country_tools"] <= 0
    ):
        raise RuntimeError(
            f"Market snapshot {snapshot_id} has incomplete required coverage: {coverage}"
        )
    _runner.validate_market_snapshot_facet_rollups(snapshot_id, coverage)

    for coverage_key, minimum_ratio_bps in MARKET_MIN_ABSOLUTE_COVERAGE_BPS.items():
        minimum_count = max(
            1,
            (coverage["traffic_tools"] * minimum_ratio_bps + 9999) // 10000,
        )
        if coverage[coverage_key] < minimum_count:
            raise RuntimeError(
                f"Market snapshot {snapshot_id} failed {coverage_key} absolute coverage gate: "
                f"current={coverage[coverage_key]}, required={minimum_count}, "
                f"traffic_tools={coverage['traffic_tools']}"
            )

    if previous_snapshot_id is not None and previous_snapshot_id != snapshot_id:
        previous_coverage = await _runner.get_market_snapshot_coverage(d1, previous_snapshot_id)
        for coverage_key in (
            "total_tools",
            "traffic_tools",
            "search_tools",
            "paid_tools",
            "ai_tools",
            "dr_tools",
            "dr_comparable_tools",
            "country_rows",
            "country_tools",
            "country_ai_rows",
            "country_ai_tools",
        ):
            previous_value = previous_coverage[coverage_key]
            current_value = coverage[coverage_key]
            if previous_value > 0 and current_value < previous_value * minimum_coverage_ratio:
                raise RuntimeError(
                    f"Market snapshot {snapshot_id} failed {coverage_key} coverage gate: "
                    f"current={current_value}, previous={previous_value}, "
                    f"minimum_ratio={minimum_coverage_ratio}"
                )

    now = utc_now_iso()
    await d1.batch(
        [
            (
                """
                UPDATE market_snapshot_versions
                SET status = 'retired', retired_at = ?, updated_at = ?
                WHERE traffic_source = ? AND status = 'active' AND id <> ?
                  AND EXISTS (
                    SELECT 1
                    FROM market_snapshot_versions candidate
                    JOIN market_catalog_eligibility_revision revision ON revision.id = 1
                    WHERE candidate.id = ?
                      AND candidate.status = 'candidate'
                      AND candidate.catalog_eligibility_revision = revision.revision
                      AND candidate.traffic_month >= market_snapshot_versions.traffic_month
                  )
                """,
                [
                    now,
                    now,
                    version.get("traffic_source") or TRAFFIC_SOURCE,
                    snapshot_id,
                    snapshot_id,
                ],
            ),
            (
                """
                UPDATE market_snapshot_versions
                SET status = 'active', activated_at = ?, retired_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'candidate'
                  AND NOT EXISTS (
                    SELECT 1 FROM market_snapshot_versions newer
                    WHERE newer.traffic_source = market_snapshot_versions.traffic_source
                      AND newer.status = 'active'
                      AND newer.traffic_month > market_snapshot_versions.traffic_month
                  )
                  AND catalog_eligibility_revision = (
                    SELECT revision
                    FROM market_catalog_eligibility_revision
                    WHERE id = 1
                  )
                """,
                [now, now, snapshot_id],
            ),
        ]
    )
    activated = await d1.query(
        "SELECT status FROM market_snapshot_versions WHERE id = ?",
        [snapshot_id],
    )
    if not activated or activated[0].get("status") != "active":
        raise RuntimeError(f"Market snapshot {snapshot_id} activation did not commit")
    return {
        "snapshot_id": snapshot_id,
        "status": "active",
        "previous_snapshot_id": previous_snapshot_id,
        "coverage": coverage,
    }

async def run_market_snapshot_pages(d1, sql, params, ranges, *, page_param_index=0):
    for lower, upper in ranges:
        await d1.run(sql, [*params[:page_param_index], lower, upper, *params[page_param_index:]])

async def build_market_snapshot_from_d1(
    d1: D1Client,
    traffic_month: str | None = None,
    *,
    activate: bool = False,
) -> dict[str, Any]:
    selected_month, baseline_month = await resolve_market_snapshot_months(d1, traffic_month)
    catalog_eligibility_revision = await ensure_market_catalog_eligibility_revision(d1)
    now = utc_now_iso()
    inserted_versions = await d1.query(
        """
        INSERT INTO market_snapshot_versions (
          status, traffic_source, traffic_month, baseline_month, dr_source,
          dr_baseline_days, catalog_eligibility_revision, coverage_json,
          build_started_at, updated_at
        )
        VALUES ('building', ?, ?, ?, ?, ?, ?, '{}', ?, ?)
        RETURNING id
        """,
        [
            TRAFFIC_SOURCE,
            selected_month,
            baseline_month,
            DOMAIN_STATE_SOURCE,
            MARKET_DR_TARGET_BASELINE_DAYS,
            catalog_eligibility_revision,
            now,
            now,
        ],
    )
    snapshot_id = to_integer(inserted_versions[0].get("id")) if inserted_versions else None
    if snapshot_id is None:
        raise RuntimeError("Market snapshot builder did not return a snapshot id")

    try:
        eligible_ids = await d1.query(f"WITH {MARKET_VISIBLE_TOOLS_CTES} SELECT id FROM visible_tools ORDER BY id")
        ids = [row['id'] for row in eligible_ids]
        # Bound index writes and restrict traffic/rating reads to the same page.
        # Candidate rows remain private until all pages and coverage gates pass.
        ranges = [(ids[start - 1] if start else 0, ids[min(start + 49, len(ids) - 1)]) for start in range(0, len(ids), 50)]
        await run_market_snapshot_pages(
            d1,
            f"""
            WITH {MARKET_VISIBLE_TOOLS_CTES}, selected_tools AS MATERIALIZED (
              SELECT * FROM visible_tools WHERE id > ? AND id <= ?
            ), current_raw AS (
              SELECT
                traffic.normalized_domain,
                traffic.visits,
                traffic.ai_visits,
                traffic.gen_ai_traffic_share,
                traffic.captured_at,
                coalesce(
                  cast(json_extract(traffic.metrics_json, '$.traffic_sources.search') AS REAL),
                  cast(json_extract(traffic.metrics_json, '$.search_traffic_share') AS REAL)
                ) AS explicit_search_share,
                coalesce(
                  cast(json_extract(traffic.metrics_json, '$.traffic_sources.search_organic') AS REAL),
                  cast(json_extract(traffic.metrics_json, '$.search_organic_traffic_share') AS REAL)
                ) AS organic_search_share,
                coalesce(
                  cast(json_extract(traffic.metrics_json, '$.traffic_sources.search_paid') AS REAL),
                  cast(json_extract(traffic.metrics_json, '$.search_paid_traffic_share') AS REAL)
                ) AS paid_search_share
              FROM domain_traffic_monthly traffic
              WHERE traffic.source = ? AND traffic.traffic_month = ?
                AND traffic.normalized_domain IN (SELECT normalized_domain FROM selected_tools)
            ), current_computed AS (
              SELECT
                current_raw.*,
                CASE
                  WHEN explicit_search_share IS NOT NULL THEN explicit_search_share
                  WHEN organic_search_share IS NOT NULL OR paid_search_share IS NOT NULL
                    THEN coalesce(organic_search_share, 0) + coalesce(paid_search_share, 0)
                END AS search_share
              FROM current_raw
            ), current_traffic AS (
              SELECT
                current_computed.*,
                CASE WHEN search_share BETWEEN 0 AND 1
                  THEN cast(round(search_share * 10000.0) AS INTEGER) END AS search_share_bps,
                CASE WHEN organic_search_share BETWEEN 0 AND 1
                  THEN cast(round(organic_search_share * 10000.0) AS INTEGER) END AS organic_search_share_bps,
                CASE WHEN paid_search_share BETWEEN 0 AND 1
                  THEN cast(round(paid_search_share * 10000.0) AS INTEGER) END AS paid_search_share_bps,
                CASE WHEN gen_ai_traffic_share BETWEEN 0 AND 1
                  THEN cast(round(gen_ai_traffic_share * 10000.0) AS INTEGER) END AS gen_ai_share_bps
              FROM current_computed
            ), baseline_traffic AS (
              SELECT normalized_domain, visits, ai_visits
              FROM domain_traffic_monthly
              WHERE source = ? AND traffic_month = ?
                AND normalized_domain IN (SELECT normalized_domain FROM selected_tools)
            ), rating_latest_ranked AS (
              SELECT
                normalized_domain,
                source,
                observed_date,
                domain_rating,
                row_number() OVER (
                  PARTITION BY normalized_domain
                  ORDER BY observed_date DESC
                ) AS latest_row
              FROM domain_rating_history
              WHERE source = ?
                AND normalized_domain IN (SELECT normalized_domain FROM selected_tools)
            ), rating_latest AS (
              SELECT
                normalized_domain,
                source,
                observed_date AS latest_date,
                domain_rating AS latest_rating
              FROM rating_latest_ranked
              WHERE latest_row = 1
            ), rating_baseline_ranked AS (
              SELECT
                latest.normalized_domain,
                baseline.observed_date AS previous_date,
                baseline.domain_rating AS previous_rating,
                row_number() OVER (
                  PARTITION BY latest.normalized_domain
                  ORDER BY
                    abs(
                      julianday(latest.latest_date)
                      - julianday(baseline.observed_date)
                      - {MARKET_DR_TARGET_BASELINE_DAYS}
                    ),
                    baseline.observed_date DESC
                ) AS baseline_row
              FROM rating_latest latest
              JOIN domain_rating_history baseline
                ON baseline.normalized_domain = latest.normalized_domain
               AND baseline.source = latest.source
               AND baseline.observed_date < latest.latest_date
              WHERE julianday(latest.latest_date) - julianday(baseline.observed_date)
                BETWEEN {MARKET_DR_MIN_BASELINE_DAYS} AND {MARKET_DR_MAX_BASELINE_DAYS}
            ), rating_pairs AS (
              SELECT
                latest.normalized_domain,
                latest.latest_date,
                baseline.previous_date,
                latest.latest_rating,
                baseline.previous_rating
              FROM rating_latest latest
              LEFT JOIN rating_baseline_ranked baseline
                ON baseline.normalized_domain = latest.normalized_domain
               AND baseline.baseline_row = 1
            )
            INSERT INTO tool_market_snapshots (
              snapshot_id, tool_id, normalized_domain, primary_term_id,
              visits, previous_visits, visits_change, visits_growth_rate,
              search_share_bps, organic_search_share_bps, paid_search_share_bps,
              search_visits, paid_search_visits,
              ai_visits, previous_ai_visits, ai_visits_change, ai_visits_growth_rate,
              gen_ai_share_bps,
              domain_rating, previous_domain_rating, domain_rating_change,
              domain_rating_velocity_30d, domain_rating_observed_date,
              previous_domain_rating_observed_date, captured_at, updated_at
            )
            SELECT
              ?,
              tool.id,
              tool.normalized_domain,
              tool.primary_term_id,
              current.visits,
              baseline.visits,
              CASE WHEN current.visits IS NOT NULL AND baseline.visits IS NOT NULL
                THEN current.visits - baseline.visits END,
              CASE WHEN current.visits IS NOT NULL AND baseline.visits > 0
                THEN cast(current.visits - baseline.visits AS REAL) / baseline.visits END,
              current.search_share_bps,
              current.organic_search_share_bps,
              current.paid_search_share_bps,
              CASE WHEN current.visits IS NOT NULL AND current.search_share_bps IS NOT NULL
                THEN cast(round(current.visits * current.search_share_bps / 10000.0) AS INTEGER) END,
              CASE WHEN current.visits IS NOT NULL AND current.paid_search_share_bps IS NOT NULL
                THEN cast(round(current.visits * current.paid_search_share_bps / 10000.0) AS INTEGER) END,
              current.ai_visits,
              baseline.ai_visits,
              CASE WHEN current.ai_visits IS NOT NULL AND baseline.ai_visits IS NOT NULL
                THEN current.ai_visits - baseline.ai_visits END,
              CASE WHEN current.ai_visits IS NOT NULL AND baseline.ai_visits > 0
                THEN cast(current.ai_visits - baseline.ai_visits AS REAL) / baseline.ai_visits END,
              current.gen_ai_share_bps,
              rating.latest_rating,
              rating.previous_rating,
              CASE WHEN rating.previous_rating IS NOT NULL
                    AND julianday(rating.latest_date) - julianday(rating.previous_date)
                      BETWEEN {MARKET_DR_MIN_BASELINE_DAYS} AND {MARKET_DR_MAX_BASELINE_DAYS}
                THEN rating.latest_rating - rating.previous_rating END,
              CASE WHEN rating.previous_rating IS NOT NULL
                    AND julianday(rating.latest_date) - julianday(rating.previous_date)
                      BETWEEN {MARKET_DR_MIN_BASELINE_DAYS} AND {MARKET_DR_MAX_BASELINE_DAYS}
                THEN (rating.latest_rating - rating.previous_rating) * {MARKET_DR_TARGET_BASELINE_DAYS}.0
                  / nullif(julianday(rating.latest_date) - julianday(rating.previous_date), 0) END,
              rating.latest_date,
              rating.previous_date,
              current.captured_at,
              ?
            FROM selected_tools tool
            LEFT JOIN current_traffic current
              ON current.normalized_domain = tool.normalized_domain
            LEFT JOIN baseline_traffic baseline
              ON baseline.normalized_domain = tool.normalized_domain
            LEFT JOIN rating_pairs rating
              ON rating.normalized_domain = tool.normalized_domain
            """,
            [
                TRAFFIC_SOURCE,
                selected_month,
                TRAFFIC_SOURCE,
                baseline_month,
                DOMAIN_STATE_SOURCE,
                snapshot_id,
                now,
            ],
            ranges,
        )
        await run_market_snapshot_pages(
            d1,
            """
            WITH country_estimates AS (
              SELECT
                snapshot.snapshot_id,
                snapshot.tool_id,
                snapshot.normalized_domain,
                country.country_code,
                country.country_position,
                country.traffic_share_bps,
                CASE
                  WHEN snapshot.visits IS NOT NULL
                    THEN cast(round(snapshot.visits * country.traffic_share_bps / 10000.0) AS INTEGER)
                  ELSE country.estimated_visits
                END AS estimated_visits,
                snapshot.gen_ai_share_bps,
                country.captured_at
              FROM tool_market_snapshots snapshot
              JOIN domain_traffic_country_monthly country
                ON country.normalized_domain = snapshot.normalized_domain
               AND country.source = ?
               AND country.traffic_month = ?
              WHERE snapshot.snapshot_id = ?
                AND snapshot.tool_id > ? AND snapshot.tool_id <= ?
            )
            INSERT INTO tool_country_market_snapshots (
              snapshot_id, tool_id, normalized_domain, country_code,
              country_position, traffic_share_bps, estimated_visits, estimated_ai_visits,
              captured_at, updated_at
            )
            SELECT
              estimate.snapshot_id,
              estimate.tool_id,
              estimate.normalized_domain,
              estimate.country_code,
              estimate.country_position,
              estimate.traffic_share_bps,
              estimate.estimated_visits,
              CASE
                WHEN estimate.estimated_visits IS NOT NULL
                     AND estimate.gen_ai_share_bps IS NOT NULL
                  THEN cast(
                    round(estimate.estimated_visits * estimate.gen_ai_share_bps / 10000.0)
                    AS INTEGER
                  )
              END,
              estimate.captured_at,
              ?
            FROM country_estimates estimate
            """,
            [TRAFFIC_SOURCE, selected_month, snapshot_id, now],
            ranges,
            page_param_index=3,
        )
        await build_market_snapshot_facet_rollups_from_d1(
            d1,
            snapshot_id,
            catalog_eligibility_revision,
            now,
        )
        coverage = await get_market_snapshot_coverage(d1, snapshot_id)
        if coverage["total_tools"] <= 0 or coverage["traffic_tools"] <= 0:
            raise RuntimeError(f"Market snapshot {snapshot_id} produced no usable catalog rows")
        validate_market_snapshot_facet_rollups(snapshot_id, coverage)
        built_at = utc_now_iso()
        await d1.run(
            """
            UPDATE market_snapshot_versions
            SET status = 'candidate', coverage_json = ?, built_at = ?, updated_at = ?, last_error = NULL
            WHERE id = ? AND status = 'building'
            """,
            [
                json.dumps(market_snapshot_coverage_payload(coverage), sort_keys=True),
                built_at,
                built_at,
                snapshot_id,
            ],
        )
    except Exception as error:
        failed_at = utc_now_iso()
        try:
            await d1.run(
                """
                UPDATE market_snapshot_versions
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'building'
                """,
                [str(error)[:2000], failed_at, snapshot_id],
            )
        except Exception as status_error:
            log_error(
                "market_snapshot.failed_status_write",
                snapshot_id=snapshot_id,
                error=str(status_error)[:300],
            )
        raise

    result: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "status": "candidate",
        "traffic_month": selected_month,
        "baseline_month": baseline_month,
        "coverage": coverage,
    }
    if activate:
        activation = await activate_market_snapshot_from_d1(d1, snapshot_id)
        result.update(activation)
    return result

async def build_market_snapshot(
    config: Config,
    traffic_month: str | None = None,
    *,
    activate: bool = False,
) -> dict[str, Any]:
    async with D1Client(config) as d1:
        return await build_market_snapshot_from_d1(
            d1,
            traffic_month,
            activate=activate,
        )

async def activate_market_snapshot(
    config: Config,
    snapshot_id: int,
) -> dict[str, Any]:
    async with D1Client(config) as d1:
        return await activate_market_snapshot_from_d1(d1, snapshot_id)

async def preview_market_snapshot(
    config: Config,
    traffic_month: str | None = None,
) -> dict[str, Any]:
    async with D1Client(config) as d1:
        return await preview_market_snapshot_from_d1(d1, traffic_month)
