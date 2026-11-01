from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from config.constants import (
    DOMAIN_TRAFFIC_MONTHLY_UPSERT_SQL,
    TRAFFIC_METRICS_SCHEMA_VERSION,
    TRAFFIC_SOURCE,
)
from libs.numbers import (
    country_name,
    estimate_visits_from_bps,
    normalize_country_code,
    parse_country_rank,
    parse_unit_ratio,
    to_integer,
    to_month_start,
    to_number,
)
from libs.timeutil import utc_now_iso

def extract_brightdata_zone(username: str) -> str | None:
    match = re.search(r"(?:^|-)zone-([A-Za-z0-9_]+)", username or "")
    return match.group(1) if match else None

def previous_traffic_month() -> str:
    now = datetime.now(timezone.utc)
    first_this_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    previous = first_this_month - timedelta(days=1)
    return f"{previous.year}-{previous.month:02d}-01"

def parse_traffic_sources(payload: dict[str, Any]) -> dict[str, Any]:
    sources = payload.get("TrafficSources") or {}
    if not isinstance(sources, dict):
        sources = {}

    social_organic = to_number(sources.get("SocialOrganic"))
    social_paid = to_number(sources.get("SocialPaid"))
    search_organic = to_number(sources.get("SearchOrganic"))
    search_paid = to_number(sources.get("SearchPaid"))

    def sum_present(*values: float | None) -> float | None:
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    def prefer_explicit(explicit: float | None, fallback: float | None) -> float | None:
        return explicit if explicit is not None else fallback

    return {
        "social_traffic_share": prefer_explicit(
            to_number(sources.get("Social")),
            sum_present(social_organic, social_paid),
        ),
        "social_organic_traffic_share": social_organic,
        "social_paid_traffic_share": social_paid,
        "paid_referrals_traffic_share": to_number(sources.get("Paid Referrals")),
        "mail_traffic_share": to_number(sources.get("Mail")),
        "search_traffic_share": prefer_explicit(
            to_number(sources.get("Search")),
            sum_present(search_organic, search_paid),
        ),
        "search_organic_traffic_share": search_organic,
        "search_paid_traffic_share": search_paid,
        "direct_traffic_share": to_number(sources.get("Direct")),
        "referrals_traffic_share": to_number(sources.get("Referrals")),
        "display_ads_traffic_share": to_number(sources.get("DisplayAds")),
        "gen_ai_traffic_share": to_number(sources.get("GenAi")),
        "affiliate_traffic_share": to_number(sources.get("Affiliate")),
    }

def parse_top_countries(payload: dict[str, Any]) -> dict[str, Any]:
    countries = payload.get("TopCountryShares") or []
    result: dict[str, Any] = {}
    for index in range(1, 6):
        country = countries[index - 1] if index - 1 < len(countries) else {}
        result[f"top_country_{index}"] = country_name(country.get("CountryCode"))
        result[f"top_country_{index}_traffic_share"] = to_number(country.get("Value"))
    return result

