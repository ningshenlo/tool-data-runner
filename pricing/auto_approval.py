"""Conservative policy for publishing legacy pricing extractions automatically.

Only simple, directly evidenced public package prices are eligible. Complex
billing semantics remain review-only even when extraction is structurally valid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


STRICT_AUTO_APPROVAL_POLICY_VERSION = "pricing-strict-auto-publish-v2"
_GENERIC_PLAN_RE = re.compile(r"^plan\s+\d+$", re.I)
_CUSTOM_QUOTE_RE = re.compile(
    r"\b(?:contact|talk\s+to)\s+sales\b|\bcustom\s+(?:price|pricing|quote)\b|\bbook\s+(?:a\s+)?demo\b",
    re.I,
)
_FREE_PLAN_RE = re.compile(r"\bfree(?:\s+(?:plan|tier|forever))?\b|(?:^|\D)0(?:\.0+)?(?:\D|$)", re.I)
_HIGH_RISK_PRICE_RE = re.compile(
    r"\b(?:discount|coupon|save|saving|introductory|limited[- ]time|special offer|"
    r"billed annually|annual commitment|per user|per seat|each user|each seat|"
    r"volume discount|minimum spend)\b|\b\d+(?:\.\d+)?\s*%\s*off\b",
    re.I,
)
_USAGE_UNIT_PATTERN = (
    r"(?:requests?|api\s+calls?|queries|tokens?|credits?|points?|minutes?|seconds?|hours?|"
    r"images?|videos?|generations?|characters?|words?|gb|gigabytes?)"
)
_PRICE_TOKEN_PATTERN = (
    r"(?:(?:USD|EUR|GBP|INR|US\$)\s*\d[\d,]*(?:\.\d+)?|"
    r"[$€£₹]\s*\d[\d,]*(?:\.\d+)?|"
    r"\d[\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|INR))"
)
_METERED_USAGE_CHARGE_RE = re.compile(
    rf"(?:{_PRICE_TOKEN_PATTERN})\s*(?:/|\bper\b|\bfor\b)\s*"
    rf"(?:\d[\d,]*(?:\.\d+)?\s*)?{_USAGE_UNIT_PATTERN}\b|"
    rf"\b{_USAGE_UNIT_PATTERN}\s*(?:costs?|at|for)\s*(?:{_PRICE_TOKEN_PATTERN})|"
    rf"\b(?:additional|extra|overage)\s+{_USAGE_UNIT_PATTERN}\b.{{0,40}}(?:{_PRICE_TOKEN_PATTERN})|"
    r"\b(?:pay[- ]as[- ]you[- ]go|usage[- ]based|metered billing|overage charge|additional usage charge)\b",
    re.I,
)
_FIXED_ALLOWANCE_RE = re.compile(
    rf"\b(?:includes?|with|comes with|up to)\s+\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)?\s+"
    rf"{_USAGE_UNIT_PATTERN}\b|"
    rf"\b\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)?\s+{_USAGE_UNIT_PATTERN}\s*"
    r"(?:included|allowance|credits?\/month|tokens?\/month|(?:per|/)\s*(?:month|mo|year|yr))\b",
    re.I,
)
_EXPLICIT_CURRENCY_RE = {
    "USD": re.compile(r"\bUSD\b|US\$", re.I),
    "EUR": re.compile(r"\bEUR\b|€", re.I),
    "GBP": re.compile(r"\bGBP\b|£", re.I),
    "INR": re.compile(r"\bINR\b|₹", re.I),
}


@dataclass(frozen=True)
class StrictAutoApprovalDecision:
    eligible: bool
    reasons: tuple[str, ...]
    policy_version: str = STRICT_AUTO_APPROVAL_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "policy_version": self.policy_version,
            "reasons": list(self.reasons),
        }


def _decimal_key(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return format(amount.normalize(), "f")


def pricing_fact_signature(payload: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Return only public price facts used for extractor agreement."""

    facts: list[tuple[Any, ...]] = []
    plans = payload.get("plans") if isinstance(payload, dict) else []
    for plan in plans if isinstance(plans, list) else []:
        if not isinstance(plan, dict):
            facts.append(("invalid_plan",))
            continue
        prices = plan.get("prices") if isinstance(plan.get("prices"), list) else []
        if not prices:
            facts.append(("missing_price",))
            continue
        price = prices[0] if isinstance(prices[0], dict) else {}
        custom_quote = bool(price.get("custom_quote")) or price.get("kind") == "custom_quote"
        facts.append(
            (
                "custom_quote" if custom_quote else str(price.get("kind") or "recurring"),
                None if custom_quote else _decimal_key(price.get("amount")),
                None if custom_quote else str(price.get("currency") or "").upper(),
                str(price.get("billing_interval") or ""),
                str(price.get("commitment_interval") or ""),
                str(price.get("unit") or "").lower(),
            )
        )
    return tuple(sorted(facts, key=repr))


def pricing_payloads_agree(rule_payload: dict[str, Any], model_payload: dict[str, Any]) -> bool:
    rule_signature = pricing_fact_signature(rule_payload)
    model_signature = pricing_fact_signature(model_payload)
    return bool(rule_signature) and rule_signature == model_signature


def has_metered_usage_charge(value: str) -> bool:
    """Return true only when usage controls the amount charged, not a bundled allowance."""

    return bool(_METERED_USAGE_CHARGE_RE.search(value or ""))


