INSERT OR IGNORE INTO classification_reprocess_requests (
  tool_id,
  anomaly_candidate_id,
  request_source,
  evidence_mode,
  reason,
  status,
  requested_by
)
SELECT
  candidate.tool_id,
  candidate.id,
  'anomaly',
  'auto',
  'Approved exact neutral transport production repair; manual classifications protected',
  'queued',
  'codex-approved-production-repair'
FROM classification_anomaly_candidates candidate
JOIN tools tool ON tool.id = candidate.tool_id
WHERE candidate.status = 'pending'
  AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
  AND tool.status = 'published'
  AND tool.duplicate_of_tool_id IS NULL
  AND EXISTS (
    SELECT 1
    FROM json_each(candidate.evidence_json, '$.matches') matched
    WHERE json_extract(matched.value, '$.code') = 'neutral_transport_example_domain'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM classification_reprocess_requests request
    WHERE request.tool_id = candidate.tool_id
      AND request.status IN ('queued', 'running')
  )
  AND coalesce(tool.entity_kind_source, '') != 'manual'
  AND NOT EXISTS (
    SELECT 1
    FROM classification_decisions decision
    WHERE decision.tool_id = tool.id
      AND decision.dimension = 'entity_kind'
      AND decision.decision LIKE 'set_entity_kind:%'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments assignment
    WHERE assignment.tool_id = tool.id
      AND assignment.is_primary = 1
      AND assignment.decision_status = 'verified'
      AND assignment.source = 'manual'
  );

UPDATE classification_anomaly_candidates AS candidate
SET status = 'approved',
    reviewed_by = 'codex-approved-production-repair',
    reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    review_reason = 'Approved exact neutral transport production repair; manual classifications protected',
    cooldown_until = NULL,
    resolution_request_id = (
      SELECT request.id
      FROM classification_reprocess_requests request
      WHERE request.tool_id = candidate.tool_id
        AND request.status IN ('queued', 'running')
      ORDER BY request.id DESC
      LIMIT 1
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE candidate.status = 'pending'
  AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
  AND EXISTS (
    SELECT 1
    FROM json_each(candidate.evidence_json, '$.matches') matched
    WHERE json_extract(matched.value, '$.code') = 'neutral_transport_example_domain'
  )
  AND EXISTS (
    SELECT 1
    FROM classification_reprocess_requests request
    WHERE request.tool_id = candidate.tool_id
      AND request.status IN ('queued', 'running')
      AND request.requested_by = 'codex-approved-production-repair'
      AND request.reason = 'Approved exact neutral transport production repair; manual classifications protected'
  );

INSERT INTO classification_anomaly_events (
  anomaly_candidate_id,
  tool_id,
  action,
  actor,
  payload_json
)
SELECT
  candidate.id,
  candidate.tool_id,
  'production_exact_neutral_repair_queued',
  'codex-approved-production-repair',
  json_object(
    'incident', CASE WHEN EXISTS (
      SELECT 1
      FROM taxonomy_incident_members member
      WHERE member.incident_id = 'INC-2026-08-EXAMPLE-COM'
        AND member.tool_id = candidate.tool_id
        AND member.apply_status IN ('applied', 'audit_only')
    ) THEN 'INC-2026-08-EXAMPLE-COM' ELSE 'exact-signature-outside-frozen-incident' END,
    'reason', candidate.review_reason,
    'request_id', candidate.resolution_request_id
  )
FROM classification_anomaly_candidates candidate
WHERE candidate.status = 'approved'
  AND candidate.review_reason = 'Approved exact neutral transport production repair; manual classifications protected'
  AND candidate.resolution_request_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM classification_anomaly_events event
    WHERE event.anomaly_candidate_id = candidate.id
      AND event.action = 'production_exact_neutral_repair_queued'
  );
