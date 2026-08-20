"""Build a read-only pollution-repair manifest for a closed taxonomy incident.

The command reads remote D1 through the normal runner credentials. It never
writes D1, creates queue requests, fetches websites, or calls an AI provider.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from runner import D1Client, load_config


DEFAULT_INCIDENT_ID = "INC-2026-08-EXAMPLE-COM"
MUTATING_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|VACUUM|ATTACH|DETACH)\b",
    re.IGNORECASE,
)


COHORT_SQL = """
WITH incident_members AS (
  SELECT
    member.id AS member_id,
    member.incident_id,
    member.tool_id,
    member.run_id AS incident_run_id,
    member.rollback_action,
    member.apply_status,
    tool.canonical_slug,
    tool.normalized_domain,
    tool.official_url,
    tool.status AS tool_status,
    tool.entity_kind,
    tool.entity_kind_source,
    tool.duplicate_of_tool_id,
    candidate.id AS anomaly_candidate_id,
    candidate.status AS anomaly_candidate_status,
    candidate.current_primary_slug AS anomaly_current_primary_slug,
    candidate.evidence_json AS anomaly_evidence_json,
    COALESCE((
      SELECT source.source_url
      FROM tool_sources source
      WHERE source.tool_id = tool.id
        AND source.source_type = 'official_site'
        AND source.verification_status = 'verified'
        AND json_extract(source.raw_payload, '$.taxonomy_evidence') = 1
      ORDER BY source.confidence_score DESC, source.id DESC
      LIMIT 1
    ), tool.official_url) AS taxonomy_evidence_url,
    EXISTS (
      SELECT 1
      FROM classification_decisions decision
      WHERE decision.tool_id = tool.id
        AND decision.dimension = 'entity_kind'
        AND decision.decision LIKE 'set_entity_kind:%'
    ) AS has_manual_entity_decision,
    (
      SELECT MAX(request.id)
      FROM classification_reprocess_requests request
      WHERE request.tool_id = tool.id
    ) AS latest_request_id
    ,(
      SELECT MAX(request.id)
      FROM classification_reprocess_requests request
      WHERE request.tool_id = tool.id
        AND request.status IN ('queued', 'running')
    ) AS active_request_id
    ,(
      SELECT assigned.decision_status
      FROM product_taxonomy_assignments assigned
      JOIN taxonomy_terms term ON term.id = assigned.term_id
      WHERE assigned.tool_id = tool.id
        AND assigned.is_primary = 1
        AND assigned.decision_status IN ('verified', 'auto_accepted')
        AND term.dimension = 'primary_category'
        AND term.status = 'active'
      ORDER BY
        CASE assigned.decision_status WHEN 'verified' THEN 0 ELSE 1 END,
        assigned.id DESC
      LIMIT 1
    ) AS accepted_primary_status
    ,(
      SELECT term.slug
      FROM product_taxonomy_assignments assigned
      JOIN taxonomy_terms term ON term.id = assigned.term_id
      WHERE assigned.tool_id = tool.id
        AND assigned.is_primary = 1
        AND assigned.decision_status IN ('verified', 'auto_accepted')
        AND term.dimension = 'primary_category'
        AND term.status = 'active'
      ORDER BY
        CASE assigned.decision_status WHEN 'verified' THEN 0 ELSE 1 END,
        assigned.id DESC
      LIMIT 1
    ) AS accepted_primary_slug
  FROM taxonomy_incident_members member
  JOIN tools tool ON tool.id = member.tool_id
  LEFT JOIN classification_anomaly_candidates candidate
    ON candidate.tool_id = member.tool_id
   AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
  WHERE member.incident_id = ?
    AND member.apply_status IN ('applied', 'audit_only')
)
SELECT
  incident_members.*,
  request.status AS latest_request_status,
  request.attempts AS latest_request_attempts,
  request.max_attempts AS latest_request_max_attempts,
  active_request.status AS active_request_status
FROM incident_members
LEFT JOIN classification_reprocess_requests request
  ON request.id = incident_members.latest_request_id
LEFT JOIN classification_reprocess_requests active_request
  ON active_request.id = incident_members.active_request_id
