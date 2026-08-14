"""Automatic classification anomaly detection and reprocessing queue helpers."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from anti_bot_signatures import detect_anti_bot_text


ANTI_BOT_CLASSIFICATION_DETECTOR = "anti_bot_classification_pollution_v1"
ANOMALY_SCAN_INTERVAL_HOURS = 6
ANOMALY_SCAN_LIMIT = 500
NEUTRAL_TRANSPORT_PLACEHOLDER_RE = re.compile(
    r"\b(?:this\s+domain\s+is\s+for\s+use\s+in\s+documentation\s+examples?"
    r"|example\s+domain\b|iana\s+example(?:\s+domain)?\b|avoid\s+use\s+in\s+operations)\b",
    re.IGNORECASE,
)


ANOMALY_SCAN_SQL = """
WITH candidates AS (
  SELECT
    t.id AS tool_id,
    t.canonical_slug,
    t.official_url,
    t.primary_category_id,
    t.category_classification_status,
    t.category_classification_raw,
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
    COALESCE((
      SELECT substr(
        COALESCE(localized.tagline, '') || ' ' ||
        COALESCE(localized.short_description, '') || ' ' ||
        COALESCE(localized.long_description, '') || ' ' ||
        COALESCE(localized.feature_highlights, ''),
        1, 12000
      )
      FROM tool_localizations localized
      WHERE localized.tool_id = t.id
        AND localized.translation_status = 'published'
      ORDER BY CASE localized.locale_code WHEN 'en' THEN 0 ELSE 1 END, localized.locale_code
      LIMIT 1
    ), '') AS localization_text,
    COALESCE((
      SELECT substr(group_concat(feature.feature_name || ' ' || COALESCE(feature.feature_description, ''), ' '), 1, 8000)
      FROM tool_key_features feature
      WHERE feature.tool_id = t.id
    ), '') AS feature_text,
    COALESCE(substr(profile.profile_json, 1, 12000), '') AS profile_text,
    COALESCE(latest_run.id, 0) AS latest_run_id,
    COALESCE(latest_run.run_status, '') AS latest_run_status,
    COALESCE(latest_run.error, '') AS latest_run_error,
    COALESCE(substr(latest_run.raw_output, 1, 16000), '') AS latest_run_text,
    COALESCE((
      SELECT substr(source.raw_payload, 1, 12000)
      FROM tool_sources source
      WHERE source.tool_id = t.id
        AND source.source_type = 'official_site'
      ORDER BY source.confidence_score DESC, source.id DESC
      LIMIT 1
    ), '') AS source_text
  FROM tools t
  LEFT JOIN categories legacy_category ON legacy_category.id = t.primary_category_id
  LEFT JOIN product_profiles profile ON profile.tool_id = t.id
  LEFT JOIN classification_runs latest_run ON latest_run.id = (
    SELECT run.id
    FROM classification_runs run
    WHERE run.tool_id = t.id
    ORDER BY run.created_at DESC, run.id DESC
    LIMIT 1
  )
  WHERE t.status IN ('pending_enrich', 'pending_review', 'published')
    AND t.duplicate_of_tool_id IS NULL
), matched AS (
  SELECT *, lower(
    localization_text || ' ' || feature_text || ' ' || profile_text || ' ' ||
    COALESCE(category_classification_raw, '') || ' ' || latest_run_text || ' ' || source_text
  ) AS detection_text
  FROM candidates
)
SELECT *
FROM matched
WHERE tool_id > ?
  AND (
  instr(detection_text, 'access denied') > 0
  OR instr(detection_text, 'access has been blocked') > 0
  OR instr(detection_text, 'just a moment') > 0
  OR instr(detection_text, 'just wait') > 0
  OR instr(detection_text, 'checking your browser') > 0
  OR instr(detection_text, 'verify you are human') > 0
  OR instr(detection_text, 'verifying you are human') > 0
  OR instr(detection_text, 'cloudflare ray id') > 0
  OR instr(detection_text, 'challenge-platform') > 0
  OR instr(detection_text, 'incapsula') > 0
  OR instr(detection_text, 'datadome') > 0
  OR instr(detection_text, 'px-captcha') > 0
  OR instr(detection_text, 'request could not be satisfied') > 0
  OR instr(detection_text, 'sucuri website firewall') > 0
  OR instr(detection_text, 'this domain is for use in documentation example') > 0
  OR instr(detection_text, 'example domain') > 0
  OR instr(detection_text, 'iana example') > 0
  OR instr(detection_text, 'avoid use in operations') > 0
  )
