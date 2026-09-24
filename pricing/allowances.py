"""Evidence-bound normalization for AI plan allowances and plan features."""

from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .auto_approval import has_metered_usage_charge


_UNIT_PATTERN = (
    r"api\s+calls?|requests?|queries|tokens?|credits?|points?|minutes?|seconds?|hours?|"
    r"images?|videos?|generations?|characters?|words?|gb|gigabytes?"
)
_ALLOWANCE_RE = re.compile(
    rf"(?<![$€£₹\w])"
    rf"(?P<prefix>(?:(?:includes?|with|comes\s+with|up\s+to)\s+)?)"
    rf"(?P<quantity>unlimited|\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)?)\s+"
    rf"(?P<unit>{_UNIT_PATTERN})"
    r"(?P<period>\s*(?:(?:per|/)\s*(?:day|month|mo|year|yr)|daily|monthly|yearly))?"
    r"(?P<suffix>\s*(?:included|allowance))?",
    re.I,
)
_PERIOD_RE = re.compile(r"(?:per|/)\s*(day|month|mo|year|yr)|\b(daily|monthly|yearly)\b", re.I)
_UNIT_ALIASES = {
    "api call": "api_call",
    "api calls": "api_call",
    "request": "request",
    "requests": "request",
    "query": "query",
    "queries": "query",
    "token": "token",
    "tokens": "token",
    "credit": "credit",
    "credits": "credit",
    "point": "credit",
    "points": "credit",
    "minute": "minute",
    "minutes": "minute",
    "second": "second",
    "seconds": "second",
    "hour": "hour",
    "hours": "hour",
    "image": "image",
    "images": "image",
    "video": "video",
    "videos": "video",
    "generation": "generation",
    "generations": "generation",
    "character": "character",
    "characters": "character",
    "word": "word",
    "words": "word",
    "gb": "gb",
    "gigabyte": "gb",
    "gigabytes": "gb",
}
_PERIOD_ALIASES = {
    "day": "day",
    "daily": "day",
    "month": "month",
    "mo": "month",
    "monthly": "month",
    "year": "year",
    "yr": "year",
    "yearly": "year",
}


def normalize_evidence_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _decimal_text(value: str) -> str | None:
    raw = value.lower().replace(",", "").strip()
    scale = Decimal("1")
    for suffix, multiplier in (
        ("thousand", Decimal("1000")),
        ("million", Decimal("1000000")),
        ("k", Decimal("1000")),
        ("m", Decimal("1000000")),
    ):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
            scale = multiplier
            break
    try:
        number = Decimal(raw) * scale
    except (InvalidOperation, ValueError):
        return None
    if number < 0:
        return None
    return format(number.normalize(), "f")


def _period(value: str) -> str | None:
    match = _PERIOD_RE.search(value or "")
    if not match:
        return None
    return _PERIOD_ALIASES.get((match.group(1) or match.group(2) or "").lower())