def parse_top_keywords(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_keywords = payload.get("TopKeywords")
    if not isinstance(raw_keywords, list):
        return []

    keywords: list[dict[str, Any]] = []
    for raw_keyword in raw_keywords:
        if not isinstance(raw_keyword, dict):
            continue
        name = str(raw_keyword.get("Name") or "").strip()
        if not name:
            continue
        keywords.append(
            {
                "name": name,
                "volume": to_integer(raw_keyword.get("Volume")),
                "estimated_value": to_number(raw_keyword.get("EstimatedValue")),
                "cpc": to_number(raw_keyword.get("Cpc")),
            }
        )
    return keywords

def parse_ai_traffic(payload: dict[str, Any]) -> dict[str, Any] | None:
    ai_details = payload.get("AiTrafficDetails")
    if not isinstance(ai_details, dict):
        return None

    traffic = ai_details.get("Traffic")
    traffic = traffic if isinstance(traffic, dict) else {}
    distribution = traffic.get("Distribution")
    distribution = distribution if isinstance(distribution, dict) else {}

    sources: list[dict[str, Any]] = []
    raw_sources = distribution.get("Chatbots")
    if isinstance(raw_sources, list):
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                continue
            name = str(raw_source.get("Name") or "").strip()
            share = to_number(raw_source.get("Value"))
            if name and share is not None:
                sources.append({"name": name, "share": share})

    history: list[dict[str, Any]] = []
    raw_history = distribution.get("Chart")
    if isinstance(raw_history, list):
        for raw_series in raw_history:
            if not isinstance(raw_series, dict):
                continue
            name = str(raw_series.get("Name") or "").strip()
            points: list[dict[str, Any]] = []
            raw_points = raw_series.get("History")
            if isinstance(raw_points, list):
                for raw_point in raw_points:
                    if not isinstance(raw_point, dict):
                        continue
                    date = to_month_start(raw_point.get("Date"))
                    share = to_number(raw_point.get("Value"))
                    if date and share is not None:
                        points.append({"date": date, "share": share})
            if name and points:
                history.append({"name": name, "points": points})

    rankings: list[dict[str, Any]] = []
    raw_rankings = traffic.get("Split")
    if isinstance(raw_rankings, list):
        for raw_ranking in raw_rankings:
            if not isinstance(raw_ranking, dict):
                continue
            name = str(raw_ranking.get("Name") or "").strip()
            rank = to_integer(raw_ranking.get("Rank"))
            if name:
                rankings.append({"name": name, "rank": rank})

    top_prompts = ai_details.get("TopPrompts")
    top_prompts = top_prompts if isinstance(top_prompts, dict) else {}
    prompts = top_prompts.get("Prompts")
    prompt_count = len(prompts) if isinstance(prompts, list) else 0
    prompt_error = str(top_prompts.get("ErrorMessage") or "").strip() or None

    return {
        "total_visits": to_number(ai_details.get("TotalVisits")),
        "referral_share": to_number(ai_details.get("ReferralTraffic")),
        "boundary": str(distribution.get("Boundary") or "").strip() or None,
        "sources": sources,
        "history": history,
        "rankings": rankings,
        "top_prompts": {
            "status": to_integer(top_prompts.get("Status")),
            "error": prompt_error,
            "count": prompt_count,
        },
    }

def build_traffic_metrics(row: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    website = str(row.get("website") or "").strip()
    if website:
        metrics["website"] = website
    if row.get("query_date"):
        metrics["query_date"] = row["query_date"]
    if row.get("engagement_visits") is not None:
        metrics["engagement_visits"] = row["engagement_visits"]

    traffic_source_fields = {
        "social": "social_traffic_share",
        "social_organic": "social_organic_traffic_share",
        "social_paid": "social_paid_traffic_share",
        "paid_referrals": "paid_referrals_traffic_share",
        "mail": "mail_traffic_share",
        "search": "search_traffic_share",
        "search_organic": "search_organic_traffic_share",
        "search_paid": "search_paid_traffic_share",
        "direct": "direct_traffic_share",
        "referrals": "referrals_traffic_share",
        "display_ads": "display_ads_traffic_share",
        "gen_ai": "gen_ai_traffic_share",
        "affiliate": "affiliate_traffic_share",
    }
    traffic_sources = {
        key: row[field]
        for key, field in traffic_source_fields.items()
        if row.get(field) is not None
    }
    if traffic_sources:
        metrics["traffic_sources"] = traffic_sources

    geographies = []
    for index in range(1, 6):
        country_code = row.get(f"top_country_{index}")
        share = row.get(f"top_country_{index}_traffic_share")
        if country_code and share is not None:
            geographies.append({"country_code": country_code, "share": share})
    if geographies:
        metrics["top_geographies"] = geographies

    raw_keywords = row.get("top_keywords")
    if isinstance(raw_keywords, list) and raw_keywords:
        metrics["top_search_keywords"] = [
            {
                "name": keyword.get("name"),
                "volume": keyword.get("volume"),
                "estimated_traffic": keyword.get("estimated_value"),
                "cpc": keyword.get("cpc"),
            }
            for keyword in raw_keywords
            if isinstance(keyword, dict) and keyword.get("name")
        ]

    ai_traffic = row.get("ai_traffic")
    if isinstance(ai_traffic, dict):
        metrics["ai_traffic"] = ai_traffic

    return metrics

def traffic_share_to_bps(value: Any) -> int | None:
    share = to_number(value)
    if share is None or share < 0 or share > 1.000001:
        return None
    share = min(share, 1.0)
    return min(10000, max(0, int(share * 10000 + 0.5)))

def requested_month_has_traffic_data(rows: list[dict[str, Any]], requested_month: str) -> bool:
    return any(
        row.get("traffic_month") == requested_month
        and (row.get("visits") is not None or row.get("global_rank") is not None)
        for row in rows
    )

def latest_observed_traffic_month(rows: list[dict[str, Any]]) -> str | None:
    months = [str(row.get("traffic_month") or "") for row in rows if row.get("traffic_month")]
    return max(months) if months else None

def build_domain_traffic_monthly_upsert(
    domain: str,
    row: dict[str, Any],
    *,
    captured_at: str | None = None,
    source_snapshot_id: int | None = None,
) -> tuple[str, list[Any]]:
    ai_traffic = row.get("ai_traffic")
    ai_traffic = ai_traffic if isinstance(ai_traffic, dict) else {}
    fetched_at = captured_at or utc_now_iso()
    return (
        DOMAIN_TRAFFIC_MONTHLY_UPSERT_SQL,
        [
            domain,
            TRAFFIC_SOURCE,
            row.get("traffic_month"),
            row.get("visits"),
            row.get("global_rank") or None,
            row.get("country_rank_country"),
            row.get("country_rank") or None,
            row.get("bounce_rate"),
            row.get("pages_per_visit"),
            row.get("avg_visit_duration_seconds"),
            row.get("gen_ai_traffic_share"),
            to_integer(ai_traffic.get("total_visits")),
            to_number(ai_traffic.get("referral_share")),
            json.dumps(build_traffic_metrics(row), ensure_ascii=False),
            TRAFFIC_METRICS_SCHEMA_VERSION,
            source_snapshot_id,
            fetched_at,
        ],
    )

def parse_monthly_rows(payload: dict[str, Any], domain: str, requested_month: str) -> list[dict[str, Any]]:
    engagements = payload.get("Engagments") or {}
    estimated_visits = payload.get("EstimatedMonthlyVisits") or {}
    snapshot_month = to_month_start(payload.get("SnapshotDate"))
    query_date = None
    if to_integer(engagements.get("Year")) and to_integer(engagements.get("Month")):
        query_date = f"{to_integer(engagements.get('Year'))}-{to_integer(engagements.get('Month')):02d}-01"

    ai_traffic = parse_ai_traffic(payload)
    base_fields = {
        "website": payload.get("SiteName") or domain,
        "query_date": query_date,
        "engagement_visits": to_integer(engagements.get("Visits")),
        "global_rank": to_integer((payload.get("GlobalRank") or {}).get("Rank")),
        **parse_country_rank(payload),
        "bounce_rate": parse_unit_ratio(engagements.get("BounceRate")),
        "pages_per_visit": to_number(engagements.get("PagePerVisit")),
        "avg_visit_duration_seconds": to_integer(engagements.get("TimeOnSite")),
        **parse_traffic_sources(payload),
        **parse_top_countries(payload),
    }
    top_keywords = parse_top_keywords(payload)
    if top_keywords:
        base_fields["top_keywords"] = top_keywords
    if ai_traffic is not None:
        base_fields["ai_traffic"] = ai_traffic

    monthly_rows: list[dict[str, Any]] = []
    if isinstance(estimated_visits, dict):
        for month, visits in estimated_visits.items():
            traffic_month = to_month_start(month)
            if traffic_month:
                monthly_rows.append({"traffic_month": traffic_month, "visits": to_integer(visits)})

    if not monthly_rows:
        traffic_month = snapshot_month or requested_month
        monthly_rows.append({"traffic_month": traffic_month, "visits": to_integer(engagements.get("Visits"))})

    monthly_rows.sort(key=lambda row: row["traffic_month"])
    latest_month = monthly_rows[-1]["traffic_month"]
    for row in monthly_rows:
        if row["traffic_month"] == latest_month:
            row.update(base_fields)
        else:
            row.update({"website": domain, "query_date": None})
    return monthly_rows

