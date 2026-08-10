"""Deterministic pricing-region selection and hashing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .dom import DomNode, PricingDomMap, normalize_text


REGION_DETECTOR_VERSION = "pricing-region-v1"
_CANDIDATE_TAGS = frozenset({"body", "main", "section", "article", "div", "table", "ul"})
_PRICE_RE = re.compile(
    r"(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR|US\$|CA\$|AU\$|[$€£¥₹])\s*\d|"
    r"\d(?:[\d,.]*\d)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR)\b",
    re.IGNORECASE,
)
_POSITIVE_RE = re.compile(
    r"\b(?:pricing|plans?|billing|monthly|annually|yearly|per month|per year|free|"
    r"enterprise|contact sales|custom pricing|most popular|per (?:request|token|seat|user|credit))\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"\b(?:privacy|cookie|terms|legal|copyright|blog|documentation|sign in|log in|navigation)\b",
    re.IGNORECASE,
)
_NEGATIVE_TAGS = frozenset({"nav", "footer", "header", "aside"})


@dataclass(frozen=True, slots=True)
class PricingRegion:
    root_node_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    text: str
    region_hash: str | None
    score: int
    detector_version: str = REGION_DETECTOR_VERSION


def _node_score(node: DomNode) -> int:
    text = node.text
    if not text or len(text) < 3:
        return -100
    attrs = " ".join(value for _, value in node.attributes)
    price_hits = len(_PRICE_RE.findall(text))
    positive_hits = len(_POSITIVE_RE.findall(text))
    negative_hits = len(_NEGATIVE_RE.findall(text))
    score = min(price_hits, 8) * 8 + min(positive_hits, 12) * 2 - min(negative_hits, 8) * 5
    if re.search(r"\b(?:pricing|plans?|billing)\b", attrs, re.IGNORECASE):
        score += 14
    if node.tag in {"main", "section", "article", "table"}:
        score += 3
    # Prefer a compact pricing container over body-sized wrappers with the same evidence.
    score -= min(max(len(text) - 6_000, 0) // 1_000, 20)
    return score


def _has_negative_ancestor(node: DomNode, by_id: dict[str, DomNode]) -> bool:
    current = node
    while current.parent_id:
        parent = by_id.get(current.parent_id)
        if parent is None:
            break
        if parent.tag in _NEGATIVE_TAGS:
            return True
        current = parent
    return False


def detect_pricing_region(dom_map: PricingDomMap) -> PricingRegion:
    by_id = {node.node_id: node for node in dom_map.nodes}
    candidates = [
        node
        for node in dom_map.nodes
        if node.tag in _CANDIDATE_TAGS and not _has_negative_ancestor(node, by_id)
    ]
    ranked = sorted(
        ((_node_score(node), len(node.text), index, node) for index, node in enumerate(candidates)),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    if not ranked or ranked[0][0] < 12:
        return PricingRegion((), (), "", None, ranked[0][0] if ranked else 0)

    score, _, _, root = ranked[0]
    region_nodes = dom_map.descendants(root.node_id)
    canonical_nodes = [
        {
            "tag": node.tag,
            "attributes": {
                name: value
                for name, value in node.attributes
                if name in {"role", "aria-label", "aria-checked", "aria-selected", "itemprop", "itemtype"}
            },
            "text": normalize_text(node.text),
            "table_row": node.table_row,
            "table_column": node.table_column,
        }
        for node in region_nodes
        if node.text
    ]
    canonical = json.dumps(canonical_nodes, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    region_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PricingRegion(
        root_node_ids=(root.node_id,),
        node_ids=tuple(node.node_id for node in region_nodes),
        text=root.text,
        region_hash=region_hash,
        score=score,
    )
