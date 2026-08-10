"""Deterministic entailment validation for Level 1 pricing claims."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .dom import PricingDomMap, normalize_text
from .normalize import NormalizationResult
from .raw_claims import RawPricingClaim
from .regions import PricingRegion


VALIDATOR_VERSION = "pricing-validator-v1"
_PRICE_RE = re.compile(
    r"(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR|US\$|CA\$|AU\$|[$€£¥₹])\s*\d|"
    r"\d(?:[\d,.]*\d)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR)\b",
    re.I,
)
_PRICE_RELATION_RE = re.compile(
    r"/\s*(?:month|mo|year|yr)\b|\bper\s+(?:month|mo|year|yr|request|call|quer(?:y|ies)|token|"
    r"seat|user|member|credit|point|minute|second|hour|image|video|generation|character|word|gb)\b|"
    r"\b(?:monthly|yearly|one[- ]time|lifetime|price(?:d|s)?|costs?)\b|"
    r"\b(?:starts?|starting)\s+(?:from|at)\b|\bplans?\s+from\b",
    re.I,
)
_FREE_PLAN_RE = re.compile(
    r"\bfree\s+(?:plan|tier|forever)\b|(?<!\w)[$€£¥₹]?\s*0\s*/\s*(?:month|year)\b",
    re.I,
)
_TRIAL_RE = re.compile(
    r"\bfree\s+trial\b|\b\d+\s*[- ]?(?:day|week|month)s?\s+free\s+trial\b",
    re.I,
)
_CUSTOM_RE = re.compile(
    r"\bcontact\s+(?:us|sales)\b|\bcustom\s+(?:pricing|quote)\b|"
    r"\brequest\s+(?:a\s+)?quote\b",
    re.I,
)
_USAGE_RE = re.compile(
    r"(?:/|\bper\s+)(?:request|call|quer(?:y|ies)|token|credit|point|minute|second|hour|image|video|"
    r"generation|character|word|gb)s?\b",
    re.I,
)
_CURRENCY_CODE_RE = re.compile(r"\b(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR)\b", re.I)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: str
    confidence: int
    reason: str
    missing_fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    semantic_required: bool = False
    version: str = VALIDATOR_VERSION


def _unsupported(reason: str, *missing_fields: str) -> ValidationResult:
    return ValidationResult("unsupported", 0, reason, tuple(missing_fields))


def validate_raw_claim(
    claim: RawPricingClaim,
    normalization: NormalizationResult,
    dom_map: PricingDomMap,
    region: PricingRegion,
) -> ValidationResult:
    if not claim.evidence:
        return _unsupported("claim has no evidence", "evidence")
    by_id = {node.node_id: node for node in dom_map.nodes}
    region_ids = frozenset(region.node_ids)
    evidence_quotes: list[str] = []
    for evidence in claim.evidence:
        node = by_id.get(evidence.node_id)
        if node is None:
            return _unsupported("evidence node is missing from DOM map", "node_id")
        if evidence.node_id not in region_ids or evidence.container_node_id not in region.root_node_ids:
            return _unsupported("evidence is outside the selected pricing region", "container_node_id")
        quote = normalize_text(evidence.quote)
        if quote and quote.casefold() not in normalize_text(node.text).casefold():
            return _unsupported("evidence quote cannot be located in its DOM node", "quote")
        evidence_quotes.append(quote)
    combined = " ".join(evidence_quotes)

    currency_codes = {code.upper() for code in _CURRENCY_CODE_RE.findall(combined)}
    if claim.claim_type in {"starting_paid_price", "usage_rate"} and len(currency_codes) > 1:
        return ValidationResult(
            "conflict",
            0,
            "price evidence contains multiple ISO currencies",
            conflicts=("multiple_currencies",),
        )

    raw_value = claim.raw_value
    if claim.claim_type == "has_free_plan":
        entailed = raw_value is True and bool(_FREE_PLAN_RE.search(combined))
    elif claim.claim_type == "has_free_trial":
        unit_pattern = r"quer(?:y|ies)" if unit_raw == "query" else rf"{re.escape(unit_raw)}s?"
        entailed = (
            isinstance(raw_value, dict)
            and raw_value.get("available") is True
            and bool(_TRIAL_RE.search(combined))
        )
    elif claim.claim_type == "card_required":
        no_card = bool(re.search(r"\bno\s+(?:credit|payment)\s+card\s+required\b", combined, re.I))
        yes_card = bool(re.search(r"\b(?:credit|payment)\s+card\s+required\b", combined, re.I))
        entailed = (raw_value is False and no_card) or (raw_value is True and yes_card and not no_card)
    elif claim.claim_type == "has_custom_quote":
        entailed = raw_value is True and bool(_CUSTOM_RE.search(combined))
    elif claim.claim_type == "has_paid_pricing":
        entailed = (
            raw_value is True
            and bool(_PRICE_RE.search(combined))
            and bool(_PRICE_RELATION_RE.search(combined))
        )
    elif claim.claim_type == "has_usage_pricing":
        entailed = raw_value is True and bool(_PRICE_RE.search(combined)) and bool(_USAGE_RE.search(combined))
    elif claim.claim_type == "starting_paid_price":
        price_text = str(raw_value.get("price_text") or "") if isinstance(raw_value, dict) else ""
        entailed = (
            bool(price_text)
            and price_text.casefold() in combined.casefold()
            and bool(_PRICE_RELATION_RE.search(combined))
        )
    elif claim.claim_type == "starting_price_period":
        entailed = str(raw_value).casefold() in combined.casefold()
    elif claim.claim_type == "usage_rate":
        price_text = str(raw_value.get("price_text") or "") if isinstance(raw_value, dict) else ""
        unit_raw = str(raw_value.get("unit_raw") or "") if isinstance(raw_value, dict) else ""
        entailed = (
            bool(price_text)
            and price_text.casefold() in combined.casefold()
            and bool(unit_raw)
            and bool(re.search(rf"\b{unit_pattern}\b", combined, re.I))
            and bool(_USAGE_RE.search(combined))
        )
    elif claim.claim_type == "free_allowance":
        quantity = str(raw_value.get("quantity_raw") or "") if isinstance(raw_value, dict) else ""
        unit = str(raw_value.get("unit_raw") or "") if isinstance(raw_value, dict) else ""
        entailed = (
            bool(quantity)
            and quantity.casefold() in combined.casefold()
            and bool(unit)
            and unit.casefold() in combined.casefold()
            and bool(re.search(r"\b(?:free|included|at no cost)\b", combined, re.I))
        )
    elif claim.claim_type == "pricing_models":
        labels = set(raw_value) if isinstance(raw_value, list) else set()
        checks = {
            "subscription": bool(
                re.search(
                    r"/\s*(?:month|year)|\b(?:monthly|yearly)\b|\bper\s+(?:month|year)\b",
                    combined,
                    re.I,
                )
            ),
            "per_seat": bool(re.search(r"(?:/|\bper\s+)(?:seat|user|member)s?\b", combined, re.I)),
            "usage_based": bool(_USAGE_RE.search(combined)),
            "credit_based": bool(re.search(r"(?:/|\bper\s+)credits?\b", combined, re.I)),
            "one_time": bool(re.search(r"\b(?:one[- ]time|lifetime)\b", combined, re.I)),
            "custom_quote": bool(_CUSTOM_RE.search(combined)),
            "hybrid": len(labels - {"hybrid"}) > 1,
        }
        entailed = bool(labels) and all(checks.get(label, False) for label in labels)
    else:
        return _unsupported("validator does not support claim type", "claim_type")

    if not entailed:
        return _unsupported("raw claim is not entailed by its evidence", "entailed_evidence")
    confidence = 98 if normalization.status in {"normalized", "not_applicable"} else 92
    return ValidationResult("entailed", confidence, "deterministic evidence rules passed")