def has_fixed_ai_allowance(value: str) -> bool:
    """Detect common AI-plan allowances such as included credits or monthly tokens."""

    return bool(_FIXED_ALLOWANCE_RE.search(value or ""))


def evaluate_strict_auto_approval(
    payload: dict[str, Any],
    *,
    review_status: str,
    confidence: int,
    validation_errors: list[str],
    http_status: int,
    page_status: str,
    strict_source_context: bool,
    model_used: bool,
    rule_model_agreement: bool,
    min_confidence: int = 82,
) -> StrictAutoApprovalDecision:
    reasons: list[str] = []

    if review_status != "approved":
        reasons.append("extractor_not_approved")
    if confidence < min_confidence:
        reasons.append("confidence_below_threshold")
    if validation_errors:
        reasons.append("validation_errors_present")
    if http_status != 200:
        reasons.append("http_status_not_200")
    if page_status != "found":
        reasons.append("pricing_page_not_found")
    if not strict_source_context:
        reasons.append("untrusted_pricing_url_context")
    if model_used and not rule_model_agreement:
        reasons.append("rule_model_price_facts_disagree")

    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    try:
        text_score = int(quality.get("text_score") or 0)
    except (TypeError, ValueError):
        text_score = 0
    if text_score < 18:
        reasons.append("low_pricing_text_quality")

    plans = payload.get("plans") if isinstance(payload.get("plans"), list) else []
    if not 1 <= len(plans) <= 6:
        reasons.append("invalid_plan_count")

    seen_names: set[str] = set()
    currencies: set[str] = set()
    for index, plan in enumerate(plans):
        if not isinstance(plan, dict):
            reasons.append(f"plan_{index}_invalid")
            continue
        name = re.sub(r"\s+", " ", str(plan.get("name") or "")).strip()
        name_key = name.casefold()
        if not name or _GENERIC_PLAN_RE.fullmatch(name):
            reasons.append(f"plan_{index}_generic_name")
        if name_key in seen_names:
            reasons.append("duplicate_plan_names")
        seen_names.add(name_key)

        prices = plan.get("prices") if isinstance(plan.get("prices"), list) else []
        if len(prices) != 1 or not isinstance(prices[0], dict):
            reasons.append(f"plan_{index}_requires_exactly_one_price")
            continue
        price = prices[0]
        display_text = re.sub(r"\s+", " ", str(price.get("display_text") or "")).strip()
        custom_quote = bool(price.get("custom_quote")) or price.get("kind") == "custom_quote"
        if custom_quote:
            if price.get("amount") not in (None, "") or price.get("currency") not in (None, ""):
                reasons.append(f"plan_{index}_custom_quote_has_amount")
            if not _CUSTOM_QUOTE_RE.search(display_text):
                reasons.append(f"plan_{index}_custom_quote_not_evidenced")
            continue

        amount = _decimal_key(price.get("amount"))
        currency = str(price.get("currency") or "").upper()
        interval = str(price.get("billing_interval") or "")
        kind = str(price.get("kind") or "recurring")
        if amount is None:
            reasons.append(f"plan_{index}_invalid_amount")
        if currency not in _EXPLICIT_CURRENCY_RE:
            reasons.append(f"plan_{index}_unsupported_currency")
        else:
            currencies.add(currency)

        is_explicit_free = amount == "0" and (
            name_key == "free" or bool(_FREE_PLAN_RE.search(display_text))
        )
        if not is_explicit_free and (
            currency not in _EXPLICIT_CURRENCY_RE
            or not _EXPLICIT_CURRENCY_RE[currency].search(display_text)
        ):
            reasons.append(f"plan_{index}_currency_not_explicit")

        if price.get("starting_at"):
            reasons.append(f"plan_{index}_starting_price")
        if price.get("unit"):
            reasons.append(f"plan_{index}_unit_pricing")
        if price.get("commitment_interval"):
            reasons.append(f"plan_{index}_commitment_pricing")
        if kind == "usage" or interval == "usage":
            reasons.append(f"plan_{index}_usage_pricing")
        elif kind == "recurring" and amount not in (None, "0") and interval not in {"monthly", "yearly"}:
            reasons.append(f"plan_{index}_missing_recurring_interval")
        elif kind == "one_time" and interval not in {"one_time", ""}:
            reasons.append(f"plan_{index}_invalid_one_time_interval")
        if _HIGH_RISK_PRICE_RE.search(display_text):
            reasons.append(f"plan_{index}_high_risk_billing_language")
        if has_metered_usage_charge(display_text):
            reasons.append(f"plan_{index}_metered_usage_charge")
        features = plan.get("features") if isinstance(plan.get("features"), list) else []
        if model_used and features:
            evidence = quality.get("feature_evidence") if isinstance(quality.get("feature_evidence"), dict) else {}
            if evidence.get("verified") is not True:
                reasons.append(f"plan_{index}_feature_evidence_unverified")
        if any(has_metered_usage_charge(str(feature)) for feature in features):
            reasons.append(f"plan_{index}_metered_usage_feature")

    if len(currencies) > 1:
        reasons.append("multiple_currencies")

    unique_reasons = tuple(sorted(set(reasons)))
    return StrictAutoApprovalDecision(not unique_reasons, unique_reasons)
