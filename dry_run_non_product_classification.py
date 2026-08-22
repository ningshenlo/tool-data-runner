"""Read-only anti-bot anomaly dry-run for the V2 non-product cohort.

This command never mutates D1 and never creates reclassification requests. It
loads the exact latest V2 `entity_not_eligible:non_product` cohort, evaluates
stored evidence with the production anti-bot signatures, and writes a local
JSON/Markdown report.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from classification_anomalies import (
    build_anti_bot_anomaly_candidate,
    build_classification_pollution_matches,
)
from runner import D1Client, load_config
from taxonomy_shadow import SHADOW_PROMPT_VERSION


MUTATING_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|VACUUM|ATTACH|DETACH)\b",
    re.IGNORECASE,
)


COHORT_CTE = """
WITH cohort AS (
  SELECT
    t.id AS tool_id,
    t.canonical_slug,
    t.normalized_domain,
    t.official_url,
    t.entity_kind,
    t.entity_kind_source,
    t.primary_category_id,
    t.category_classification_status,
    COALESCE((
      SELECT localized.name
      FROM tool_localizations localized
      WHERE localized.tool_id = t.id
        AND localized.translation_status = 'published'
        AND trim(COALESCE(localized.name, '')) <> ''
      ORDER BY CASE localized.locale_code WHEN 'en' THEN 0 ELSE 1 END, localized.locale_code
      LIMIT 1
    ), t.canonical_slug) AS tool_name,
    COALESCE((
      SELECT assigned.decision_status
      FROM product_taxonomy_assignments assigned
      JOIN taxonomy_terms assigned_term
        ON assigned_term.id = assigned.term_id
       AND assigned_term.dimension = 'primary_category'
      WHERE assigned.tool_id = t.id
        AND assigned.is_primary = 1
        AND assigned.decision_status IN ('verified', 'auto_accepted', 'legacy')
      ORDER BY
        CASE assigned.decision_status WHEN 'verified' THEN 0 WHEN 'auto_accepted' THEN 1 ELSE 2 END,
        assigned.id DESC
      LIMIT 1
    ), '') AS assignment_decision_status,
    COALESCE((
      SELECT assigned.source
      FROM product_taxonomy_assignments assigned
      JOIN taxonomy_terms assigned_term
        ON assigned_term.id = assigned.term_id
       AND assigned_term.dimension = 'primary_category'
      WHERE assigned.tool_id = t.id
        AND assigned.is_primary = 1
        AND assigned.decision_status IN ('verified', 'auto_accepted', 'legacy')
      ORDER BY
        CASE assigned.decision_status WHEN 'verified' THEN 0 WHEN 'auto_accepted' THEN 1 ELSE 2 END,
        assigned.id DESC
      LIMIT 1
    ), '') AS assignment_source,
    COALESCE((
      SELECT assigned.term_id
      FROM product_taxonomy_assignments assigned
      JOIN taxonomy_terms assigned_term
        ON assigned_term.id = assigned.term_id
       AND assigned_term.dimension = 'primary_category'
      WHERE assigned.tool_id = t.id
        AND assigned.is_primary = 1
        AND assigned.decision_status IN ('verified', 'auto_accepted', 'legacy')
      ORDER BY
        CASE assigned.decision_status WHEN 'verified' THEN 0 WHEN 'auto_accepted' THEN 1 ELSE 2 END,
        assigned.id DESC
      LIMIT 1
    ), 0) AS current_primary_term_id,
    COALESCE((
      SELECT assigned_term.slug
      FROM product_taxonomy_assignments assigned
      JOIN taxonomy_terms assigned_term ON assigned_term.id = assigned.term_id
      WHERE assigned.tool_id = t.id
        AND assigned.is_primary = 1
        AND assigned_term.dimension = 'primary_category'
        AND assigned.decision_status IN ('verified', 'auto_accepted', 'legacy')
      ORDER BY
        CASE assigned.decision_status WHEN 'verified' THEN 0 WHEN 'auto_accepted' THEN 1 ELSE 2 END,
        assigned.id DESC
      LIMIT 1
    ), legacy_category.canonical_slug, '') AS current_primary_slug,
    latest_run.id AS latest_run_id,
    latest_run.run_status AS latest_run_status,
    latest_run.error AS latest_run_error,
    COALESCE(json_extract(latest_run.candidate_terms_json, '$.entity.candidate_kind'), '') AS entity_candidate_kind,
    COALESCE(CAST(json_extract(latest_run.candidate_terms_json, '$.entity.confidence') AS REAL), 0.0) AS entity_confidence,
    COALESCE(json_extract(latest_run.candidate_terms_json, '$.entity.reason'), '') AS entity_reason,
    COALESCE(json_extract(latest_run.candidate_terms_json, '$.entity.evidence'), '[]') AS entity_evidence_json,
    COALESCE(json_extract(latest_run.raw_output, '$.page_quality.state'), '') AS page_quality_state,
    COALESCE(json_extract(latest_run.raw_output, '$.profile_extraction_path'), '') AS profile_extraction_path,
    COALESCE(json_extract(latest_run.raw_output, '$.source_url'), '') AS classification_source_url,
    latest_run.created_at AS latest_run_created_at
  FROM tools t
  JOIN classification_runs latest_run ON latest_run.id = (
    SELECT run.id
    FROM classification_runs run
    WHERE run.tool_id = t.id
      AND run.prompt_version = ?
    ORDER BY run.created_at DESC, run.id DESC
    LIMIT 1
  )
  LEFT JOIN categories legacy_category ON legacy_category.id = t.primary_category_id
  WHERE t.status = 'published'
    AND t.duplicate_of_tool_id IS NULL
    AND t.entity_kind = 'non_product'
    AND latest_run.run_status = 'skipped'
    AND latest_run.error = 'entity_not_eligible:non_product'
)
"""


COHORT_SQL = COHORT_CTE + """
SELECT *
FROM cohort
ORDER BY tool_id
"""


EVIDENCE_SQL = COHORT_CTE + """,
candidate_text AS (
  SELECT
    cohort.*,
    COALESCE(substr(t.category_classification_raw, 1, 16000), '') AS category_classification_raw,
    COALESCE((
      SELECT substr(
        COALESCE(localized.tagline, '') || ' ' ||
        COALESCE(localized.short_description, '') || ' ' ||
        COALESCE(localized.long_description, '') || ' ' ||
        COALESCE(localized.feature_highlights, ''),
        1, 12000
      )
      FROM tool_localizations localized
      WHERE localized.tool_id = cohort.tool_id
        AND localized.translation_status = 'published'
      ORDER BY CASE localized.locale_code WHEN 'en' THEN 0 ELSE 1 END, localized.locale_code
      LIMIT 1
    ), '') AS localization_text,
    COALESCE((
      SELECT substr(group_concat(feature.feature_name || ' ' || COALESCE(feature.feature_description, ''), ' '), 1, 8000)
      FROM tool_key_features feature
      WHERE feature.tool_id = cohort.tool_id
    ), '') AS feature_text,
    COALESCE(substr(profile.profile_json, 1, 12000), '') AS profile_text,
    COALESCE(substr(latest_run.raw_output, 1, 16000), '') AS latest_run_text,
    COALESCE((
      SELECT substr(source.raw_payload, 1, 12000)
      FROM tool_sources source
      WHERE source.tool_id = cohort.tool_id
        AND source.source_type = 'official_site'
      ORDER BY source.confidence_score DESC, source.id DESC
      LIMIT 1
    ), '') AS source_text
  FROM cohort
  JOIN tools t ON t.id = cohort.tool_id
  JOIN classification_runs latest_run ON latest_run.id = cohort.latest_run_id
  LEFT JOIN product_profiles profile ON profile.tool_id = cohort.tool_id
), matched AS (
  SELECT *, lower(
    localization_text || ' ' || feature_text || ' ' || profile_text || ' ' ||
    category_classification_raw || ' ' || latest_run_text || ' ' || source_text
  ) AS detection_text
  FROM candidate_text
)
SELECT *
FROM matched
WHERE
  instr(detection_text, 'cdn-cgi/challenge-platform') > 0
  OR instr(detection_text, 'cf-chl-') > 0
  OR instr(detection_text, 'cf_chl_') > 0
  OR instr(detection_text, 'cloudflare ray id') > 0
  OR instr(detection_text, 'just a moment') > 0
  OR instr(detection_text, 'just wait') > 0
  OR instr(detection_text, 'checking your browser') > 0
  OR instr(detection_text, 'checking browser') > 0
  OR instr(detection_text, 'you have been blocked') > 0
  OR instr(detection_text, 'security service to protect itself') > 0
  OR instr(detection_text, 'enable javascript and cookies to continue') > 0
  OR instr(detection_text, 'attention required') > 0
  OR instr(detection_text, 'access has been blocked') > 0
  OR instr(detection_text, 'request has been denied for security reasons') > 0
  OR instr(detection_text, 'access to this page has been denied') > 0
  OR instr(detection_text, 'request was blocked') > 0
  OR instr(detection_text, 'request has been blocked') > 0
  OR instr(detection_text, 'access denied') > 0
  OR instr(detection_text, 'error 403') > 0
  OR instr(detection_text, 'verify you are human') > 0
  OR instr(detection_text, 'verifying you are human') > 0
  OR instr(detection_text, 'complete the security check') > 0
  OR instr(detection_text, 'please wait while we verify') > 0
  OR instr(detection_text, 'please wait while we check') > 0
  OR instr(detection_text, 'press & hold') > 0
  OR instr(detection_text, 'press and hold') > 0
  OR instr(detection_text, 'px-captcha') > 0
  OR instr(detection_text, '_pxhd') > 0
  OR instr(detection_text, 'akamai') > 0
  OR instr(detection_text, 'do not have permission to access') > 0
  OR instr(detection_text, 'don''t have permission to access') > 0
  OR instr(detection_text, '_incapsula_resource') > 0
  OR instr(detection_text, 'imperva captcha') > 0
  OR instr(detection_text, 'incapsula incident id') > 0
  OR instr(detection_text, 'powered by imperva') > 0
  OR instr(detection_text, 'request unsuccessful') > 0
  OR instr(detection_text, 'datadome-captcha') > 0
  OR instr(detection_text, 'captcha-delivery.com') > 0
  OR instr(detection_text, 'enable js and disable any ad blocker') > 0
  OR instr(detection_text, 'request could not be satisfied') > 0
  OR instr(detection_text, 'generated by cloudfront') > 0
  OR instr(detection_text, 'sucuri website firewall') > 0
  OR instr(detection_text, 'website firewall - access denied') > 0
  OR instr(detection_text, 'website firewall: access denied') > 0
  OR instr(detection_text, 'too many requests') > 0
  OR instr(detection_text, 'rate limited') > 0
  OR instr(detection_text, 'rate limit exceeded') > 0
  OR instr(detection_text, 'this domain is for use in documentation example') > 0
  OR instr(detection_text, 'example domain') > 0
  OR instr(detection_text, 'iana example') > 0
  OR instr(detection_text, 'avoid use in operations') > 0