def _raw_feature(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("raw_name") or value.get("name") or value.get("evidence_quote") or ""
    return normalize_evidence_text(str(value or ""))[:240]


def extract_fixed_allowance_quotes(value: str, limit: int = 8) -> list[str]:
    """Extract literal bundled allowance phrases from a price-local text section."""

    quotes: list[str] = []
    seen: set[str] = set()
    for match in _ALLOWANCE_RE.finditer(normalize_evidence_text(value)):
        quote = normalize_evidence_text(match.group(0))
        nearby = normalize_evidence_text(value[max(0, match.start() - 45):match.end() + 45])
        if has_metered_usage_charge(nearby):
            continue
        key = quote.casefold()
        if key in seen:
            continue
        seen.add(key)
        quotes.append(quote)
        if len(quotes) >= limit:
            break
    return quotes


def normalize_plan_feature(value: Any) -> dict[str, Any] | None:
    raw_name = _raw_feature(value)
    if not raw_name:
        return None

    if has_metered_usage_charge(raw_name):
        return {
            "feature_group": "usage_charge",
            "raw_name": raw_name,
            "normalized_key": "usage_charge",
            "state": "limited",
            "value_type": "text",
            "value_number": None,
            "value_text": raw_name,
            "unit": None,
            "period": None,
            "scope": "plan",
            "evidence_quote": raw_name,
        }

    match = _ALLOWANCE_RE.search(raw_name)
    if match:
        quantity_raw = match.group("quantity").strip()
        unit = _UNIT_ALIASES.get(re.sub(r"\s+", " ", match.group("unit").lower()))
        unlimited = quantity_raw.lower() == "unlimited"
        value_number = None if unlimited else _decimal_text(quantity_raw)
        if unit and (unlimited or value_number is not None):
            return {
                "feature_group": "allowance",
                "raw_name": raw_name,
                "normalized_key": f"allowance.{unit}",
                "state": "included" if unlimited else "limited",
                "value_type": "text" if unlimited else "number",
                "value_number": value_number,
                "value_text": "unlimited" if unlimited else None,
                "unit": unit,
                "period": _period(match.group("period") or ""),
                "scope": "plan",
                "evidence_quote": raw_name,
            }

    slug = re.sub(r"[^a-z0-9]+", "_", raw_name.casefold()).strip("_")[:100] or "feature"
    return {
        "feature_group": "feature",
        "raw_name": raw_name,
        "normalized_key": f"feature.{slug}",
        "state": "included",
        "value_type": "text",
        "value_number": None,
        "value_text": raw_name,
        "unit": None,
        "period": None,
        "scope": "plan",
        "evidence_quote": raw_name,
    }


def retain_evidenced_plan_features(
    plans: list[dict[str, Any]],
    source_text: str,
    *,
    max_plan_distance: int = 1600,
) -> dict[str, int | bool]:
    """Keep only literal model features associated with their nearest preceding plan."""

    source = normalize_evidence_text(source_text).casefold()
    plan_names = [normalize_evidence_text(str(plan.get("name") or "")).casefold() for plan in plans]
    plan_occurrences: list[tuple[int, int]] = []
    for plan_index, name in enumerate(plan_names):
        if not name:
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", source):
            plan_occurrences.append((match.start(), plan_index))
    plan_occurrences.sort()

    total = 0
    kept = 0
    for plan_index, plan in enumerate(plans):
        features = plan.get("features") if isinstance(plan.get("features"), list) else []
        retained: list[str] = []
        for raw_feature in features:
            total += 1
            feature = _raw_feature(raw_feature)
            if not feature:
                continue
            feature_key = feature.casefold()
            search_start = 0
            evidenced = False
            while (feature_position := source.find(feature_key, search_start)) >= 0:
                preceding = [item for item in plan_occurrences if item[0] <= feature_position]
                if len(plans) == 1:
                    evidenced = True
                elif preceding:
                    plan_position, nearest_plan_index = preceding[-1]
                    evidenced = (
                        nearest_plan_index == plan_index
                        and feature_position - plan_position <= max_plan_distance
                    )
                if evidenced:
                    break
                search_start = feature_position + max(1, len(feature_key))
            if evidenced and feature.casefold() not in {item.casefold() for item in retained}:
                retained.append(feature)
                kept += 1
        plan["features"] = retained

    return {
        "verified": True,
        "total": total,
        "kept": kept,
        "dropped": total - kept,
    }


def merge_evidenced_rule_features(
    rule_payload: dict[str, Any],
    model_payload: dict[str, Any],
) -> int:
    """Preserve deterministic price-local features after a model price-fact check."""

    rule_plans = rule_payload.get("plans") if isinstance(rule_payload.get("plans"), list) else []
    model_plans = model_payload.get("plans") if isinstance(model_payload.get("plans"), list) else []

    def price_key(plan: dict[str, Any]) -> tuple[str, str, str, str]:
        prices = plan.get("prices") if isinstance(plan.get("prices"), list) else []
        price = prices[0] if prices and isinstance(prices[0], dict) else {}
        return (
            str(price.get("amount") or ""),
            str(price.get("currency") or "").upper(),
            str(price.get("billing_interval") or ""),
            str(price.get("kind") or "recurring"),
        )

    merged = 0
    for rule_plan in rule_plans:
        if not isinstance(rule_plan, dict):
            continue
        rule_features = rule_plan.get("features") if isinstance(rule_plan.get("features"), list) else []
        if not rule_features:
            continue
        rule_name = normalize_evidence_text(str(rule_plan.get("name") or "")).casefold()
        named = [
            plan
            for plan in model_plans
            if isinstance(plan, dict)
            and normalize_evidence_text(str(plan.get("name") or "")).casefold() == rule_name
        ]
        candidates = named or [
            plan
            for plan in model_plans
            if isinstance(plan, dict) and price_key(plan) == price_key(rule_plan)
        ]
        if len(candidates) != 1:
            continue
        target = candidates[0]
        target_features = target.get("features") if isinstance(target.get("features"), list) else []
        target_features = [_raw_feature(feature) for feature in target_features if _raw_feature(feature)]
        seen = {feature.casefold() for feature in target_features}
        for rule_feature in rule_features:
            feature = _raw_feature(rule_feature)
            if not feature or feature.casefold() in seen:
                continue
            target_features.append(feature)
            seen.add(feature.casefold())
            merged += 1
        target["features"] = target_features

    if merged:
        quality = model_payload.get("quality") if isinstance(model_payload.get("quality"), dict) else {}
        evidence = quality.get("feature_evidence") if isinstance(quality.get("feature_evidence"), dict) else {}
        evidence["verified"] = True
        evidence["total"] = int(evidence.get("total") or 0) + merged
        evidence["kept"] = int(evidence.get("kept") or 0) + merged
        evidence["dropped"] = int(evidence.get("dropped") or 0)
        evidence["rule_merged"] = int(evidence.get("rule_merged") or 0) + merged
        quality["feature_evidence"] = evidence
        model_payload["quality"] = quality
    return merged