ORDER BY incident_members.tool_id
"""

PUBLIC_CATEGORY_IMPACT_SQL = """
WITH RECURSIVE category_scope(id) AS (
  SELECT id
  FROM taxonomy_terms
  WHERE dimension = 'primary_category'
    AND slug = 'ai-security-compliance'
    AND status = 'active'
  UNION ALL
  SELECT child.id
  FROM taxonomy_terms child
  JOIN category_scope parent ON child.parent_id = parent.id
  WHERE child.dimension = 'primary_category'
    AND child.status = 'active'
), effective AS (
  SELECT
    tool.id AS tool_id,
    COALESCE(
      (
        SELECT assignment.term_id
        FROM product_taxonomy_assignments assignment
        JOIN taxonomy_terms term ON term.id = assignment.term_id
        WHERE assignment.tool_id = tool.id
          AND assignment.is_primary = 1
          AND assignment.decision_status IN ('verified', 'auto_accepted', 'legacy')
          AND term.dimension = 'primary_category'
          AND term.status = 'active'
        ORDER BY
          CASE assignment.decision_status
            WHEN 'verified' THEN 0 WHEN 'auto_accepted' THEN 1 WHEN 'legacy' THEN 2 ELSE 3
          END,
          CASE assignment.source WHEN 'manual' THEN 0 WHEN 'auto' THEN 1 ELSE 2 END,
          assignment.id DESC
        LIMIT 1
      ),
      (
        SELECT term.id
        FROM taxonomy_terms term
        WHERE term.dimension = 'primary_category'
          AND term.status = 'active'
          AND term.source_category_id = tool.primary_category_id
        ORDER BY term.taxonomy_version DESC, term.id DESC
        LIMIT 1
      )
    ) AS current_term_id,
    COALESCE(
      (
        SELECT assignment.term_id
        FROM product_taxonomy_assignments assignment
        JOIN taxonomy_terms term ON term.id = assignment.term_id
        WHERE assignment.tool_id = tool.id
          AND assignment.is_primary = 1
          AND assignment.decision_status IN ('verified', 'auto_accepted', 'legacy')
          AND term.dimension = 'primary_category'
          AND term.status = 'active'
          AND (
            assignment.decision_status != 'legacy'
            OR NOT EXISTS (
              SELECT 1
              FROM classification_anomaly_candidates candidate
              WHERE candidate.tool_id = tool.id
                AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
                AND candidate.status IN ('pending', 'approved')
            )
          )
        ORDER BY
          CASE assignment.decision_status
            WHEN 'verified' THEN 0 WHEN 'auto_accepted' THEN 1 WHEN 'legacy' THEN 2 ELSE 3
          END,
          CASE assignment.source WHEN 'manual' THEN 0 WHEN 'auto' THEN 1 ELSE 2 END,
          assignment.id DESC
        LIMIT 1
      ),
      (
        SELECT term.id
        FROM taxonomy_terms term
        WHERE term.dimension = 'primary_category'
          AND term.status = 'active'
          AND term.source_category_id = tool.primary_category_id
          AND NOT EXISTS (
            SELECT 1
            FROM classification_anomaly_candidates candidate
            WHERE candidate.tool_id = tool.id
              AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
              AND candidate.status IN ('pending', 'approved')
          )
        ORDER BY term.taxonomy_version DESC, term.id DESC
        LIMIT 1
      )
    ) AS projected_term_id
  FROM tools tool
  WHERE tool.status = 'published'
    AND tool.content_safety_status = 'safe'
    AND tool.duplicate_of_tool_id IS NULL
    AND tool.verification_status IN ('verified', 'pending')
    AND tool.staleness_status IN ('fresh', 'aging')
)
SELECT
  SUM(CASE WHEN current_term_id IN (SELECT id FROM category_scope) THEN 1 ELSE 0 END) AS current_visible_tools,
  SUM(CASE WHEN projected_term_id IN (SELECT id FROM category_scope) THEN 1 ELSE 0 END) AS projected_visible_tools,
  SUM(CASE
    WHEN current_term_id IN (SELECT id FROM category_scope)
     AND (
       projected_term_id IS NULL
       OR projected_term_id NOT IN (SELECT id FROM category_scope)
     )
    THEN 1 ELSE 0 END
  ) AS quarantined_from_category
