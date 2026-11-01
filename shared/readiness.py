"""Canonical catalog readiness and auto-publish SQL predicates.

Admin UI and workers must treat this module as the single source of truth for
hard gates. Warnings (screenshot, favicon, traffic, etc.) stay outside these
predicates.
"""

from __future__ import annotations

from taxonomy_precision import MIN_AUTO_PRIMARY_CONFIDENCE

__all__ = [
    "accepted_primary_taxonomy_predicate",
    "tool_auto_publish_admission_predicate",
    "tool_live_ready_predicate",
]


def accepted_primary_taxonomy_predicate(tool_alias: str) -> str:
    if tool_alias not in {"t", "tools"}:
        raise ValueError("Unsupported tools table alias")
    return f"""
      EXISTS (
        SELECT 1
        FROM product_taxonomy_assignments assignment
        JOIN taxonomy_terms term
          ON term.id = assignment.term_id
         AND term.dimension = 'primary_category'
         AND term.status = 'active'
        WHERE assignment.tool_id = {tool_alias}.id
          AND assignment.is_primary = 1
          AND assignment.decision_status IN ('verified', 'auto_accepted')
          AND (assignment.decision_status = 'verified'
               OR coalesce(assignment.confidence, 0) >= {MIN_AUTO_PRIMARY_CONFIDENCE})
      )
    """


def tool_live_ready_predicate(tool_alias: str) -> str:
    if tool_alias not in {"t", "tools"}:
        raise ValueError("Unsupported tools table alias")
    return f"""
      {tool_alias}.content_safety_status = 'safe'
      AND EXISTS (
        SELECT 1 FROM tool_enrichment_states state
        WHERE state.tool_id = {tool_alias}.id AND state.readiness = 'ready'
      )
      AND EXISTS (
        SELECT 1 FROM tool_localizations localization
        WHERE localization.tool_id = {tool_alias}.id
          AND trim(localization.name) <> ''
      )
      AND {accepted_primary_taxonomy_predicate(tool_alias)}
      AND EXISTS (
        SELECT 1 FROM tool_sources source
        WHERE source.tool_id = {tool_alias}.id
      )
    """


def tool_auto_publish_admission_predicate(tool_alias: str) -> str:
    if tool_alias not in {"t", "tools"}:
        raise ValueError("Unsupported tools table alias")
    # Admission holds affect publication only: evidence/asset enrichment must continue.
    # Keep legacy rows eligible; CASE prevents malformed legacy JSON from aborting a batch.
    return f"""
      NOT EXISTS (
        SELECT 1 FROM tool_sources admission_source
        WHERE admission_source.tool_id = {tool_alias}.id
          AND admission_source.source_type = 'official_site'
          AND CASE WHEN json_valid(admission_source.raw_payload)
            THEN json_extract(admission_source.raw_payload,
              '$.qualification.catalog_fit.decision.autoPublishAllowed') = 0
            ELSE 0 END
      )
    """
