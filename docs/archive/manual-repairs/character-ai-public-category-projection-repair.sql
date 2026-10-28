INSERT INTO tool_change_log (
  tool_id,
  change_type,
  old_value,
  new_value,
  verified_at,
  notes
)
SELECT
  184,
  'category_changed',
  json_object('category_slug', 'ai-security-compliance', 'category_id', 48),
  json_object(
    'category_slug', 'games-entertainment-lifestyle',
    'category_id', 46,
    'taxonomy_primary_slug', 'entertainment-companionship',
    'taxonomy_primary_term_id', 298,
    'legacy_projection', 'parent_only',
    'leaf_source_category_id', NULL
  ),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  'Approved production repair by codex-approved-production-repair; taxonomy leaf 298 has no legacy source_category_id, so the public compatibility projection uses mapped parent term 26/category 46.'
WHERE EXISTS (
  SELECT 1
  FROM product_taxonomy_assignments assignment
  WHERE assignment.tool_id = 184
    AND assignment.term_id = 298
    AND assignment.is_primary = 1
    AND assignment.decision_status = 'verified'
    AND assignment.source = 'manual'
)
AND NOT EXISTS (
  SELECT 1
  FROM tool_change_log change
  WHERE change.tool_id = 184
    AND change.change_type = 'category_changed'
    AND instr(coalesce(change.notes, ''), 'taxonomy leaf 298 has no legacy source_category_id') > 0
);

DELETE FROM tool_categories
WHERE tool_id = 184
  AND EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments assignment
    WHERE assignment.tool_id = 184
      AND assignment.term_id = 298
      AND assignment.is_primary = 1
      AND assignment.decision_status = 'verified'
      AND assignment.source = 'manual'
  );

INSERT INTO tool_categories (
  tool_id,
  category_id,
  source,
  raw_output,
  classified_at
)
SELECT
  184,
  46,
  'manual',
  json_object(
    'actor', 'codex-approved-production-repair',
    'source', 'manual_verified_taxonomy_projection',
    'taxonomy_primary_term_id', 298,
    'taxonomy_primary_slug', 'entertainment-companionship',
    'taxonomy_parent_term_id', 26,
    'taxonomy_parent_slug', 'games-entertainment-lifestyle',
    'legacy_projection', 'parent_only',
    'reason', 'taxonomy leaf 298 has no legacy source_category_id'
  ),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE EXISTS (
  SELECT 1
  FROM categories category
  WHERE category.id = 46
    AND category.canonical_slug = 'games-entertainment-lifestyle'
    AND category.status = 'active'
)
AND EXISTS (
  SELECT 1
  FROM product_taxonomy_assignments assignment
  WHERE assignment.tool_id = 184
    AND assignment.term_id = 298
    AND assignment.is_primary = 1
    AND assignment.decision_status = 'verified'
    AND assignment.source = 'manual'
);

UPDATE tools
SET primary_category_id = 46,
    category_classification_status = 'manual_ok',
    category_classification_last_error = NULL,
    category_classification_raw = json_object(
      'actor', 'codex-approved-production-repair',
      'source', 'manual_verified_taxonomy_projection',
      'taxonomy_primary_term_id', 298,
      'taxonomy_primary_slug', 'entertainment-companionship',
      'legacy_projection', 'parent_only',
      'reason', 'taxonomy leaf 298 has no legacy source_category_id'
    ),
    category_classification_updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id = 184
  AND EXISTS (
    SELECT 1
    FROM tool_categories category
    WHERE category.tool_id = 184
      AND category.category_id = 46
      AND category.source = 'manual'
  );

INSERT INTO tool_category_classification_events (
  tool_id,
  outcome,
  category_l1_slug,
  category_l2_slug,
  model_name,
  prompt_version,
  taxonomy_version,
  raw_output,
  error
)
SELECT
  184,
  'manual',
  'games-entertainment-lifestyle',
  NULL,
  'manual',
  'codex-production-repair-v1',
  2,
  json_object(
    'actor', 'codex-approved-production-repair',
    'taxonomy_primary_term_id', 298,
    'taxonomy_primary_slug', 'entertainment-companionship',
    'legacy_projection', 'parent_only',
    'reason', 'taxonomy leaf 298 has no legacy source_category_id'
  ),
  NULL
WHERE EXISTS (
  SELECT 1
  FROM tools tool
  WHERE tool.id = 184
    AND tool.primary_category_id = 46
)
AND NOT EXISTS (
  SELECT 1
  FROM tool_category_classification_events event
  WHERE event.tool_id = 184
    AND event.prompt_version = 'codex-production-repair-v1'
    AND event.category_l1_slug = 'games-entertainment-lifestyle'
);