ORDER BY tool_id
LIMIT ?
"""


class ReadOnlyD1Client(D1Client):
    """D1 client that rejects mutation SQL even if the script changes later."""

    async def execute(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> Any:
        if MUTATING_SQL.search(sql):
            raise RuntimeError(f"dry-run rejected mutating SQL for {operation or 'unknown operation'}")
        return await super().execute(sql, params, operation=operation)

    async def batch(
        self,
        statements: list[tuple[str, list[Any]]],
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        raise RuntimeError(f"dry-run rejected D1 batch for {operation or 'unknown operation'}")

    async def run(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError(f"dry-run rejected D1 run for {operation or 'unknown operation'}")


def _counter(rows: list[dict[str, Any]], key: str, *, empty: str = "none") -> dict[str, int]:
    counts = Counter(str(row.get(key) or empty) for row in rows)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _confidence_bucket(value: Any) -> str:
    try:
        confidence = float(value or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence >= 0.9:
        return "0.90-1.00"
    if confidence >= 0.8:
        return "0.80-0.89"
    if confidence >= 0.7:
        return "0.70-0.79"
    if confidence >= 0.6:
        return "0.60-0.69"
    return "below-0.60"


def _match_record(row: dict[str, Any]) -> dict[str, Any] | None:
    matches = build_classification_pollution_matches(row)
    if not matches:
        return None

    candidate = build_anti_bot_anomaly_candidate(row)
    return {
        "tool_id": int(row.get("tool_id") or 0),
        "tool_name": str(row.get("tool_name") or ""),
        "canonical_slug": str(row.get("canonical_slug") or ""),
        "normalized_domain": str(row.get("normalized_domain") or ""),
        "official_url": str(row.get("official_url") or ""),
        "entity_kind_source": str(row.get("entity_kind_source") or ""),
        "assignment_decision_status": str(row.get("assignment_decision_status") or ""),
        "assignment_source": str(row.get("assignment_source") or ""),
        "current_primary_slug": str(row.get("current_primary_slug") or ""),
        "latest_run_id": int(row.get("latest_run_id") or 0),
        "matches": matches,
        "detector_candidate": candidate,
    }


def _markdown_table(records: list[dict[str, Any]], limit: int = 30) -> str:
    lines = [
        "| ID | Tool | Domain | Category | Score | Severity | Provider / code | Source |",
        "|---:|---|---|---|---:|---|---|---|",
    ]
    for record in records[:limit]:
        candidate = record.get("detector_candidate") or {}
        match = record["matches"][0]
        values = (
            str(record["tool_id"]),
            record["tool_name"],
            record["normalized_domain"],
            record["current_primary_slug"] or "none",
            str(candidate.get("score") or 55),
            str(candidate.get("severity") or "below-threshold"),
            f"{match['provider']} / {match['code']}",
            match["source"],
        )
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    return "\n".join(lines)


def _build_report(
    cohort_rows: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
    *,
    prompt_version: str,
) -> dict[str, Any]:
    records = [record for row in evidence_rows if (record := _match_record(row))]
    records.sort(
        key=lambda record: (
            -int((record.get("detector_candidate") or {}).get("score") or 55),
            record["tool_id"],
        )
    )
    candidates = [record for record in records if record.get("detector_candidate")]
    match_rows = [match for record in records for match in record["matches"]]
    anti_bot_tools = sum(
        any(match["provider"] != "neutral_transport" for match in record["matches"])
        for record in records
    )
    neutral_transport_tools = sum(
        any(match["provider"] == "neutral_transport" for match in record["matches"])
        for record in records
    )
    severity = Counter(
        str(record["detector_candidate"]["severity"])
        for record in candidates
    )
    confidence_buckets = Counter(
        _confidence_bucket(row.get("entity_confidence")) for row in cohort_rows
    )
    entity_decisions = [
        {
            "tool_id": int(row.get("tool_id") or 0),
            "tool_name": str(row.get("tool_name") or ""),
            "canonical_slug": str(row.get("canonical_slug") or ""),
            "normalized_domain": str(row.get("normalized_domain") or ""),
            "official_url": str(row.get("official_url") or ""),
            "assignment_decision_status": str(row.get("assignment_decision_status") or ""),
            "current_primary_slug": str(row.get("current_primary_slug") or ""),
            "entity_candidate_kind": str(row.get("entity_candidate_kind") or ""),
            "entity_confidence": float(row.get("entity_confidence") or 0.0),
            "entity_reason": str(row.get("entity_reason") or ""),
            "entity_evidence_json": str(row.get("entity_evidence_json") or "[]"),
            "page_quality_state": str(row.get("page_quality_state") or ""),
            "profile_extraction_path": str(row.get("profile_extraction_path") or ""),
            "classification_source_url": str(row.get("classification_source_url") or ""),
        }
        for row in cohort_rows
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run-read-only",
        "prompt_version": prompt_version,
        "cohort_definition": {
            "tool_status": "published",
            "duplicate_of_tool_id": None,
            "entity_kind": "non_product",
            "latest_v2_run_status": "skipped",
            "latest_v2_error": "entity_not_eligible:non_product",
        },
        "summary": {
            "cohort_total": len(cohort_rows),
            "sql_prefilter_rows": len(evidence_rows),
            "signature_hit_tools": len(records),
            "anti_bot_signature_hit_tools": anti_bot_tools,
            "neutral_transport_hit_tools": neutral_transport_tools,
            "detector_candidate_tools": len(candidates),
            "below_threshold_signature_tools": len(records) - len(candidates),
            "no_signature_tools": len(cohort_rows) - len(records),
            "signature_hit_percent": round(100 * len(records) / max(1, len(cohort_rows)), 2),
            "detector_candidate_percent": round(100 * len(candidates) / max(1, len(cohort_rows)), 2),
        },
        "cohort_breakdown": {
            "assignment_decision_status": _counter(cohort_rows, "assignment_decision_status"),
            "assignment_source": _counter(cohort_rows, "assignment_source"),
            "entity_kind_source": _counter(cohort_rows, "entity_kind_source"),
            "current_primary_slug": _counter(cohort_rows, "current_primary_slug"),
            "entity_candidate_kind": _counter(cohort_rows, "entity_candidate_kind"),
            "entity_confidence_bucket": dict(confidence_buckets),
            "page_quality_state": _counter(cohort_rows, "page_quality_state"),
            "profile_extraction_path": _counter(cohort_rows, "profile_extraction_path"),
        },
        "detection_breakdown": {
            "severity": dict(sorted(severity.items(), key=lambda item: (-item[1], item[0]))),
            "provider": dict(Counter(str(match["provider"]) for match in match_rows).most_common()),
            "code": dict(Counter(str(match["code"]) for match in match_rows).most_common()),
            "source": dict(Counter(str(match["source"]) for match in match_rows).most_common()),
        },
        "entity_decisions": entity_decisions,
        "records": records,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    records = report["records"]
    return "\n".join(
        [
            "# Non-product classification anomaly dry-run",
            "",
            f"Generated: `{report['generated_at']}`",
            f"Mode: `{report['mode']}` (no D1 writes, no queue changes)",
            f"Prompt: `{report['prompt_version']}`",
            "",
            "## Summary",
            "",
            f"- Cohort scanned: **{summary['cohort_total']}**",
            f"- Classification evidence pollution hits: **{summary['signature_hit_tools']}** ({summary['signature_hit_percent']}%)",
            f"- Neutral example.com transport leakage: **{summary['neutral_transport_hit_tools']}**",
            f"- WAF / anti-bot signature hits: **{summary['anti_bot_signature_hit_tools']}**",
            f"- Existing detector threshold passed: **{summary['detector_candidate_tools']}** ({summary['detector_candidate_percent']}%)",
            f"- Signature hits below current threshold: **{summary['below_threshold_signature_tools']}**",
            f"- No stored pollution signature: **{summary['no_signature_tools']}**",
            f"- Entity confidence: `{json.dumps(report['cohort_breakdown']['entity_confidence_bucket'], ensure_ascii=False)}`",
            f"- Page quality states: `{json.dumps(report['cohort_breakdown']['page_quality_state'], ensure_ascii=False)}`",
            "",
            "## Detection breakdown",
            "",
            f"- Severity: `{json.dumps(report['detection_breakdown']['severity'], ensure_ascii=False)}`",
            f"- Providers: `{json.dumps(report['detection_breakdown']['provider'], ensure_ascii=False)}`",
            f"- Evidence sources: `{json.dumps(report['detection_breakdown']['source'], ensure_ascii=False)}`",
            "",
            "## Highest-ranked hits",
            "",
            _markdown_table(records),
            "",
            "The JSON companion contains all matched records and compact evidence snippets.",
        ]
    )


async def _run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    config = load_config(require_brightdata=False)
    async with ReadOnlyD1Client(config) as d1:
        cohort_rows = await d1.query(
            COHORT_SQL,
            [args.prompt_version],
            operation="classification_anomaly.dry_run_non_product_cohort",
        )
        if args.expected_count and len(cohort_rows) != args.expected_count:
            raise RuntimeError(
                f"cohort safety check failed: expected {args.expected_count}, got {len(cohort_rows)}"
            )
        evidence_rows = await d1.query(
            EVIDENCE_SQL,
            [args.prompt_version, args.prefilter_limit],
            operation="classification_anomaly.dry_run_non_product_evidence",
        )

    report = _build_report(
        cohort_rows,
        evidence_rows,
        prompt_version=args.prompt_version,
    )
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = output_dir / f"non-product-classification-dry-run-{stamp}"
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(report) + "\n", encoding="utf-8")
    return json_path, markdown_path, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-version", default=SHADOW_PROMPT_VERSION)
    parser.add_argument("--expected-count", type=int, default=1371)
    parser.add_argument("--prefilter-limit", type=int, default=2000)
    parser.add_argument("--output-dir", default="logs")
    args = parser.parse_args()
    json_path, markdown_path, report = asyncio.run(_run(args))
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")


if __name__ == "__main__":
    main()
