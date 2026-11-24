"""Final primary-market policy shared by batch and direct classification.

Confidence is not a calibrated probability; these are necessary admission
conditions, not a claim of measured accuracy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

PRIMARY_POLICY_VERSION = "primary-evidence-v5-multimarket-corroborated-2026-11-24"
MIN_AUTO_PRIMARY_CONFIDENCE = 0.50
SECURITY_FAMILY_SLUG = "ai-security-compliance"
MIN_MULTIMARKET_TOP1_L1_CONFIDENCE = 0.85
MIN_MULTIMARKET_RUNNERUP_L1_CONFIDENCE = 0.80
MIN_MULTIMARKET_PRIMARY_LEAF_CONFIDENCE = 0.75
MIN_AUTO_SECONDARY_CONFIDENCE = 0.70
DETECTION_MARKETS = {"content-detection-authenticity", "content-authenticity-detection"}
DETECTION_BOUNDARY = (
    "CATEGORY-SPECIFIC RULE: apply this requirement ONLY to the "
    "content-detection-authenticity leaf, never to other markets. To select THAT leaf, "
    "the main purchased outcome must be an AI-origin, "
    "deepfake, plagiarism, or content-authenticity verdict. Citation/source attribution "
    "and locating supporting passages do not establish authenticity or correctness. "
    "Style analysis, Unicode cleanup, humanization, rewriting, moderation, and generic "
    "security claims are not this primary market. Compare the main sold workflow, "
    "not a free inspection step or a marketing keyword. Preserve explicit denials "
    "of authorship detection. If this leaf is excluded, choose a supported writing, "
    "infrastructure, or other candidate under its own definition. An empty primary is "
    "appropriate only if no candidate market fits; other markets need no detection workflow."
)


def valid_confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and 0 <= result <= 1 else 0.0


def catalog_fingerprint(catalog: Any) -> str:
    payload = [
        {key: getattr(term, key) for key in (
            "term_id", "dimension", "slug", "name", "parent_id", "definition",
            "includes", "excludes", "examples", "taxonomy_version",
        )}
        for term in sorted(catalog.terms, key=lambda term: term.term_id)
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _quotes(profile: dict[str, Any], keys: tuple[str, ...]) -> str:
    quotes = []
    for key in keys:
        value = profile.get(key)
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, dict):
                continue
            for evidence in item.get("evidence") or []:
                if isinstance(evidence, dict) and evidence.get("quote"):
                    quotes.append(str(evidence["quote"]))
    return " ".join(quotes)


def _term_slug(term: Any) -> str:
    return str(getattr(term, "slug", "") or "")


def _is_security_family_term(term: Any) -> bool:
    if term is None:
        return False
    slug = _term_slug(term)
    parent_slug = str(getattr(term, "parent_slug", "") or "")
    return (
        slug in DETECTION_MARKETS
        or slug == SECURITY_FAMILY_SLUG
        or parent_slug == SECURITY_FAMILY_SLUG
    )


def _hit_l1_slug(hit: dict[str, Any]) -> str:
    return _term_slug(hit.get("term")) or str(hit.get("slug") or "")


def is_high_confidence_multimarket_l1(
    l1_hits: list[dict[str, Any]],
    *,
    min_l1_gap: float = 0.08,
) -> bool:
    """Allow close non-security L1 scores when both top markets are strongly supported."""
    if len(l1_hits) < 2:
        return False
    ranked = sorted(
        l1_hits,
        key=lambda hit: valid_confidence(hit.get("confidence")),
        reverse=True,
    )
    top1_conf = valid_confidence(ranked[0].get("confidence"))
    top1_slug = _hit_l1_slug(ranked[0])
    if top1_conf < MIN_MULTIMARKET_TOP1_L1_CONFIDENCE or top1_slug == SECURITY_FAMILY_SLUG:
        return False
    gap_threshold = max(0.0, float(min_l1_gap))
    close_runners = [
        hit
        for hit in ranked[1:]
        if top1_conf - valid_confidence(hit.get("confidence")) < gap_threshold
    ]
    if not close_runners:
        return False
    for hit in close_runners:
        if (
            valid_confidence(hit.get("confidence")) < MIN_MULTIMARKET_RUNNERUP_L1_CONFIDENCE
            or _hit_l1_slug(hit) == SECURITY_FAMILY_SLUG
        ):
            return False
    return True


def is_corroborated_multimarket(
    decision: dict[str, Any] | None,
    l1_hits: list[dict[str, Any]],
    secondary_decisions: list[dict[str, Any]] | None = None,
    *,
    min_l1_gap: float = 0.08,
) -> bool:
    """Verify a dual-high product has grounded primary and secondary market leaves."""
    if not decision or not decision.get("evidence") or not decision.get("term"):
        return False
    if valid_confidence(decision.get("confidence")) < MIN_MULTIMARKET_PRIMARY_LEAF_CONFIDENCE:
        return False
    term = decision["term"]
    if _is_security_family_term(term):
        return False
    if not is_high_confidence_multimarket_l1(l1_hits, min_l1_gap=min_l1_gap):
        return False
    ranked_l1 = sorted(
        l1_hits,
        key=lambda hit: valid_confidence(hit.get("confidence")),
        reverse=True,
    )
    top1_term = ranked_l1[0].get("term")
    if top1_term is None or (term.parent_id or term.term_id) != top1_term.term_id:
        return False
    top1_conf = valid_confidence(ranked_l1[0].get("confidence"))
    gap_threshold = max(0.0, float(min_l1_gap))
    close_runners = [
        hit
        for hit in ranked_l1[1:]
        if top1_conf - valid_confidence(hit.get("confidence")) < gap_threshold
    ]
    if not close_runners:
        return False
    secondaries = secondary_decisions or []
    for hit in close_runners:
        runner_term = hit.get("term")
        if runner_term is None:
            return False
        corroborated = any(
            (sec.get("term") is not None)
            and ((sec["term"].parent_id or sec["term"].term_id) == runner_term.term_id)
            and valid_confidence(sec.get("confidence")) >= MIN_AUTO_PRIMARY_CONFIDENCE
            and bool(sec.get("evidence"))
            and not _is_security_family_term(sec["term"])
            for sec in secondaries
        )
        if not corroborated:
            return False
    return True


def secondary_acceptance_status(decision: dict[str, Any]) -> str:
    """Promote grounded non-security secondary markets with strong confidence."""
    term = decision.get("term")
    if (
        term is not None
        and not _is_security_family_term(term)
        and valid_confidence(decision.get("confidence")) >= MIN_AUTO_SECONDARY_CONFIDENCE
        and bool(decision.get("evidence"))
    ):
        return "auto_accepted"
    return "provisional"


def primary_acceptance(
    decision: dict[str, Any] | None,
    profile: dict[str, Any],
    l1_hits: list[dict[str, Any]],
    *,
    threshold: float = MIN_AUTO_PRIMARY_CONFIDENCE,
    min_l1_gap: float = 0.08,
    secondary_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fail closed even when an escalation model returned a valid leaf slug."""
    reasons = []
    confidence = valid_confidence((decision or {}).get("confidence"))
    minimum = max(MIN_AUTO_PRIMARY_CONFIDENCE, valid_confidence(threshold))
    if not decision:
        reasons.append("no_valid_leaf")
    if confidence < minimum:
        reasons.append("low_primary_confidence")
    if not (decision or {}).get("evidence"):
        reasons.append("missing_grounded_leaf_evidence")
    if (profile.get("evidence_tier") == "upstream_metadata"
            or "upstream-fallback" in str(profile.get("extractor_version") or "")):
        reasons.append("upstream_metadata_requires_review")
    entity = profile.get("entity_decision") or {}
    if not entity.get("accepted") or entity.get("kind") not in {"independent_product", "app_or_extension"}:
        reasons.append("entity_not_eligible")
    main_text = _quotes(profile, ("main_sold_workflow", "primary_job"))
    if not main_text.strip():
        reasons.append("missing_main_workflow_evidence")
    multimarket_corroborated = is_corroborated_multimarket(
        decision,
        l1_hits,
        secondary_decisions,
        min_l1_gap=min_l1_gap,
    )
    if decision:
        term = decision["term"]
        parent_id = term.parent_id or term.term_id
        selected = next((hit for hit in l1_hits if hit["term"].term_id == parent_id), None)
        if selected is None or valid_confidence(selected.get("confidence")) < MIN_AUTO_PRIMARY_CONFIDENCE:
            reasons.append("unsupported_parent_market")
        ordered = sorted((valid_confidence(hit.get("confidence")) for hit in l1_hits), reverse=True)
        if len(ordered) > 1 and ordered[0] - ordered[1] < max(0, min_l1_gap) and not multimarket_corroborated:
            reasons.append("unresolved_market_ambiguity")
        if term.slug in DETECTION_MARKETS:
            # Necessary evidence on the main workflow, never a keyword assignment.
            # Unrecognized language or mixed purpose stays in human review.
            detection = re.search(
                r"(?:\b(?:detect\w*|identify|check\w*|verify|analy[sz]\w*)\b.{0,90}"
                r"(?:AI[ -](?:generated|written)|generated by AI|automatically generated|machine[ -]generated|"
                r"generated (?:by|from) (?:large )?language models?|deepfake|plagiar|synthetic (?:content|media)|authenticity)|"
                r"(?:AI (?:content|text|image|audio|video) |deepfake |plagiarism )(?:detect\w*|check\w*)|"
                r"(?:检测|识别|鉴别).{0,25}(?:AI生成|人工智能生成|深度伪造|抄袭)|(?:AI|人工智能).{0,8}(?:内容|文本).{0,4}检测)",
                main_text, re.I,
            )
            if not detection:
                reasons.append("detection_main_workflow_not_established")
            negative_text = main_text + " " + _quotes(profile, ("negative_claims",))
            if re.search(r"(?:not|isn.t|does not|no).{0,60}(?:authorship|AI detector|detect.{0,15}AI|prove.{0,15}written)|不是.{0,12}(?:检测|作者)", negative_text, re.I):
                reasons.append("explicit_detection_denial")
            if re.search(r"humaniz\w*|rewrit\w*|remove.{0,20}(?:traces|watermark)|改写|去除.{0,8}痕迹", main_text, re.I):
                reasons.append("detection_editing_boundary_requires_review")
    status = "auto_accepted" if not reasons else ("provisional" if confidence >= 0.35 else "unresolved")
    return {
        "policy_version": PRIMARY_POLICY_VERSION,
        "status": status,
        "eligible": not reasons,
        "reasons": reasons,
        "multimarket_corroborated": multimarket_corroborated,
    }
