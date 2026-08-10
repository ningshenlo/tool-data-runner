"""Conservative Level 1 pricing facts bound to DOM evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .dom import DomNode, PricingDomMap, normalize_text
from .regions import PricingRegion


RAW_CLAIM_EXTRACTOR_VERSION = "pricing-raw-claims-v1"
_PRICE_RE = re.compile(
    r"(?:(?P<currency_code_before>USD|EUR|GBP|CAD|AUD|JPY|CNY|INR)\s*"
    r"(?P<symbol_before>US\$|CA\$|AU\$|[$€£¥₹])?\s*(?P<amount_before>\d[\d,]*(?:\.\d{1,4})?)|"
    r"(?P<symbol_only>US\$|CA\$|AU\$|[$€£¥₹])\s*(?P<amount_symbol>\d[\d,]*(?:\.\d{1,4})?)|"
    r"(?P<amount_after>\d[\d,]*(?:\.\d{1,4})?)\s*(?P<currency_code_after>USD|EUR|GBP|CAD|AUD|JPY|CNY|INR)\b)",
    re.IGNORECASE,
)
_FREE_PLAN_RE = re.compile(
    r"\b(?:free\s+(?:plan|tier|forever)|(?:plan|tier)\s+(?:is\s+)?free|free\s*[$€£¥₹]?\s*0)\b|"
    r"(?<!\w)[$€£¥₹]?\s*0\s*(?:/|per\s+)(?:month|mo|year|yr)\b",
    re.IGNORECASE,
)
_CUSTOM_QUOTE_RE = re.compile(
    r"\b(?:contact\s+(?:us|sales)|talk\s+to\s+sales|custom\s+(?:pricing|quote)|request\s+(?:a\s+)?quote)\b",
    re.IGNORECASE,
)
_DENOMINATOR_UNIT_RE = re.compile(
    r"(?:/|\bper\s+)(request|call|query|queries|token|seat|user|member|credit|point|minute|second|"
    r"hour|image|video|generation|character|word|gb|gigabyte)s?\b",
    re.IGNORECASE,
)
_METERED_UNITS = frozenset(
    {
        "request",
        "call",
        "query",
        "token",
        "credit",
        "point",
        "minute",
        "second",
        "hour",
        "image",
        "video",
        "generation",
        "character",
        "word",
        "gb",
        "gigabyte",
    }
)
_SEAT_UNITS = frozenset({"seat", "user", "member"})
_BILLING_PERIOD_RE = re.compile(
    r"(?:/\s*|\bper\s+)?\b(month|mo|year|yr)\b|\b(monthly|yearly)\b",
    re.I,
)
_PRICE_RELATION_RE = re.compile(
    r"(?:/\s*(?:month|mo|year|yr)\b|\bper\s+(?:month|mo|year|yr)\b|"
    r"\b(?:monthly|yearly|one[- ]time|lifetime)\b|"
    r"\b(?:starts?|starting)\s+(?:from|at)\b|\b(?:price(?:d|s)?|costs?)\b|"
    r"\bplans?\s+from\b)",
    re.IGNORECASE,
)
_FREE_ALLOWANCE_RE = re.compile(
    r"(?P<quantity>\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)?)\s*"
    r"(?P<unit>requests?|calls?|queries|tokens?|credits?|points?|minutes?|seconds?|hours?|"
    r"images?|videos?|generations?|characters?|words?|seats?|users?|members?|gb|gigabytes?)\s*"
    r"(?:(?:per|/)\s*(?P<period>month|mo|year|yr|day))?"
    r"\s*(?:free|included|at no cost)\b",
    re.IGNORECASE,
)
_FREE_TRIAL_RE = re.compile(
    r"\b(?:(?P<duration>\d+)\s*[- ]?(?P<unit>day|week|month)s?\s+)?free\s+trial\b",
    re.IGNORECASE,
)
_CARD_REQUIRED_RE = re.compile(r"\b(?:credit|payment)\s+card\s+required\b", re.IGNORECASE)
_NO_CARD_REQUIRED_RE = re.compile(
    r"\bno\s+(?:credit|payment)\s+card\s+required\b|"
    r"\bwithout\s+(?:a\s+)?(?:credit|payment)\s+card\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    node_id: str
    container_node_id: str
    quote: str
    selector_hint: str
    table_row: int | None
    table_column: int | None
    evidence_hash: str
    evidence_type: str = "dom"


@dataclass(frozen=True, slots=True)
class RawPricingClaim:
    claim_type: str
    subject_type: str
    subject_key: str
    raw_value: Any
    evidence: tuple[ClaimEvidence, ...]
    claim_fingerprint: str
    extractor_version: str = RAW_CLAIM_EXTRACTOR_VERSION


def _decimal_amount(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _price_value(match: re.Match[str]) -> dict[str, str]:
    amount = match.group("amount_before") or match.group("amount_symbol") or match.group("amount_after")
    currency_code = match.group("currency_code_before") or match.group("currency_code_after")
    currency_symbol = match.group("symbol_before") or match.group("symbol_only")
    value = {"amount_raw": amount, "price_text": normalize_text(match.group(0))}
    if currency_code:
        value["currency_code_raw"] = currency_code.upper()
    if currency_symbol:
        value["currency_symbol_raw"] = currency_symbol
    return value


def _region_nodes(dom_map: PricingDomMap, region: PricingRegion) -> list[DomNode]:
    included = frozenset(region.node_ids)
    return [node for node in dom_map.nodes if node.node_id in included and node.text]


def _smallest_matching_node(
    nodes: list[DomNode],
    predicate: Callable[[str], bool],
) -> DomNode | None:
    matches = [node for node in nodes if predicate(node.text)]
    return min(matches, key=lambda node: (len(node.text), node.node_id)) if matches else None


def _evidence(node: DomNode, region: PricingRegion, quote: str | None = None) -> ClaimEvidence:
    clean_quote = normalize_text(quote or node.text)[:1_500]
    payload = json.dumps(
        {"node_id": node.node_id, "quote": clean_quote},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ClaimEvidence(
        node_id=node.node_id,
        container_node_id=region.root_node_ids[0],
        quote=clean_quote,
        selector_hint=node.selector_hint,
        table_row=node.table_row,
        table_column=node.table_column,
        evidence_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _claim(
    claim_type: str,
    raw_value: Any,
    evidence: ClaimEvidence | tuple[ClaimEvidence, ...],
) -> RawPricingClaim:
    evidence_items = evidence if isinstance(evidence, tuple) else (evidence,)
    identity = {
        "claim_type": claim_type,
        "subject_key": "product:root",
        "raw_value": raw_value,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return RawPricingClaim(
        claim_type=claim_type,
        subject_type="product",
        subject_key="product:root",
        raw_value=raw_value,
        evidence=evidence_items,
        claim_fingerprint=fingerprint,
    )


def extract_level1_raw_claims(dom_map: PricingDomMap, region: PricingRegion) -> tuple[RawPricingClaim, ...]:
    if not region.root_node_ids:
        return ()
    nodes = _region_nodes(dom_map, region)
    claims: list[RawPricingClaim] = []
    pricing_models: set[str] = set()

    free_node = _smallest_matching_node(
        nodes,
        lambda text: bool(_FREE_PLAN_RE.search(text)) and not re.search(r"\bfree\s+trial\b", text, re.I),
    )
    if free_node:
        claims.append(_claim("has_free_plan", True, _evidence(free_node, region)))

    trial_node = _smallest_matching_node(nodes, lambda text: bool(_FREE_TRIAL_RE.search(text)))
    if trial_node:
        trial_match = _FREE_TRIAL_RE.search(trial_node.text)
        assert trial_match is not None
        trial_value: dict[str, Any] = {"available": True}
        if trial_match.group("duration"):
            trial_value["duration_raw"] = trial_match.group("duration")
            trial_value["duration_unit_raw"] = trial_match.group("unit").lower()
        claims.append(_claim("has_free_trial", trial_value, _evidence(trial_node, region)))

    no_card_node = _smallest_matching_node(nodes, lambda text: bool(_NO_CARD_REQUIRED_RE.search(text)))
    card_node = _smallest_matching_node(nodes, lambda text: bool(_CARD_REQUIRED_RE.search(text)))
    if no_card_node:
        claims.append(_claim("card_required", False, _evidence(no_card_node, region)))
    elif card_node:
        claims.append(_claim("card_required", True, _evidence(card_node, region)))

    custom_node = _smallest_matching_node(nodes, lambda text: bool(_CUSTOM_QUOTE_RE.search(text)))
    if custom_node:
        claims.append(_claim("has_custom_quote", True, _evidence(custom_node, region)))
        pricing_models.add("custom_quote")

    paid_candidates: list[tuple[Decimal, DomNode, re.Match[str], str | None, str]] = []
    usage_candidates: list[tuple[Decimal, DomNode, re.Match[str], re.Match[str]]] = []
    for node in nodes:
        for price_match in _PRICE_RE.finditer(node.text):
            raw_value = _price_value(price_match)
            amount = _decimal_amount(raw_value["amount_raw"])
            if amount is None or amount <= 0:
                continue
            local_context = node.text[
                max(0, price_match.start() - 20):min(len(node.text), price_match.end() + 50)
            ]
            unit_match = _DENOMINATOR_UNIT_RE.search(local_context)
            unit_raw = unit_match.group(1).lower() if unit_match is not None else None
            if unit_raw == "queries":
                unit_raw = "query"
            denominator_kind = (
                "metered"
                if unit_raw in _METERED_UNITS
                else "seat"
                if unit_raw in _SEAT_UNITS
                else None
            )
            if denominator_kind is None and not _PRICE_RELATION_RE.search(local_context):
                continue
            paid_candidates.append((amount, node, price_match, denominator_kind, local_context))
            if unit_match is not None and denominator_kind == "metered":
                usage_candidates.append((amount, node, price_match, unit_match))

    if paid_candidates:
        _, paid_node, paid_match, _, _ = min(
            paid_candidates,
            key=lambda item: (item[0], len(item[1].text), item[1].node_id),
        )
        claims.append(_claim("has_paid_pricing", True, _evidence(paid_node, region)))
        non_usage_prices = [candidate for candidate in paid_candidates if candidate[3] != "metered"]
        if non_usage_prices:
            _, node, price_match, denominator_kind, local_context = min(
                non_usage_prices,
                key=lambda item: (item[0], len(item[1].text), item[1].node_id),
            )
            claims.append(
                _claim(
                    "starting_paid_price",
                    _price_value(price_match),
                    _evidence(node, region),
                )
            )
            period_match = _BILLING_PERIOD_RE.search(local_context)
            if period_match:
                period_raw = period_match.group(1) or period_match.group(2)
                claims.append(
                    _claim(
                        "starting_price_period",
                        period_raw.lower(),
                        _evidence(node, region),
                    )
                )
            if denominator_kind == "seat":
                pricing_models.add("per_seat")

        relationship_text = " ".join(candidate[4] for candidate in paid_candidates)
        if re.search(r"/\s*(?:month|mo|year|yr)\b|\bper\s+(?:month|year)\b|\b(?:monthly|yearly)\b", relationship_text, re.I):
            pricing_models.add("subscription")
        if re.search(r"\b(?:one[- ]time|lifetime)\b", relationship_text, re.I):
            pricing_models.add("one_time")

    usage_candidate = min(
        usage_candidates,
        key=lambda item: (len(item[1].text), item[0], item[1].node_id),
    ) if usage_candidates else None
    if usage_candidate:
        _, node, price_match, unit_match = usage_candidate
        usage_value = _price_value(price_match)
        usage_value["unit_raw"] = unit_match.group(1).lower()
        claims.append(_claim("has_usage_pricing", True, _evidence(node, region)))
        claims.append(_claim("usage_rate", usage_value, _evidence(node, region)))
        pricing_models.add("usage_based")
        if usage_value["unit_raw"] in {"credit", "point"}:
            pricing_models.add("credit_based")

    allowance_node = _smallest_matching_node(nodes, lambda text: bool(_FREE_ALLOWANCE_RE.search(text)))
    if allowance_node:
        allowance_match = _FREE_ALLOWANCE_RE.search(allowance_node.text)
        assert allowance_match is not None
        allowance_value = {
            "quantity_raw": allowance_match.group("quantity").strip(),
            "unit_raw": allowance_match.group("unit").lower(),
        }
        if allowance_match.group("period"):
            allowance_value["period_raw"] = allowance_match.group("period").lower()
        claims.append(_claim("free_allowance", allowance_value, _evidence(allowance_node, region)))

    if pricing_models:
        monetization_models: set[str] = set()
        if "subscription" in pricing_models:
            monetization_models.add("recurring")
        if pricing_models & {"usage_based", "credit_based"}:
            monetization_models.add("metered")
        if "one_time" in pricing_models:
            monetization_models.add("one_time")
        if "custom_quote" in pricing_models:
            monetization_models.add("custom_quote")
        if len(monetization_models) > 1:
            pricing_models.add("hybrid")
        model_nodes = [node for node in (custom_node,) if node is not None]
        if paid_candidates:
            model_nodes.append(paid_candidates[0][1])
        if usage_candidate:
            model_nodes.append(usage_candidate[1])
        unique_model_nodes = {node.node_id: node for node in model_nodes}
        if unique_model_nodes:
            claims.append(
                _claim(
                    "pricing_models",
                    sorted(pricing_models),
                    tuple(_evidence(node, region) for node in unique_model_nodes.values()),
                )
            )

    # This extractor only emits positive, explicitly evidenced claims. Absence never becomes false.
    return tuple(claims)
