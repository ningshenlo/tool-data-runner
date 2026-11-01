"""Numeric / month / country parsing helpers."""

from __future__ import annotations

import math
import re
from typing import Any


def to_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def to_integer(value: Any) -> int | None:
    parsed = to_number(value)
    if parsed is None:
        return None
    return max(0, int(parsed))


def to_month_start(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    match = re.match(r"^(\d{4})-(\d{2})", text)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-01"


def country_name(country_code: Any) -> str | None:
    if not country_code:
        return None
    return str(country_code).upper()


def parse_country_rank(payload: dict[str, Any]) -> dict[str, Any]:
    country_rank = payload.get("CountryRank") or {}
    country_code = country_rank.get("CountryCode")
    rank = to_integer(country_rank.get("Rank"))
    return {
        "country_rank_country": country_code,
        "country_rank": rank,
        "country_rank_text": f"{country_code} #{rank}" if country_code and rank is not None else None,
    }


def normalize_country_code(value: Any) -> str | None:
    country_code = str(value or "").strip().upper()
    return country_code if re.fullmatch(r"[A-Z]{2}", country_code) else None


def estimate_visits_from_bps(visits: Any, traffic_share_bps: int) -> int | None:
    normalized_visits = to_integer(visits)
    if normalized_visits is None:
        return None
    return (normalized_visits * traffic_share_bps + 5000) // 10000


def parse_unit_ratio(value: Any) -> float | None:
    ratio = to_number(value)
    if ratio is None or not math.isfinite(ratio) or ratio < -1e-12 or ratio > 1 + 1e-12:
        return None
    return min(1.0, max(0.0, ratio))
