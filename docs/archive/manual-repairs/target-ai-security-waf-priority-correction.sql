UPDATE classification_reprocess_requests
SET requested_at = '2026-08-20T09:21:00.000Z',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE reason = 'Approved target-category WAF pollution production repair; manual classifications protected'
  AND requested_by = 'codex-approved-production-repair'
  AND status = 'queued'
  AND attempts = 0;

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
  'production_target_waf_repair_prioritized',
  'codex-approved-production-repair',
  json_object(
    'reason', 'Prioritize the seven user-visible AI Security WAF repairs ahead of the approved bulk queue',
    'previous_requested_at', '2026-08-20T09:24:00.000Z',
    'requested_at', request.requested_at,
    'request_id', request.id
  )
FROM classification_anomaly_candidates candidate
JOIN classification_reprocess_requests request
  ON request.id = candidate.resolution_request_id
WHERE candidate.review_reason = 'Approved target-category WAF pollution production repair; manual classifications protected'
  AND request.requested_at = '2026-08-20T09:21:00.000Z'
  AND NOT EXISTS (
    SELECT 1
    FROM classification_anomaly_events event
    WHERE event.anomaly_candidate_id = candidate.id
      AND event.action = 'production_target_waf_repair_prioritized'
  );
