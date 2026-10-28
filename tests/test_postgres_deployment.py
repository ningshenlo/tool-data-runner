"""Offline checks that production database selection and query catalog coverage fail closed."""
import os
import unittest
from unittest.mock import patch

from postgres_legacy import AINAV_DATABASE_ID, transport_for, LegacyPostgresClient
import runner
from runner import (
    tool_live_ready_predicate,
    tool_auto_publish_admission_predicate,
    accepted_primary_taxonomy_predicate,
    ASSET_DB_STORAGE_BUCKET,
)


class PostgresDeploymentTests(unittest.TestCase):
    def test_postgres_backend_requires_connection_instead_of_falling_back(self):
        with patch.dict(os.environ, {'DATABASE_BACKEND': 'postgres', 'DATABASE_URL': ''}):
            with self.assertRaisesRegex(ValueError, 'no fallback to D1'):
                transport_for(AINAV_DATABASE_ID)

    def test_postgres_backend_rejects_other_databases(self):
        with patch.dict(os.environ, {'DATABASE_BACKEND': 'postgres'}):
            with self.assertRaisesRegex(ValueError, 'scoped to ainav'):
                transport_for('other-database')

    def test_postgres_backend_selects_pg_transport(self):
        url = 'postgresql://ci:ci@postgres.invalid/sigpik?sslmode=verify-full'
        with patch.dict(os.environ, {'DATABASE_BACKEND': 'postgres', 'DATABASE_URL': url}), patch('postgres_legacy.LegacyPostgresClient') as client:
            self.assertIs(transport_for(AINAV_DATABASE_ID), client.return_value)
            client.assert_called_once_with(url)

    def test_pipeline_queries_covered_by_postgres_catalog(self):
        client = LegacyPostgresClient('postgresql://ci:ci@postgres.invalid/sigpik')
        predicate = f"({tool_live_ready_predicate('t')}) AND ({tool_auto_publish_admission_predicate('t')})"

        queries = [
            (
                f"""
                SELECT t.id
                FROM tools t
                WHERE t.status = 'pending_review'
                  AND t.duplicate_of_tool_id IS NULL
                  AND ({predicate})
                ORDER BY t.id
                LIMIT ?
                """,
                [10],
            ),
            (
                f"""
                INSERT INTO tool_change_log (
                  tool_id, change_type, old_value, new_value, verified_at, notes
                )
                SELECT
                  t.id,
                  'status_changed',
                  ?,
                  ?,
                  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                  ?
                FROM tools t
                WHERE t.id IN (?, ?)
                  AND t.status = 'pending_review'
                  AND t.duplicate_of_tool_id IS NULL
                  AND ({predicate})
                """,
                ['{"status":"pending_review"}', '{"status":"published"}', 'Automated catalog publish', 101, 102],
            ),
            (
                f"""
                UPDATE tools
                SET status = 'published',
                    first_published_at = coalesce(
                      first_published_at,
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id IN (?)
                  AND status = 'pending_review'
                  AND duplicate_of_tool_id IS NULL
                  AND ({tool_live_ready_predicate('tools')})
                  AND ({tool_auto_publish_admission_predicate('tools')})
                RETURNING id
                """,
                [101],
            ),
            (
                f"""
                SELECT t.id AS tool_id
                FROM tools t
                LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
                WHERE t.status IN ('pending_enrich', 'pending_review', 'published')
                  AND t.duplicate_of_tool_id IS NULL
                  AND (
                    state.evaluated_at IS NULL
                    OR t.updated_at > state.evaluated_at
                    OR instr(coalesce(state.blocking_json, ''), '"screenshot"') > 0
                    OR instr(coalesce(state.blocking_json, ''), '"published_localization"') > 0
                    OR instr(coalesce(state.blocking_json, ''), '"key_feature"') > 0
                    OR instr(coalesce(state.blocking_json, ''), '"key_features"') > 0
                    OR (
                      instr(coalesce(state.blocking_json, ''), '"category"') > 0
                      AND {accepted_primary_taxonomy_predicate('t')}
                    )
                  )
                ORDER BY CASE WHEN t.status = 'pending_enrich' THEN 0 ELSE 1 END,
                         CASE WHEN state.evaluated_at IS NULL THEN 0 ELSE 1 END,
                         state.evaluated_at,
                         t.id
                LIMIT ?
                """,
                [25],
            ),
            (
                f"""
                SELECT
                  t.id,
                  state.readiness AS previous_readiness,
                  CASE WHEN t.content_safety_status = 'safe' THEN 1 ELSE 0 END AS has_content_safety,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM tool_assets a
                    WHERE a.tool_id = t.id AND a.asset_kind = 'screenshot' AND a.is_current = 1
                  ) THEN 1 ELSE 0 END AS has_screenshot,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM tool_assets a
                    WHERE a.tool_id = t.id AND a.asset_kind = 'favicon' AND a.is_current = 1
                  ) THEN 1 ELSE 0 END AS has_favicon,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM tool_localizations l
                    WHERE l.tool_id = t.id
                      AND trim(l.name) <> ''
                      AND trim(coalesce(l.short_description, '')) <> ''
                  ) THEN 1 ELSE 0 END AS has_localization,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM tool_localizations l
                    WHERE l.tool_id = t.id
                      AND trim(l.name) <> ''
                      AND coalesce(l.name_review_status, 'legacy_unreviewed') NOT IN ('needs_review', 'rejected')
                      AND (
                        length(trim(l.name)) <= 80
                        OR (l.name_review_status = 'human_approved' AND length(trim(l.name)) <= 120)
                      )
                      AND instr(l.name, '<') = 0
                      AND instr(l.name, '>') = 0
                      AND instr(l.name, '`') = 0
                      AND instr(lower(l.name), 'json-ld') = 0
                  ) THEN 1 ELSE 0 END AS has_name_quality,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM product_taxonomy_assignments assignment
                    JOIN taxonomy_terms term
                      ON term.id = assignment.term_id
                     AND term.dimension = 'primary_category'
                     AND term.status = 'active'
                    WHERE assignment.tool_id = t.id
                      AND assignment.is_primary = 1
                      AND assignment.decision_status IN ('verified', 'auto_accepted')
                      AND (
                        assignment.decision_status = 'verified'
                        OR coalesce(assignment.confidence, 0) >= 0.5
                      )
                  ) THEN 1 ELSE 0 END AS has_category,
                  CASE WHEN NOT EXISTS (
                    SELECT 1
                    FROM product_taxonomy_assignments assignment
                    JOIN taxonomy_terms term
                      ON term.id = assignment.term_id
                     AND term.dimension = 'primary_category'
                     AND term.status = 'active'
                    WHERE assignment.tool_id = t.id
                      AND assignment.is_primary = 1
                      AND assignment.decision_status IN ('verified', 'auto_accepted')
                      AND (
                        assignment.decision_status = 'verified'
                        OR coalesce(assignment.confidence, 0) >= 0.5
                      )
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM product_taxonomy_assignments pending_assignment
                    JOIN taxonomy_terms pending_term
                      ON pending_term.id = pending_assignment.term_id
                    WHERE pending_assignment.tool_id = t.id
                      AND pending_assignment.is_primary = 1
                      AND pending_assignment.decision_status IN ('provisional', 'unresolved')
                      AND pending_term.dimension = 'primary_category'
                      AND pending_term.status = 'active'
                  ) THEN 1 ELSE 0 END AS category_needs_manual,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM tool_sources s WHERE s.tool_id = t.id
                  ) THEN 1 ELSE 0 END AS has_source,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM domain_traffic_monthly tm
                    WHERE tm.normalized_domain = t.normalized_domain
                      AND tm.source = ?
                  ) THEN 1 ELSE 0 END AS has_traffic,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM domain_states ds
                    WHERE ds.normalized_domain = t.normalized_domain
                      AND ds.source = ?
                      AND ds.last_crawled_at IS NOT NULL
                  ) THEN 1 ELSE 0 END AS has_domain_state
                FROM tools t
                LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
                WHERE t.id = ?
                LIMIT 1
                """,
                ['similarweb', 'ahrefs', 101],
            ),
            (
                """
                SELECT
                  t.id AS tool_id,
                  t.canonical_slug,
                  t.normalized_domain,
                  t.official_url
                FROM tools t
                LEFT JOIN asset_tasks task
                  ON task.tool_id = t.id
                 AND task.source = ?
                WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
                  AND t.duplicate_of_tool_id IS NULL
                  AND trim(t.normalized_domain) <> ''
                  AND (
                    (t.status IN ('pending_enrich', 'pending_review') AND t.content_safety_status <> 'safe')
                    OR NOT EXISTS (
                      SELECT 1
                      FROM tool_assets ta
                      WHERE ta.tool_id = t.id
                        AND ta.asset_kind = 'favicon'
                        AND ta.is_current = 1
                    )
                    OR NOT EXISTS (
                      SELECT 1
                      FROM tool_localizations tl
                      WHERE tl.tool_id = t.id
                        AND trim(tl.name) <> ''
                        AND trim(coalesce(tl.short_description, '')) <> ''
                    )
                  )
                  AND (
                    task.tool_id IS NULL
                    OR (
                      task.dead_letter_at IS NULL
                      AND task.status IN ('failed', 'sync_failed')
                      AND task.attempts < task.max_attempts
                      AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?)
                    )
                    OR (task.status = 'queued' AND task.dead_letter_at IS NULL)
                    OR (
                      task.status = 'processing'
                      AND task.dead_letter_at IS NULL
                      AND task.lease_expires_at IS NOT NULL
                      AND task.lease_expires_at <= ?
                    )
                    OR (
                      task.status = 'done'
                      AND task.updated_at < ?
                    )
                  )
                ORDER BY t.id ASC
                LIMIT ?
                """,
                ['site_scraper', '2026-10-28T00:00:00Z', '2026-10-28T00:00:00Z', '2026-10-27T00:00:00Z', 25],
            ),
            (
                """
                SELECT
                  CASE WHEN EXISTS (
                    SELECT 1 FROM tools t
                    WHERE t.id = ? AND t.content_safety_status = 'safe'
                  ) THEN 1 ELSE 0 END AS has_content_safety,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM tool_assets ta
                    WHERE ta.tool_id = ?
                      AND ta.asset_kind = 'screenshot'
                      AND ta.storage_bucket = ?
                      AND ta.is_current = 1
                  ) THEN 1 ELSE 0 END AS has_screenshot,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM tool_assets ta
                    WHERE ta.tool_id = ?
                      AND ta.asset_kind = 'favicon'
                      AND ta.is_current = 1
                  ) THEN 1 ELSE 0 END AS has_favicon,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM tool_localizations tl
                    WHERE tl.tool_id = ?
                      AND trim(tl.name) <> ''
                      AND trim(coalesce(tl.short_description, '')) <> ''
                  ) THEN 1 ELSE 0 END AS has_description
                """,
                [101, 101, ASSET_DB_STORAGE_BUCKET, 101, 101],
            ),
        ]

        for sql, params in queries:
            entry, prepared_sql, param_dict = client.prepare(sql, params)
            self.assertIsNotNone(entry)
            self.assertIn('%(p1)s', prepared_sql)
            self.assertEqual(len(param_dict), len(params))
