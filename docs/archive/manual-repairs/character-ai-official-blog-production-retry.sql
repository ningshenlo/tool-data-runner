INSERT INTO classification_reprocess_requests (
  tool_id,
  anomaly_candidate_id,
  request_source,
  evidence_mode,
  evidence_url,
  reason,
  status,
  requested_by,
  requested_at
)
SELECT
  candidate.tool_id,
  candidate.id,
  'anomaly',
  'explicit_url',
  'https://blog.character.ai/creator-bundle/',
  'Approved final Character AI retry using official first-party blog evidence after help-center WAF gate',
  'queued',
  'codex-approved-production-repair',
  '2026-08-20T09:20:58.000Z'
FROM classification_anomaly_candidates candidate
JOIN tools tool ON tool.id = candidate.tool_id
WHERE candidate.id = 36
  AND candidate.tool_id = 184
  AND candidate.detector_code = 'anti_bot_classification_pollution_v1'
  AND candidate.status = 'approved'
  AND tool.status = 'published'
  AND tool.duplicate_of_tool_id IS NULL
  AND coalesce(tool.entity_kind_source, '') != 'manual'
  AND NOT EXISTS (
    SELECT 1
    FROM classification_reprocess_requests request
    WHERE request.tool_id = candidate.tool_id
      AND request.status IN ('queued', 'running')
  );

UPDATE classification_anomaly_candidates
SET status = 'approved',
    reviewed_by = 'codex-approved-production-repair',
    reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    review_reason = 'Approved final Character AI retry using official first-party blog evidence after help-center WAF gate',
    resolution_request_id = (
      SELECT request.id
      FROM classification_reprocess_requests request
      WHERE request.tool_id = 184
        AND request.status IN ('queued', 'running')
        AND request.reason = 'Approved final Character AI retry using official first-party blog evidence after help-center WAF gate'
      ORDER BY request.id DESC
      LIMIT 1
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id = 36
  AND tool_id = 184
  AND EXISTS (
    SELECT 1
    FROM classification_reprocess_requests request
    WHERE request.tool_id = 184
      AND request.status IN ('queued', 'running')
      AND request.reason = 'Approved final Character AI retry using official first-party blog evidence after help-center WAF gate'
  );

INSERT INTO classification_anomaly_events (
  anomaly_candidate_id,
  tool_id,
  action,
  actor,
  payload_json
)
SELECT
  36,
  184,
  'production_official_blog_retry_queued',
  'codex-approved-production-repair',
  json_object(
    'reason', 'Approved final Character AI retry using official first-party blog evidence after help-center WAF gate',
    'evidence_mode', 'explicit_url',
    'evidence_url', 'https://blog.character.ai/creator-bundle/',
    'request_id', candidate.resolution_request_id
  )
FROM classification_anomaly_candidates candidate
WHERE candidate.id = 36
  AND candidate.resolution_request_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM classification_anomaly_events event
    WHERE event.anomaly_candidate_id = 36
      AND event.action = 'production_official_blog_retry_queued'
  );