ORDER BY tool_id
LIMIT ?
"""


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_has_valid_product_metadata(source_text: str) -> tuple[bool, dict[str, str]]:
    raw = _json_object(source_text)
    metadata = raw.get("page_metadata")
    if not isinstance(metadata, dict):
        return False, {}
    selected: dict[str, str] = {}
    for key in ("title", "description", "h1", "openGraphDescription"):
        value = str(metadata.get(key) or "").strip()
        if value and not detect_anti_bot_text(value):
            selected[key] = value[:500]
    combined = " ".join(selected.values())
    return len(combined) >= 40, selected


def build_classification_pollution_matches(row: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for source, key in (
        ("localization", "localization_text"),
        ("features", "feature_text"),
        ("product_profile", "profile_text"),
        ("legacy_classification", "category_classification_raw"),
        ("classification_run", "latest_run_text"),
        ("official_source", "source_text"),
    ):
        value = str(row.get(key) or "")
        detected = detect_anti_bot_text(value)
        if detected:
            evidence.append(
                {
                    "source": source,
                    "state": detected.state,
                    "provider": detected.provider,
                    "code": detected.code,
                    "evidence": detected.evidence,
                    "confidence": detected.confidence,
                    "matched_codes": list(detected.matched_codes),
                }
            )
            continue
        placeholder = NEUTRAL_TRANSPORT_PLACEHOLDER_RE.search(value)
        if placeholder:
            evidence.append(
                {
                    "source": source,
                    "state": "invalid_page",
                    "provider": "neutral_transport",
                    "code": "neutral_transport_example_domain",
                    "evidence": re.sub(r"\s+", " ", placeholder.group(0)).strip()[:240],
                    "confidence": 0.99,
                    "matched_codes": ["neutral_transport_example_domain"],
                }
            )
    return evidence


def build_anti_bot_anomaly_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    evidence = build_classification_pollution_matches(row)

    if not evidence:
        return None

    neutral_transport_detected = any(
        item.get("code") == "neutral_transport_example_domain" for item in evidence
    )
    if neutral_transport_detected:
        score = 85
        signals: list[dict[str, Any]] = [
            {
                "code": "neutral_transport_page_pollution",
                "score": 85,
                "reason": "Classification evidence came from the neutral example.com transport page instead of the product homepage.",
            }
        ]
    else:
        score = 55
        signals = [
            {
                "code": "anti_bot_product_fact_pollution",
                "score": 55,
                "reason": "Stored product or classification evidence contains a known WAF/challenge signature.",
            }
        ]

    legacy_without_provenance = (
        str(row.get("assignment_decision_status") or "") == "legacy"
        or str(row.get("assignment_source") or "") == "legacy"
    ) and not str(row.get("category_classification_raw") or "").strip()
    if legacy_without_provenance:
        score += 20
        signals.append(
            {
                "code": "legacy_category_without_provenance",
                "score": 20,
                "reason": "Effective category is legacy and has no raw model output or evidence.",
            }
        )

    latest_error = str(row.get("latest_run_error") or "").lower()
    if "entity_unresolved" in latest_error or "page_invalid" in latest_error:
        score += 15
        signals.append(
            {
                "code": "new_pipeline_unresolved",
                "score": 15,
                "reason": "The newest taxonomy run could not establish a usable product entity.",
            }
        )

    valid_source_metadata, source_metadata = _source_has_valid_product_metadata(
        str(row.get("source_text") or "")
    )
    if valid_source_metadata:
        score += 15
        signals.append(
            {
                "code": "valid_discovery_metadata_conflicts_with_block_page",
                "score": 15,
                "reason": "A prior official-site discovery record contains usable product metadata.",
            }
        )

    current_slug = str(row.get("current_primary_slug") or "")
    if current_slug == "ai-security-compliance":
        score += 10
        signals.append(
            {
                "code": "security_category_may_reflect_waf_copy",
                "score": 10,
                "reason": "Security/compliance is the effective category while stored facts are a WAF page.",
            }
        )

    score = min(100, score)
    if score < 60:
        return None
    severity = "high" if score >= 80 else "medium" if score >= 65 else "low"
    return {
        "tool_id": int(row.get("tool_id") or 0),
        "detector_code": ANTI_BOT_CLASSIFICATION_DETECTOR,
        "severity": severity,
        "score": score,
        "current_primary_term_id": int(row.get("current_primary_term_id") or 0) or None,
        "current_primary_slug": current_slug or None,
        "evidence": {
            "version": 1,
            "signals": signals,
            "matches": evidence,
            "latest_run": {
                "id": int(row.get("latest_run_id") or 0) or None,
                "status": str(row.get("latest_run_status") or ""),
                "error": str(row.get("latest_run_error") or "")[:500],
            },
            "source_metadata": source_metadata,
        },
    }


async def _claim_detector_scan(d1: Any, *, lease_owner: str) -> str | None:
    lease_token = uuid.uuid4().hex
    rows = await d1.query(
        """
        INSERT INTO classification_anomaly_detector_state (
          detector_code, lease_owner, lease_token, lease_expires_at,
          last_started_at, next_scan_at, updated_at
        )
        VALUES (
          ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+30 minutes'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+6 hours'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        ON CONFLICT(detector_code) DO UPDATE SET
          lease_owner = excluded.lease_owner,
          lease_token = excluded.lease_token,
          lease_expires_at = excluded.lease_expires_at,
          last_started_at = excluded.last_started_at,
          updated_at = excluded.updated_at
        WHERE (
          classification_anomaly_detector_state.next_scan_at IS NULL
          OR classification_anomaly_detector_state.next_scan_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
          AND (
            classification_anomaly_detector_state.lease_expires_at IS NULL
            OR classification_anomaly_detector_state.lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
          )
        RETURNING lease_token
        """,
        [ANTI_BOT_CLASSIFICATION_DETECTOR, lease_owner, lease_token],
        operation="classification_anomaly.claim_scan",
    )
    return lease_token if rows else None


async def _load_detector_scan_cursor(d1: Any, lease_token: str) -> int:
    rows = await d1.query(
        """
        SELECT last_result_json
        FROM classification_anomaly_detector_state
        WHERE detector_code = ? AND lease_token = ?
        """,
        [ANTI_BOT_CLASSIFICATION_DETECTOR, lease_token],
        operation="classification_anomaly.load_cursor",
    )
    if not rows:
        return 0
    payload = _json_object(rows[0].get("last_result_json"))
    try:
        return max(0, int(payload.get("next_cursor_tool_id") or 0))
    except (TypeError, ValueError):
        return 0


async def scan_classification_anomalies(
    d1: Any,
    *,
    limit: int = ANOMALY_SCAN_LIMIT,
    lease_owner: str = "taxonomy-worker",
) -> dict[str, int]:
    counts = {"scanned": 0, "candidates": 0, "skipped": 0}
    lease_token = await _claim_detector_scan(d1, lease_owner=lease_owner)
    if not lease_token:
        counts["skipped"] = 1
        return counts

    try:
        scan_cursor_tool_id = await _load_detector_scan_cursor(d1, lease_token)
        page_limit = max(1, min(int(limit or ANOMALY_SCAN_LIMIT), 2000))
        rows = await d1.query(
            ANOMALY_SCAN_SQL,
            [scan_cursor_tool_id, page_limit],
            operation="classification_anomaly.scan",
        )
        counts["scanned"] = len(rows)
        statements: list[tuple[str, list[Any]]] = []
        for row in rows:
            candidate = build_anti_bot_anomaly_candidate(row)
            if not candidate or candidate["tool_id"] <= 0:
                continue
            counts["candidates"] += 1
            statements.append(
                (
                    """
                    INSERT INTO classification_anomaly_candidates (
                      tool_id, detector_code, severity, score, status,
                      current_primary_term_id, current_primary_slug, evidence_json,
                      first_detected_at, last_detected_at, created_at, updated_at
                    )
                    VALUES (
                      ?, ?, ?, ?, 'pending', ?, ?, ?,
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                    ON CONFLICT(tool_id, detector_code) DO UPDATE SET
                      severity = excluded.severity,
                      score = excluded.score,
                      current_primary_term_id = excluded.current_primary_term_id,
                      current_primary_slug = excluded.current_primary_slug,
                      evidence_json = excluded.evidence_json,
                      occurrence_count = classification_anomaly_candidates.occurrence_count + 1,
                      last_detected_at = excluded.last_detected_at,
                      status = CASE
                        WHEN classification_anomaly_candidates.status = 'approved' THEN 'approved'
                        WHEN classification_anomaly_candidates.status IN ('rejected', 'snoozed')
                          AND classification_anomaly_candidates.cooldown_until > strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                          THEN classification_anomaly_candidates.status
                        ELSE 'pending'
                      END,
                      resolution_request_id = CASE
                        WHEN classification_anomaly_candidates.status = 'resolved' THEN NULL
                        ELSE classification_anomaly_candidates.resolution_request_id
                      END,
                      updated_at = excluded.updated_at
                    """,
                    [
                        candidate["tool_id"],
                        candidate["detector_code"],
                        candidate["severity"],
                        candidate["score"],
                        candidate["current_primary_term_id"],
                        candidate["current_primary_slug"],
                        json.dumps(candidate["evidence"], ensure_ascii=False, separators=(",", ":")),
                    ],
                )
            )

        if statements:
            await d1.batch(statements, operation="classification_anomaly.upsert_candidates")

        next_cursor_tool_id = (
            max((int(row.get("tool_id") or 0) for row in rows), default=0)
            if len(rows) >= page_limit
            else 0
        )
        scan_result = {
            **counts,
            "scan_cursor_tool_id": scan_cursor_tool_id,
            "next_cursor_tool_id": next_cursor_tool_id,
            "wrapped": next_cursor_tool_id == 0,
        }
        await d1.run(
            """
            UPDATE classification_anomaly_detector_state
            SET lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                next_scan_at = CASE
                  WHEN ? > 0 THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+5 minutes')
                  ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+6 hours')
                END,
                last_result_json = ?,
                last_error = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE detector_code = ? AND lease_token = ?
            """,
            [
                next_cursor_tool_id,
                json.dumps(scan_result, separators=(",", ":")),
                ANTI_BOT_CLASSIFICATION_DETECTOR,
                lease_token,
            ],
            operation="classification_anomaly.complete_scan",
        )
        return counts
    except Exception as error:
        await d1.run(
            """
            UPDATE classification_anomaly_detector_state
            SET lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                next_scan_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+1 hour'),
                last_error = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE detector_code = ? AND lease_token = ?
            """,
            [str(error)[:800], ANTI_BOT_CLASSIFICATION_DETECTOR, lease_token],
            operation="classification_anomaly.fail_scan",
        )
        raise


async def load_queued_reclassification_tasks(d1: Any, *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return await d1.query(
        """
        SELECT
          request.id AS reclassification_request_id,
          request.anomaly_candidate_id,
          request.evidence_mode,
          request.evidence_url,
          request.attempts AS reclassification_attempts,
          request.max_attempts AS reclassification_max_attempts,
          tool.id AS tool_id,
          tool.canonical_slug,
          tool.normalized_domain,
          tool.official_url,
          CASE request.evidence_mode
            WHEN 'explicit_url' THEN NULLIF(request.evidence_url, '')
            WHEN 'official_url' THEN tool.official_url
            WHEN 'verified_source' THEN COALESCE((
              SELECT source.source_url
              FROM tool_sources source
              WHERE source.tool_id = tool.id
                AND source.source_type = 'official_site'
                AND source.verification_status = 'verified'
                AND json_extract(source.raw_payload, '$.taxonomy_evidence') = 1
              ORDER BY source.confidence_score DESC, source.id DESC
              LIMIT 1
            ), tool.official_url)
            ELSE COALESCE((
              SELECT source.source_url
              FROM tool_sources source
              WHERE source.tool_id = tool.id
                AND source.source_type = 'official_site'
                AND source.verification_status = 'verified'
                AND json_extract(source.raw_payload, '$.taxonomy_evidence') = 1
              ORDER BY source.confidence_score DESC, source.id DESC
              LIMIT 1
            ), tool.official_url)
          END AS taxonomy_evidence_url,
          tool.entity_kind,
          tool.entity_kind_source,
          tool.status
        FROM classification_reprocess_requests request
        JOIN tools tool ON tool.id = request.tool_id
        WHERE request.attempts < request.max_attempts
          AND (
            request.status = 'queued'
            OR (
              request.status = 'running'
              AND request.lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
          )
          AND tool.duplicate_of_tool_id IS NULL
        ORDER BY request.requested_at, request.id
        LIMIT ?
        """,
        [limit],
        operation="classification_reprocess.load_queue",
    )


async def claim_reclassification_request(
    d1: Any,
    request_id: int,
    *,
    lease_owner: str,
) -> str | None:
    lease_token = uuid.uuid4().hex
    rows = await d1.query(
        """
        UPDATE classification_reprocess_requests
        SET status = 'running',
            attempts = attempts + 1,
            lease_owner = ?,
            lease_token = ?,
            lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+1 hour'),
            started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
          AND attempts < max_attempts
          AND (
            status = 'queued'
            OR (status = 'running' AND lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
          )
        RETURNING lease_token
        """,
        [lease_owner, lease_token, request_id],
        operation="classification_reprocess.claim",
    )
    return lease_token if rows else None


async def complete_reclassification_request(
    d1: Any,
    *,
    request_id: int,
    lease_token: str,
    result: Any,
    auto_accept_threshold: float,
) -> str:
    primary_slug = str(getattr(result, "primary_slug", "") or "")
    primary_confidence = float(getattr(result, "primary_confidence", 0.0) or 0.0)
    run_status = str(getattr(result, "status", "failed") or "failed")
    run_id = int(getattr(result, "run_id", 0) or 0) or None
    error = str(getattr(result, "error", "") or "")[:800]

    if run_status == "succeeded" and primary_slug and primary_confidence >= auto_accept_threshold:
        request_status = "succeeded"
    elif run_status in {"partial", "skipped", "succeeded"}:
        request_status = "needs_manual"
    else:
        request_status = "failed"

    result_payload = {
        "status": run_status,
        "primary_slug": primary_slug or None,
        "primary_confidence": primary_confidence,
        "entity_kind": str(getattr(result, "entity_kind", "unresolved") or "unresolved"),
        "classification_run_id": run_id,
        "error": error or None,
    }
    rows = await d1.query(
        """
        UPDATE classification_reprocess_requests
        SET status = ?,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            classification_run_id = ?,
            result_json = ?,
            last_error = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND status = 'running' AND lease_token = ?
        RETURNING anomaly_candidate_id, tool_id
        """,
        [
            request_status,
            run_id,
            json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
            error or None,
            request_id,
            lease_token,
        ],
        operation="classification_reprocess.complete",
    )
    if not rows:
        return "stale"

    anomaly_id = int(rows[0].get("anomaly_candidate_id") or 0)
    tool_id = int(rows[0].get("tool_id") or 0)
    if anomaly_id > 0:
        await d1.batch(
            [
                (
                    """
                    UPDATE classification_anomaly_candidates
                    SET status = ?,
                        resolution_request_id = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    ["resolved" if request_status == "succeeded" else "approved", request_id, anomaly_id],
                ),
                (
                    """
                    INSERT INTO classification_anomaly_events (
                      anomaly_candidate_id, tool_id, action, actor, payload_json
                    ) VALUES (?, ?, ?, 'taxonomy-worker', ?)
                    """,
                    [
                        anomaly_id,
                        tool_id,
                        f"reclassification_{request_status}",
                        json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
                    ],
                ),
            ],
            operation="classification_reprocess.complete_anomaly",
        )
    return request_status


async def fail_reclassification_request(
    d1: Any,
    *,
    request_id: int,
    lease_token: str,
    error: str,
) -> str:
    rows = await d1.query(
        """
        UPDATE classification_reprocess_requests
        SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            completed_at = CASE
              WHEN attempts < max_attempts THEN completed_at
              ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            END,
            last_error = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND status = 'running' AND lease_token = ?
        RETURNING status
        """,
        [error[:800], request_id, lease_token],
        operation="classification_reprocess.fail",
    )
    return str(rows[0].get("status") or "stale") if rows else "stale"
