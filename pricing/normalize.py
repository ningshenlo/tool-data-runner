"""Deterministic normalization for evidence-bound pricing claims."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .raw_claims import RawPricingClaim


NORMALIZER_VERSION = "pricing-normalizer-v1"
_ISO_CURRENCIES = frozenset({"USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CNY", "INR"})
_SYMBOL_CURRENCIES = {
    "US$": "USD",
    "CA$": "CAD",
    "AU$": "AUD",
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
}
_AMBIGUOUS_SYMBOLS = frozenset({"$", "¥"})
_CURRENCY_EXPONENT = {"JPY": 0}
_PERIOD_ALIASES = {
    "mo": "month",
    "month": "month",
    "monthly": "month",
    "yr": "year",
    "year": "year",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "day": "day",
    "daily": "day",
    "week": "week",
    "weekly": "week",
}
_UNIT_ALIASES = {
    "request": "request",
    "requests": "request",
    "call": "call",
    "calls": "call",
    "query": "query",
    "queries": "query",
    "token": "token",
    "tokens": "token",
    "character": "character",
    "characters": "character",
    "word": "word",
    "words": "word",
    "seat": "seat",
    "seats": "seat",
    "user": "user",
    "users": "user",
    "member": "member",
    "members": "member",
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
    "credit": "credit",
    "credits": "credit",
    "point": "point",
    "points": "point",
    "gb": "gigabyte",
    "gigabyte": "gigabyte",
    "gigabytes": "gigabyte",
}
_UNIT_FAMILIES = {
    "request": "operation",
    "call": "operation",
    "query": "operation",
    "token": "text",
    "character": "text",
    "word": "text",
    "seat": "seat",
    "user": "seat",
    "member": "seat",
    "minute": "time",
    "second": "time",
    "hour": "time",
    "image": "generation",
    "video": "generation",
    "generation": "generation",
    "credit": "credit",
    "point": "credit",
    "gigabyte": "storage",
}
_MODEL_LABELS = frozenset(
    {"subscription", "per_seat", "usage_based", "credit_based", "one_time", "hybrid", "custom_quote"}
)
_QUANTITY_RE = re.compile(
    r"^\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>k|m|thousand|million)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    status: str
    normalized_value: Any | None
    confidence: int
    completeness_score: int
    errors: tuple[str, ...] = ()
    version: str = NORMALIZER_VERSION


def _decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def normalize_quantity(raw: str) -> Decimal | None:
    match = _QUANTITY_RE.match(str(raw))
    if not match:
        return None
    try:
        value = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    suffix = (match.group("suffix") or "").lower()
    if suffix in {"k", "thousand"}:
        value *= 1_000
    elif suffix in {"m", "million"}:
        value *= 1_000_000
    return value


def normalize_period(raw: str | None) -> str | None:
    return _PERIOD_ALIASES.get(str(raw or "").strip().lower())


def normalize_unit(raw: str | None) -> tuple[str, str] | None:
    unit = _UNIT_ALIASES.get(str(raw or "").strip().lower())
    if not unit:
        return None
    return unit, _UNIT_FAMILIES[unit]


def _normalize_currency(raw_value: dict[str, Any]) -> tuple[str | None, list[str], int]:
    raw_code = str(raw_value.get("currency_code_raw") or "").upper()
    raw_symbol = str(raw_value.get("currency_symbol_raw") or "")
    if raw_code:
        if raw_code not in _ISO_CURRENCIES:
            return None, ["unsupported_currency_code"], 0
        symbol_currency = _SYMBOL_CURRENCIES.get(raw_symbol)
        if symbol_currency and symbol_currency != raw_code:
            return None, ["currency_code_symbol_conflict"], 0
        return raw_code, [], 100
    if raw_symbol in _SYMBOL_CURRENCIES:
        return _SYMBOL_CURRENCIES[raw_symbol], [], 95
    if raw_symbol in _AMBIGUOUS_SYMBOLS:
        return None, ["ambiguous_currency_symbol"], 0
    return None, ["missing_currency"], 0


def _normalize_price(raw_value: Any, *, require_unit: bool) -> NormalizationResult:
    if not isinstance(raw_value, dict):
        return NormalizationResult("failed", None, 0, 0, ("price_value_not_object",))
    amount = normalize_quantity(str(raw_value.get("amount_raw") or ""))
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    confidence_parts: list[int] = []
    complete_parts = 3 if require_unit else 2
    completed = 0
    if amount is None:
        errors.append("invalid_amount")
    else:
        normalized["amount"] = _decimal_string(amount)
        confidence_parts.append(100)
        completed += 1

    currency, currency_errors, currency_confidence = _normalize_currency(raw_value)
    errors.extend(currency_errors)
    if currency:
        normalized["currency"] = currency
        exponent = _CURRENCY_EXPONENT.get(currency, 2)
        if amount is not None:
            minor_value = amount * (Decimal(10) ** exponent)
            if minor_value == minor_value.to_integral_value():
                normalized["amount_minor"] = int(minor_value)
                normalized["currency_exponent"] = exponent
        confidence_parts.append(currency_confidence)
        completed += 1

    if require_unit:
        unit = normalize_unit(str(raw_value.get("unit_raw") or ""))
        if unit is None:
            errors.append("missing_or_invalid_denominator_unit")
        else:
            normalized["unit"], normalized["unit_family"] = unit
            confidence_parts.append(100)
            completed += 1

    completeness = round(completed * 100 / complete_parts)
    return NormalizationResult(
        "normalized" if not errors else "failed",
        normalized or None,
        min(confidence_parts) if confidence_parts and not errors else 0,
        completeness,
        tuple(errors),
    )


def normalize_raw_claim(claim: RawPricingClaim) -> NormalizationResult:
    claim_type = claim.claim_type
    raw_value = claim.raw_value
    if claim_type in {
        "has_free_plan",
        "card_required",
        "has_paid_pricing",
        "has_usage_pricing",
        "has_custom_quote",
    }:
        if not isinstance(raw_value, bool):
            return NormalizationResult("failed", None, 0, 0, ("expected_boolean",))
        return NormalizationResult("not_applicable", raw_value, 100, 100)

    if claim_type == "has_free_trial":
        if not isinstance(raw_value, dict) or raw_value.get("available") is not True:
            return NormalizationResult("failed", None, 0, 0, ("invalid_free_trial",))
        normalized: dict[str, Any] = {"available": True}
        if raw_value.get("duration_raw") is not None:
            quantity = normalize_quantity(str(raw_value["duration_raw"]))
            period = normalize_period(str(raw_value.get("duration_unit_raw") or ""))
            if quantity is None or period is None:
                return NormalizationResult("failed", normalized, 0, 50, ("invalid_trial_duration",))
            normalized["duration"] = _decimal_string(quantity)
            normalized["duration_unit"] = period
        return NormalizationResult("normalized", normalized, 100, 100)

    if claim_type == "starting_paid_price":
        return _normalize_price(raw_value, require_unit=False)

    if claim_type == "usage_rate":
        return _normalize_price(raw_value, require_unit=True)

    if claim_type == "starting_price_period":
        period = normalize_period(str(raw_value))
        if not period:
            return NormalizationResult("failed", None, 0, 0, ("invalid_billing_period",))
        return NormalizationResult("normalized", period, 100, 100)

    if claim_type == "free_allowance":
        if not isinstance(raw_value, dict):
            return NormalizationResult("failed", None, 0, 0, ("allowance_value_not_object",))
        quantity = normalize_quantity(str(raw_value.get("quantity_raw") or ""))
        unit = normalize_unit(str(raw_value.get("unit_raw") or ""))
        errors: list[str] = []
        normalized: dict[str, Any] = {}
        completed = 0
        if quantity is None:
            errors.append("invalid_allowance_quantity")
        else:
            normalized["quantity"] = _decimal_string(quantity)
            completed += 1
        if unit is None:
            errors.append("invalid_allowance_unit")
        else:
            normalized["unit"], normalized["unit_family"] = unit
            completed += 1
        if raw_value.get("period_raw"):
            period = normalize_period(str(raw_value["period_raw"]))
            if period:
                normalized["period"] = period
            else:
                errors.append("invalid_allowance_period")
        return NormalizationResult(
            "normalized" if not errors else "failed",
            normalized or None,
            100 if not errors else 0,
            round(completed * 100 / 2),
            tuple(errors),
        )

    if claim_type == "pricing_models":
        if not isinstance(raw_value, list) or not raw_value:
            return NormalizationResult("failed", None, 0, 0, ("pricing_models_not_list",))
        labels = sorted({str(label).strip().lower() for label in raw_value})
        invalid = [label for label in labels if label not in _MODEL_LABELS]
        if invalid:
            return NormalizationResult(
                "failed",
                labels,
                0,
                0,
                tuple(f"unsupported_pricing_model:{label}" for label in invalid),
            )
        return NormalizationResult("normalized", labels, 100, 100)

    return NormalizationResult("failed", None, 0, 0, ("unsupported_claim_type",))
