-- Backfill legacy category links created after migration 0032 into the
-- canonical taxonomy assignment table.
--
-- This migration never replaces a verified/auto-accepted/legacy primary.
-- It only gives tools with no accepted primary a temporary legacy primary so
-- the OpenAI taxonomy worker can subsequently replace it with a trusted result.

-- Preserve every mapped legacy category as a non-primary taxonomy assignment.
INSERT OR IGNORE INTO product_taxonomy_assignments (
  tool_id,
  term_id,
  run_id,
  is_primary,
  confidence,
  decision_status,
  source,
  evidence_json,
  assigned_at,
  updated_at
)
SELECT
  tc.tool_id,
  term.id,
  NULL,
  0,
  NULL,
  'legacy',
  'legacy',
  json_object(
    'migration', '0089',
    'source', 'tool_categories',
    'source_category_id', tc.category_id
  ),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM tool_categories tc
JOIN tools tool ON tool.id = tc.tool_id
JOIN taxonomy_terms term
  ON term.source_category_id = tc.category_id
 AND term.dimension = 'primary_category'
 AND term.status = 'active'
WHERE tool.status IN ('pending_enrich', 'pending_review', 'published')
  AND tool.duplicate_of_tool_id IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments accepted_primary
    JOIN taxonomy_terms accepted_term
      ON accepted_term.id = accepted_primary.term_id
     AND accepted_term.dimension = 'primary_category'
     AND accepted_term.status = 'active'
    WHERE accepted_primary.tool_id = tool.id
      AND accepted_primary.is_primary = 1
      AND accepted_primary.decision_status IN (
        'verified', 'auto_accepted', 'legacy'
      )
  );

-- Keep tools.primary_category_id as a fallback when no tool_categories row
-- exists for it. It remains non-primary until the guarded promotion below.
INSERT OR IGNORE INTO product_taxonomy_assignments (
  tool_id,
  term_id,
  run_id,
  is_primary,
  confidence,
  decision_status,
  source,
  evidence_json,
  assigned_at,
  updated_at
)
SELECT
  tool.id,
  term.id,
  NULL,
  0,
  NULL,
  'legacy',
  'legacy',
  json_object(
    'migration', '0089',
    'source', 'tools.primary_category_id',
    'source_category_id', tool.primary_category_id
  ),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM tools tool
JOIN taxonomy_terms term
  ON term.source_category_id = tool.primary_category_id
 AND term.dimension = 'primary_category'
 AND term.status = 'active'
WHERE tool.status IN ('pending_enrich', 'pending_review', 'published')
  AND tool.duplicate_of_tool_id IS NULL
  AND tool.primary_category_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM product_taxonomy_assignments accepted_primary
    JOIN taxonomy_terms accepted_term
      ON accepted_term.id = accepted_primary.term_id
     AND accepted_term.dimension = 'primary_category'
     AND accepted_term.status = 'active'
    WHERE accepted_primary.tool_id = tool.id
      AND accepted_primary.is_primary = 1
      AND accepted_primary.decision_status IN (
        'verified', 'auto_accepted', 'legacy'
      )
  );

-- Promote exactly one legacy assignment only for tools that still have no
-- accepted primary. Prefer a human-linked category, then a leaf, then the
-- legacy primary_category_id fallback.
WITH ranked_legacy_candidates AS (
  SELECT
    assignment.id AS assignment_id,
    assignment.tool_id,
    ROW_NUMBER() OVER (
      PARTITION BY assignment.tool_id
      ORDER BY
        CASE WHEN EXISTS (
          SELECT 1
          FROM tool_categories manual_link
          WHERE manual_link.tool_id = assignment.tool_id
            AND manual_link.category_id = term.source_category_id
            AND manual_link.source = 'manual'
        ) THEN 0 ELSE 1 END,
        CASE WHEN term.parent_id IS NOT NULL THEN 0 ELSE 1 END,
        CASE WHEN tool.primary_category_id = term.source_category_id THEN 0 ELSE 1 END,
        term.display_order,
        term.id
    ) AS candidate_rank
  FROM product_taxonomy_assignments assignment
  JOIN taxonomy_terms term
    ON term.id = assignment.term_id
   AND term.dimension = 'primary_category'
   AND term.status = 'active'
  JOIN tools tool ON tool.id = assignment.tool_id
  WHERE assignment.source = 'legacy'
    AND assignment.decision_status = 'legacy'
    AND tool.status IN ('pending_enrich', 'pending_review', 'published')
    AND tool.duplicate_of_tool_id IS NULL
    AND NOT EXISTS (
      SELECT 1
      FROM product_taxonomy_assignments accepted_primary
      JOIN taxonomy_terms accepted_term
        ON accepted_term.id = accepted_primary.term_id
       AND accepted_term.dimension = 'primary_category'
       AND accepted_term.status = 'active'
      WHERE accepted_primary.tool_id = assignment.tool_id
        AND accepted_primary.is_primary = 1
        AND accepted_primary.decision_status IN (
          'verified', 'auto_accepted', 'legacy'
        )
    )
)
UPDATE product_taxonomy_assignments
SET is_primary = 1,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id IN (
  SELECT assignment_id
  FROM ranked_legacy_candidates
  WHERE candidate_rank = 1
);

INSERT INTO taxonomy_change_log (actor, action, payload_json)
VALUES (
  'migration:0089',
  'backfill_post_seed_legacy_taxonomy',
  json_object(
    'source', 'legacy_category_projection',
    'trusted_primary_policy', 'preserve',
    'note', 'temporary legacy primaries remain eligible for OpenAI reclassification'
  )
);