FROM effective
"""


class ReadOnlyD1Client(D1Client):
    """Reject mutation methods even if this dry-run is changed later."""

    async def execute(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> Any:
        if MUTATING_SQL.search(sql):
            raise RuntimeError(f"dry-run rejected mutating SQL for {operation or 'unknown'}")
        return await super().execute(sql, params, operation=operation)

    async def batch(
        self,
        statements: list[tuple[str, list[Any]]],
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        raise RuntimeError(f"dry-run rejected D1 batch for {operation or 'unknown'}")

    async def run(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError(f"dry-run rejected D1 run for {operation or 'unknown'}")


def _usable_evidence_url(value: Any) -> tuple[bool, str]:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False, "invalid_url"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "invalid_url"
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"example.com", "www.example.com"} or hostname.endswith(".example.com"):
        return False, "neutral_transport_url"
    return True, "eligible"


def _anomaly_matches(row: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(str(row.get("anomaly_evidence_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    matches = payload.get("matches") if isinstance(payload, dict) else None
    return [item for item in matches or [] if isinstance(item, dict)]


def repair_scopes(row: dict[str, Any]) -> list[str]:
    source_to_scope = {
        "localization": "localization",
        "features": "features",
        "product_profile": "product_profile",
        "legacy_classification": "legacy_classification",
        "classification_run": "classification",
        "official_source": "official_source",
    }
    return sorted(
        {
            source_to_scope[source]
            for item in _anomaly_matches(row)
            if (source := str(item.get("source") or "")) in source_to_scope
        }
    )


def has_neutral_transport_evidence(row: dict[str, Any]) -> bool:
    return any(
        item.get("code") == "neutral_transport_example_domain"
        or "neutral_transport_example_domain" in (item.get("matched_codes") or [])
        for item in _anomaly_matches(row)
    )


def classify_row(row: dict[str, Any]) -> str:
    if str(row.get("tool_status") or "") != "published":
        return "not_published"
    if int(row.get("duplicate_of_tool_id") or 0) > 0:
        return "duplicate"
    if str(row.get("entity_kind_source") or "") == "manual" or int(
        row.get("has_manual_entity_decision") or 0
    ):
        return "manual_protected"
    if str(row.get("anomaly_candidate_status") or "") not in {"pending", "approved"}:
        return "no_active_pollution_candidate"
    if not has_neutral_transport_evidence(row):
        return "different_pollution_signature"
    if int(row.get("active_request_id") or 0) > 0:
        return "active_reprocess_request"
    usable, reason = _usable_evidence_url(row.get("taxonomy_evidence_url"))
    return "eligible" if usable else reason


def build_report(
    rows: list[dict[str, Any]],
    incident_id: str,
    public_category_impact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for row in rows:
        bucket = classify_row(row)
        scopes = repair_scopes(row)
        records.append(
            {
                "tool_id": int(row.get("tool_id") or 0),
                "canonical_slug": str(row.get("canonical_slug") or ""),
                "normalized_domain": str(row.get("normalized_domain") or ""),
                "official_url": str(row.get("official_url") or ""),
                "taxonomy_evidence_url": str(row.get("taxonomy_evidence_url") or ""),
                "bucket": bucket,
                "incident_member_id": int(row.get("member_id") or 0),
                "rollback_action": str(row.get("rollback_action") or ""),
                "apply_status": str(row.get("apply_status") or ""),
                "anomaly_candidate_id": int(row.get("anomaly_candidate_id") or 0) or None,
                "anomaly_candidate_status": str(row.get("anomaly_candidate_status") or ""),
                "anomaly_current_primary_slug": str(row.get("anomaly_current_primary_slug") or ""),
                "repair_scopes": scopes,
                "needs_content_repair": any(
                    scope in {"localization", "features", "product_profile", "official_source"}
                    for scope in scopes
                ),
                "needs_classification_reprocess": any(
                    scope in {"classification", "legacy_classification", "product_profile"}
                    for scope in scopes
                ) or not str(row.get("accepted_primary_status") or ""),
                "entity_kind": str(row.get("entity_kind") or ""),
                "entity_kind_source": str(row.get("entity_kind_source") or ""),
                "accepted_primary_status": str(row.get("accepted_primary_status") or ""),
                "accepted_primary_slug": str(row.get("accepted_primary_slug") or ""),
                "active_request_id": int(row.get("active_request_id") or 0) or None,
                "active_request_status": str(row.get("active_request_status") or ""),
                "latest_request_id": int(row.get("latest_request_id") or 0) or None,
                "latest_request_status": str(row.get("latest_request_status") or ""),
            }
        )
    records.sort(key=lambda item: item["tool_id"])
    eligible = [item for item in records if item["bucket"] == "eligible"]
    fingerprint_payload = "\n".join(
        f"{item['tool_id']}\t{item['anomaly_candidate_id']}\t{item['taxonomy_evidence_url']}\t{','.join(item['repair_scopes'])}"
        for item in eligible
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run-read-only-no-fetch-no-model",
        "incident_id": incident_id,
        "summary": {
            "incident_members_scanned": len(records),
            "eligible_for_repair": len(eligible),
            "excluded": len(records) - len(eligible),
            "eligible_needing_content_repair": sum(
                1 for item in eligible if item["needs_content_repair"]
            ),
            "eligible_needing_classification_reprocess": sum(
                1 for item in eligible if item["needs_classification_reprocess"]
            ),
            "bucket_counts": dict(Counter(item["bucket"] for item in records).most_common()),
            "estimated_max_tools": len(eligible),
            "d1_writes": 0,
            "model_calls": 0,
        },
        "eligible_manifest_fingerprint": hashlib.sha256(
            fingerprint_payload.encode("utf-8")
        ).hexdigest(),
        "public_category_impact": {
            "category_slug": "ai-security-compliance",
            "current_visible_tools": int(
                (public_category_impact or {}).get("current_visible_tools") or 0
            ),
            "projected_visible_tools": int(
                (public_category_impact or {}).get("projected_visible_tools") or 0
            ),
            "quarantined_from_category": int(
                (public_category_impact or {}).get("quarantined_from_category") or 0
            ),
        },
        "records": records,
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    impact = report["public_category_impact"]
    lines = [
        "# Taxonomy incident pollution-repair dry-run",
        "",
        f"- Incident: `{report['incident_id']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Mode: `{report['mode']}`",
        f"- Incident members scanned: **{summary['incident_members_scanned']}**",
        f"- Eligible for repair: **{summary['eligible_for_repair']}**",
        f"- Need content repair: **{summary['eligible_needing_content_repair']}**",
        f"- Need classification reprocess: **{summary['eligible_needing_classification_reprocess']}**",
        f"- Excluded by safety gates: **{summary['excluded']}**",
        f"- Buckets: `{json.dumps(summary['bucket_counts'], ensure_ascii=False)}`",
        f"- Manifest fingerprint: `{report['eligible_manifest_fingerprint']}`",
        f"- Public category `{impact['category_slug']}`: **{impact['current_visible_tools']} -> {impact['projected_visible_tools']}**",
        f"- Quarantined from that category: **{impact['quarantined_from_category']}**",
        "",
        "This report made no D1 writes, fetched no websites, and made no model calls.",
    ]
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    config = load_config(require_brightdata=False)
    async with ReadOnlyD1Client(config) as d1:
        rows = await d1.query(
            COHORT_SQL,
            [args.incident_id],
            operation="taxonomy_incident.requeue_dry_run",
        )
        impact_rows = await d1.query(
            PUBLIC_CATEGORY_IMPACT_SQL,
            operation="taxonomy_incident.public_category_impact",
        )
    if args.expected_incident_members and len(rows) != args.expected_incident_members:
        raise RuntimeError(
            "cohort safety check failed: "
            f"expected {args.expected_incident_members}, got {len(rows)}"
        )
    report = build_report(rows, args.incident_id, impact_rows[0] if impact_rows else None)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = output_dir / f"taxonomy-incident-requeue-dry-run-{stamp}"
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report) + "\n", encoding="utf-8")
    return json_path, markdown_path, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", default=DEFAULT_INCIDENT_ID)
    parser.add_argument("--expected-incident-members", type=int, default=1369)
    parser.add_argument("--output-dir", default="logs")
    args = parser.parse_args()
    json_path, markdown_path, report = asyncio.run(_run(args))
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print(f"manifest_fingerprint={report['eligible_manifest_fingerprint']}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")


if __name__ == "__main__":
    main()
