"""Static defaults and knobs for tool-data-runner.

Runtime secrets and per-deploy overrides still come from environment via
``runner.load_config``. This module holds stable product defaults that code
imports by name.
"""

from __future__ import annotations

# --- traffic / Similarweb -------------------------------------------------
SIMILARWEB_API_BASE = "https://data.similarweb.com/api/v1/data"
SIMILARWEB_EXTENSION_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
SIMILARWEB_EXTENSION_ID = "hoklmmgfnpapgjgcpechhaamimifchmp"
SIMILARWEB_EXTENSION_VERSION_FALLBACK = "6.12.21"
SIMILARWEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
TRAFFIC_SOURCE = "similarweb"
TRAFFIC_METRICS_SCHEMA_VERSION = 2
TRAFFIC_BACKFILL_MAX_SNAPSHOT_STATEMENTS = 32

DOMAIN_TRAFFIC_MONTHLY_UPSERT_SQL = """
    INSERT INTO domain_traffic_monthly (
      normalized_domain, source, traffic_month, visits, global_rank,
      country_rank_country, country_rank, bounce_rate, pages_per_visit,
      avg_visit_duration_seconds, gen_ai_traffic_share, ai_visits,
      ai_referral_share, metrics_json, metrics_schema_version,
      source_snapshot_id, captured_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (normalized_domain, source, traffic_month) DO UPDATE
    SET visits = coalesce(excluded.visits, domain_traffic_monthly.visits),
        global_rank = coalesce(excluded.global_rank, domain_traffic_monthly.global_rank),
        country_rank_country = coalesce(
          excluded.country_rank_country,
          domain_traffic_monthly.country_rank_country
        ),
        country_rank = coalesce(excluded.country_rank, domain_traffic_monthly.country_rank),
        bounce_rate = coalesce(excluded.bounce_rate, domain_traffic_monthly.bounce_rate),
        pages_per_visit = coalesce(
          excluded.pages_per_visit,
          domain_traffic_monthly.pages_per_visit
        ),
        avg_visit_duration_seconds = coalesce(
          excluded.avg_visit_duration_seconds,
          domain_traffic_monthly.avg_visit_duration_seconds
        ),
        gen_ai_traffic_share = coalesce(
          excluded.gen_ai_traffic_share,
          domain_traffic_monthly.gen_ai_traffic_share
        ),
        ai_visits = coalesce(excluded.ai_visits, domain_traffic_monthly.ai_visits),
        ai_referral_share = coalesce(
          excluded.ai_referral_share,
          domain_traffic_monthly.ai_referral_share
        ),
        metrics_json = json_patch(domain_traffic_monthly.metrics_json, excluded.metrics_json),
        metrics_schema_version = max(
          domain_traffic_monthly.metrics_schema_version,
          excluded.metrics_schema_version
        ),
        source_snapshot_id = coalesce(
          excluded.source_snapshot_id,
          domain_traffic_monthly.source_snapshot_id
        ),
        captured_at = excluded.captured_at,
        updated_at = excluded.captured_at
"""

DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL = """
    DELETE FROM domain_traffic_country_monthly
    WHERE normalized_domain = ? AND source = ? AND traffic_month = ?
"""

DOMAIN_TRAFFIC_COUNTRY_REFRESH_ESTIMATES_SQL = """
    UPDATE domain_traffic_country_monthly
    SET estimated_visits = cast(round(? * traffic_share_bps / 10000.0) AS INTEGER),
        updated_at = ?
    WHERE normalized_domain = ? AND source = ? AND traffic_month = ?
"""

DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL = """
    INSERT INTO domain_traffic_country_monthly (
      normalized_domain, source, traffic_month, country_code, country_position,
      traffic_share_bps, estimated_visits, source_snapshot_id, captured_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (normalized_domain, source, traffic_month, country_code) DO UPDATE
    SET country_position = excluded.country_position,
        traffic_share_bps = excluded.traffic_share_bps,
        estimated_visits = excluded.estimated_visits,
        source_snapshot_id = coalesce(
          excluded.source_snapshot_id,
          domain_traffic_country_monthly.source_snapshot_id
        ),
        captured_at = excluded.captured_at,
        updated_at = excluded.captured_at
"""

# --- domain state / Ahrefs / RDAP -----------------------------------------
DOMAIN_STATE_SOURCE = "ahrefs"
AHREFS_DOMAIN_RATING_URL = "https://api.ahrefs.com/v3/public/domain-rating-free"
AHREFS_DEFAULT_REQUESTS_PER_MINUTE = 60
AHREFS_MAX_REQUESTS_PER_MINUTE = 60
IANA_RDAP_DNS = "https://data.iana.org/rdap/dns.json"
RDAP_USER_AGENT = "traffic-runner-domain-whois/0.1"

# --- assets / browser rendering -------------------------------------------
ASSET_SOURCE = "site_scraper"
ASSET_DB_STORAGE_BUCKET = "sitesimgs"
DEFAULT_R2_BUCKET = "sitesimgs"
ASSET_REQUIREMENT_ORDER = ("content_safety", "favicon", "description")
AUTO_APPROVE_TOOL_NAME_CONFIDENCE = 60
CATALOG_AUTO_PUBLISH_POLICY_VERSION = "catalog-auto-publish-v2"
CATALOG_AUTO_PUBLISH_MAX_LIMIT = 100
REVIEW_TOOL_NAME_CONFIDENCE = 40
ASSET_DEAD_LETTER_REVIVE_HOURS = 24
MAX_HOMEPAGE_HTML_BYTES = 1_200_000
HOMEPAGE_MAX_REDIRECTS = 8
HOMEPAGE_BROWSER_6000_RETRY_DELAY_SECONDS = 6 * 60 * 60
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
D1_API_BASE = "https://api.cloudflare.com/client/v4"

# --- taxonomy defaults ----------------------------------------------------
# Primary taxonomy model via Browser Rendering custom_ai (AI Gateway form).
DEFAULT_TAXONOMY_CLASSIFICATION_MODEL = "deepseek/deepseek-v4-flash"
# Taxonomy L1/leaf decisions must never silently fall back to Workers AI.
DEFAULT_TAXONOMY_CLASSIFICATION_FALLBACK_MODEL = ""

# --- domain parsing -------------------------------------------------------
COMMON_THREE_LABEL_SUFFIXES = {
    "co.uk",
    "com.au",
    "co.jp",
    "co.in",
    "co.nz",
    "co.kr",
    "com.br",
    "com.cn",
    "com.sg",
    "com.hk",
    "co.za",
    "com.mx",
}
