"""Build the immutable artifacts and raw facts for one pricing snapshot."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .dom import PricingDomMap, parse_pricing_dom
from .raw_claims import RawPricingClaim, extract_level1_raw_claims
from .regions import PricingRegion, detect_pricing_region
from .snapshot import SnapshotArtifact, build_snapshot_artifact


SNAPSHOT_FORMAT_VERSION = "pricing-snapshot-v1"
_CURRENCY_CODE_RE = re.compile(r"\b(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR)\b", re.IGNORECASE)
_CURRENCY_SYMBOL_RE = re.compile(r"US\$|CA\$|AU\$|[$€£¥₹]")


@dataclass(frozen=True, slots=True)
class SnapshotArtifactPayload:
    artifact: SnapshotArtifact
    body: bytes


@dataclass(frozen=True, slots=True)
class PricingSnapshotBundle:
    dom_map: PricingDomMap
    region: PricingRegion
    raw_claims: tuple[RawPricingClaim, ...]
    artifacts: tuple[SnapshotArtifactPayload, ...]
    observed_currency_context: str
    format_version: str = SNAPSHOT_FORMAT_VERSION


def _payload(
    artifact_type: str,
    body: str,
    content_type: str,
) -> SnapshotArtifactPayload:
    encoded = body.encode("utf-8")
    return SnapshotArtifactPayload(
        artifact=build_snapshot_artifact(
            artifact_type,
            encoded,
            content_type=content_type,
            retention_class="changed_snapshot",
        ),
        body=encoded,
    )


def build_pricing_snapshot_bundle(
    html: str,
    *,
    rendered: bool = False,
    original_html: str | None = None,
) -> PricingSnapshotBundle:
    dom_map = parse_pricing_dom(html)
    region = detect_pricing_region(dom_map)
    raw_claims = extract_level1_raw_claims(dom_map, region)
    html_artifacts = (
        (
            _payload("html", original_html, "text/html; charset=utf-8"),
            _payload("rendered_html", html, "text/html; charset=utf-8"),
        )
        if rendered and original_html is not None
        else (_payload("rendered_html" if rendered else "html", html, "text/html; charset=utf-8"),)
    )
    artifacts = html_artifacts + (
        _payload("text", dom_map.visible_text, "text/plain; charset=utf-8"),
        _payload("structured_data", dom_map.structured_data_json(), "application/json"),
        _payload("dom_map", dom_map.to_json(), "application/json"),
    )
    context = {
        "diagnostic_only": True,
        "currency_codes": sorted({match.upper() for match in _CURRENCY_CODE_RE.findall(region.text)}),
        "currency_symbols": sorted(set(_CURRENCY_SYMBOL_RE.findall(region.text))),
    }
    return PricingSnapshotBundle(
        dom_map=dom_map,
        region=region,
        raw_claims=raw_claims,
        artifacts=artifacts,
        observed_currency_context=json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
