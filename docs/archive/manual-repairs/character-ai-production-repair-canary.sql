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
  'Approved production canary retry after grounded evidence normalization',
  'queued',
  'codex-approved-production-repair'
FROM classification_anomaly_candidates candidate
WHERE candidate.id = 94
  AND candidate.tool_id = 678
  AND candidate.status IN ('pending', 'approved')
  AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
  AND EXISTS (
    SELECT 1
    FROM json_each(candidate.evidence_json, '$.matches') matched
    WHERE json_extract(matched.value, '$.code') = 'neutral_transport_example_domain'
  );

UPDATE classification_anomaly_candidates
SET status = 'approved',
    reviewed_by = 'codex-approved-production-repair',
    reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    review_reason = 'Approved production canary retry after grounded evidence normalization',
    cooldown_until = NULL,
    resolution_request_id = (
      SELECT request.id
      FROM classification_reprocess_requests request
      WHERE request.tool_id = 678
        AND request.status IN ('queued', 'running')
      ORDER BY request.id DESC
      LIMIT 1
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id = 94
  AND status IN ('pending', 'approved')
  AND EXISTS (
    SELECT 1
    FROM classification_reprocess_requests request
    WHERE request.tool_id = 678
      AND request.status IN ('queued', 'running')
  );

INSERT INTO classification_anomaly_events (
  anomaly_candidate_id,
  tool_id,
  action,
  actor,
  payload_json
)
SELECT
  94,
  678,
  'production_repair_canary_retry_queued',
  'codex-approved-production-repair',
  json_object(
    'incident', 'neutral-transport-exact-signature',
    'reason', 'Approved production canary retry after grounded evidence normalization',
    'request_id', candidate.resolution_request_id
  )
FROM classification_anomaly_candidates candidate
WHERE candidate.id = 94
  AND candidate.status = 'approved'
  AND NOT EXISTS (
    SELECT 1
    FROM classification_anomaly_events event
    WHERE event.anomaly_candidate_id = 94
      AND event.action = 'production_repair_canary_retry_queued'
  );
