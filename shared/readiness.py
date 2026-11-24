"""Canonical catalog readiness and auto-publish SQL predicates.

Admin UI and workers must treat this module as the single source of truth for
hard gates. Warnings (screenshot, favicon, traffic, etc.) stay outside these
predicates.
"""

from __future__ import annotations

from taxonomy_precision import MIN_AUTO_PRIMARY_CONFIDENCE

__all__ = [
    "accepted_primary_taxonomy_predicate",
    "clean_tool_name_sql",
    "clean_tool_slug_sql",
    "tool_auto_publish_admission_predicate",
    "tool_live_ready_predicate",
]


def clean_tool_name_sql(loc_alias: str) -> str:
    if loc_alias not in {"l", "localization"}:
        raise ValueError("Unsupported tool_localizations table alias")
    return f"""
      trim({loc_alias}.name) <> ''
      AND coalesce({loc_alias}.name_review_status, 'legacy_unreviewed') NOT IN ('needs_review', 'rejected')
      AND (length(trim({loc_alias}.name)) <= 60 OR ({loc_alias}.name_review_status = 'human_approved' AND length(trim({loc_alias}.name)) <= 120))
      AND {loc_alias}.name NOT LIKE '%<%'
      AND {loc_alias}.name NOT LIKE '%>%'
      AND {loc_alias}.name NOT LIKE '%`%'
      AND lower({loc_alias}.name) NOT LIKE '%json-ld%'
      AND (
        coalesce({loc_alias}.name_review_status, 'legacy_unreviewed') = 'human_approved'
        OR (
          {loc_alias}.name NOT LIKE '% | %'
          AND {loc_alias}.name NOT LIKE '% - %'
          AND {loc_alias}.name NOT LIKE '% — %'
          AND {loc_alias}.name NOT LIKE '% – %'
          AND {loc_alias}.name NOT LIKE '%: %'
          AND {loc_alias}.name NOT LIKE '% · %'
          AND lower(trim({loc_alias}.name)) NOT LIKE 'just a moment%'
          AND lower(trim({loc_alias}.name)) NOT LIKE 'access denied%'
          AND lower(trim({loc_alias}.name)) NOT LIKE '403 forbidden%'
          AND lower(trim({loc_alias}.name)) <> 'home'
          AND lower(trim({loc_alias}.name)) NOT LIKE 'deployment paused%'
        )
      )
    """


def clean_tool_slug_sql(loc_alias: str) -> str:
    if loc_alias not in {"l", "localization"}:
        raise ValueError("Unsupported tool_localizations table alias")
    return f"""
      trim(coalesce({loc_alias}.localized_slug, '')) <> ''
      AND length(trim({loc_alias}.localized_slug)) <= 50
      AND lower(trim({loc_alias}.localized_slug)) NOT LIKE 'access-denied%'
      AND lower(trim({loc_alias}.localized_slug)) NOT LIKE 'just-a-moment%'
      AND lower(trim({loc_alias}.localized_slug)) NOT LIKE 'ai-directory-%'
      AND NOT (
        length(trim({loc_alias}.localized_slug)) >= 10
        AND substr(trim({loc_alias}.localized_slug), length(trim({loc_alias}.localized_slug)) - 8, 1) = '-'
        AND glob('[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]', substr(trim({loc_alias}.localized_slug), length(trim({loc_alias}.localized_slug)) - 7))
      )
    """


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
          AND {clean_tool_name_sql('localization')}
          AND {clean_tool_slug_sql('localization')}
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
