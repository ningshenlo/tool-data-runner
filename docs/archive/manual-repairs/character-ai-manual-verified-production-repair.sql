INSERT INTO product_taxonomy_assignments (
  tool_id,
  term_id,
  run_id,
  is_primary,
  confidence,
  decision_status,
  source,
  evidence_json,
  assigned_at,
  reviewed_at,
  updated_at
)
SELECT
  184,
  term.id,
  7003,
  1,
  1.0,
  'verified',
  'manual',
  json_object(
    'reviewer', 'codex-approved-production-repair',
    'review_basis', 'Official first-party Character.ai product evidence and taxonomy definition review',
    'official_url', 'https://blog.character.ai/creator-bundle/',
    'support_url', 'https://support.character.ai/hc/en-us/articles/14997389547931-What-is-Character-AI',
    'classification_run_id', 7003,
    'classification_run_candidate_kind', 'product_module',
    'classification_run_entity_confidence', 0.72,
    'manual_entity_resolution', 'The evidence describes features of the Character.ai platform; tool 184 represents the platform, not the article or feature bundle.',
    'taxonomy_reason', 'The active leaf explicitly includes Character Chat, Roleplay, and AI Companion.'
  ),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM taxonomy_terms term
WHERE term.id = 298
  AND term.dimension = 'primary_category'
  AND term.slug = 'entertainment-companionship'
  AND term.status = 'active'
  AND term.parent_id IS NOT NULL
  AND term.taxonomy_version = (
    SELECT MAX(latest.taxonomy_version)
    FROM taxonomy_terms latest
    WHERE latest.dimension = 'primary_category'
      AND latest.status = 'active'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM taxonomy_terms child
    WHERE child.parent_id = term.id
      AND child.dimension = 'primary_category'
      AND child.status = 'active'
      AND child.taxonomy_version = term.taxonomy_version
  )
ON CONFLICT(tool_id, term_id) DO UPDATE SET
  run_id = excluded.run_id,
  is_primary = 1,
  confidence = 1.0,
  decision_status = 'verified',
  source = 'manual',
  evidence_json = excluded.evidence_json,
  reviewed_at = excluded.reviewed_at,
  assigned_at = excluded.assigned_at,
  updated_at = excluded.updated_at;

UPDATE product_taxonomy_assignments
SET is_primary = 0,
    decision_status = CASE
      WHEN decision_status IN ('provisional', 'auto_accepted', 'unresolved') THEN 'superseded'
      ELSE decision_status
    END,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE tool_id = 184
  AND term_id <> 298
  AND is_primary = 1
  AND EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments verified
    WHERE verified.tool_id = 184
      AND verified.term_id = 298
      AND verified.is_primary = 1
      AND verified.decision_status = 'verified'
      AND verified.source = 'manual'
  );

UPDATE tools
SET entity_kind = 'independent_product',
    entity_kind_source = 'manual',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id = 184
  AND EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments verified
    WHERE verified.tool_id = 184
      AND verified.term_id = 298
      AND verified.is_primary = 1
      AND verified.decision_status = 'verified'
      AND verified.source = 'manual'
  );

INSERT INTO classification_decisions (
  tool_id,
  assignment_id,
  previous_term_id,
  decided_term_id,
  dimension,
  decision,
  reviewer_id,
  reason
)
SELECT
  184,
  NULL,
  NULL,
  NULL,
  'entity_kind',
  'set_entity_kind:independent_product',
  'codex-approved-production-repair',
  'Official Character.ai evidence identifies an independent AI entertainment platform; run 7003 described the article as a product module, not the represented tool.'
WHERE EXISTS (
  SELECT 1
  FROM product_taxonomy_assignments verified
  WHERE verified.tool_id = 184
    AND verified.term_id = 298
    AND verified.is_primary = 1
    AND verified.decision_status = 'verified'
    AND verified.source = 'manual'
)
AND NOT EXISTS (
  SELECT 1
  FROM classification_decisions decision
  WHERE decision.tool_id = 184
    AND decision.dimension = 'entity_kind'
    AND decision.decision = 'set_entity_kind:independent_product'
    AND decision.reviewer_id = 'codex-approved-production-repair'
);

INSERT INTO classification_decisions (
  tool_id,
  assignment_id,
  previous_term_id,
  decided_term_id,
  dimension,
  decision,
  reviewer_id,
  reason
)
SELECT
  184,
  assignment.id,
  28,
  298,
  'primary_category',
  'set_primary',
  'codex-approved-production-repair',
  'Official Character.ai evidence matches taxonomy leaf entertainment-companionship, whose definition includes Character Chat, Roleplay, and AI Companion.'
FROM product_taxonomy_assignments assignment
WHERE assignment.tool_id = 184
  AND assignment.term_id = 298
  AND assignment.is_primary = 1
  AND assignment.decision_status = 'verified'
  AND assignment.source = 'manual'
  AND NOT EXISTS (
    SELECT 1
    FROM classification_decisions decision
    WHERE decision.tool_id = 184
      AND decision.dimension = 'primary_category'
      AND decision.decision = 'set_primary'
      AND decision.decided_term_id = 298
      AND decision.reviewer_id = 'codex-approved-production-repair'
  );

UPDATE classification_anomaly_candidates
SET status = 'resolved',
    reviewed_by = 'codex-approved-production-repair',
    reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    review_reason = 'Manual verified production repair from official Character.ai evidence; primary=entertainment-companionship',
    cooldown_until = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id = 36
  AND tool_id = 184
  AND EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments verified
    WHERE verified.tool_id = 184
      AND verified.term_id = 298
      AND verified.is_primary = 1
      AND verified.decision_status = 'verified'
      AND verified.source = 'manual'
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
  'production_manual_verified_repair_applied',
  'codex-approved-production-repair',
  json_object(
    'previous_primary_term_id', 28,
    'previous_primary_slug', 'ai-security-compliance',
    'decided_primary_term_id', 298,
    'decided_primary_slug', 'entertainment-companionship',
    'entity_kind', 'independent_product',
    'classification_run_id', 7003,
    'official_evidence_url', 'https://blog.character.ai/creator-bundle/'
  )
WHERE EXISTS (
  SELECT 1
  FROM product_taxonomy_assignments verified
  WHERE verified.tool_id = 184
    AND verified.term_id = 298
    AND verified.is_primary = 1
    AND verified.decision_status = 'verified'
    AND verified.source = 'manual'
)
AND NOT EXISTS (
  SELECT 1
  FROM classification_anomaly_events event
  WHERE event.anomaly_candidate_id = 36
    AND event.action = 'production_manual_verified_repair_applied'
);
