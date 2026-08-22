import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

from classification_anomalies import (
    ANTI_BOT_CLASSIFICATION_DETECTOR,
    claim_reclassification_request,
    complete_reclassification_request,
    load_queued_reclassification_tasks,
    scan_classification_anomalies,
)


class SQLiteD1:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    async def query(self, sql, params=None, *, operation=None):
        cursor = self.connection.execute(sql, params or [])
        if not cursor.description:
            return []
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    async def run(self, sql, params=None, *, operation=None):
        before = self.connection.total_changes
        cursor = self.connection.execute(sql, params or [])
        self.connection.commit()
        return {
            "changes": self.connection.total_changes - before,
            "last_row_id": cursor.lastrowid,
        }

    async def batch(self, statements, *, operation=None):
        results = []
        with self.connection:
            for sql, params in statements:
                before = self.connection.total_changes
                cursor = self.connection.execute(sql, params or [])
                results.append(
                    {
                        "changes": self.connection.total_changes - before,
                        "last_row_id": cursor.lastrowid,
                    }
                )
        return results


def create_store() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE categories (id INTEGER PRIMARY KEY, canonical_slug TEXT NOT NULL);
        CREATE TABLE tools (
          id INTEGER PRIMARY KEY,
          canonical_slug TEXT NOT NULL,
          normalized_domain TEXT NOT NULL,
          official_url TEXT NOT NULL,
          primary_category_id INTEGER,
          category_classification_status TEXT,
          category_classification_raw TEXT,
          entity_kind TEXT,
          entity_kind_source TEXT,
          status TEXT NOT NULL,
          duplicate_of_tool_id INTEGER
        );
        CREATE TABLE taxonomy_terms (
          id INTEGER PRIMARY KEY,
          dimension TEXT NOT NULL,
          slug TEXT NOT NULL
        );
        CREATE TABLE product_taxonomy_assignments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tool_id INTEGER NOT NULL,
          term_id INTEGER NOT NULL,
          is_primary INTEGER NOT NULL,
          decision_status TEXT NOT NULL,
          source TEXT NOT NULL
        );
        CREATE TABLE tool_localizations (
          tool_id INTEGER NOT NULL,
          locale_code TEXT NOT NULL,
          name TEXT,
          tagline TEXT,
          short_description TEXT,
          long_description TEXT,
          feature_highlights TEXT,
          translation_status TEXT NOT NULL
        );
        CREATE TABLE tool_key_features (
          tool_id INTEGER NOT NULL,
          feature_name TEXT NOT NULL,
          feature_description TEXT
        );
        CREATE TABLE product_profiles (tool_id INTEGER PRIMARY KEY, profile_json TEXT);
        CREATE TABLE classification_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tool_id INTEGER NOT NULL,
          run_status TEXT NOT NULL,
          error TEXT,
          raw_output TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE tool_sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tool_id INTEGER NOT NULL,
          source_type TEXT NOT NULL,
          source_url TEXT NOT NULL,
          verification_status TEXT NOT NULL,
          confidence_score REAL NOT NULL,
          raw_payload TEXT
        );
        """
    )
    migration = (
        Path(__file__).resolve().parent.parent
        / "sigpik"
        / "d1"
        / "migrations"
        / "0062_classification_anomaly_reprocessing.sql"
    ).read_text(encoding="utf-8")
    connection.executescript(migration)
    return connection


class ClassificationAnomalyStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = create_store()
        self.d1 = SQLiteD1(self.connection)
        self.connection.executescript(
            """
            INSERT INTO categories (id, canonical_slug) VALUES (28, 'ai-security-compliance');
            INSERT INTO taxonomy_terms (id, dimension, slug)
              VALUES (280, 'primary_category', 'ai-security-compliance');
            INSERT INTO tools (
              id, canonical_slug, normalized_domain, official_url, primary_category_id,
              category_classification_status, category_classification_raw,
              entity_kind, entity_kind_source, status, duplicate_of_tool_id
            ) VALUES (
              318, 'janitorai', 'janitorai.com', 'https://janitorai.com/', 28,
              'legacy', NULL, 'unresolved', 'auto', 'published', NULL
            );
            INSERT INTO product_taxonomy_assignments (
              tool_id, term_id, is_primary, decision_status, source
            ) VALUES (318, 280, 1, 'legacy', 'legacy');
            INSERT INTO tool_localizations (
              tool_id, locale_code, name, tagline, short_description,
              long_description, feature_highlights, translation_status
            ) VALUES (
              318, 'en', 'JanitorAI', 'Access has been blocked by the firewall',
              'Access denied', 'Security measures are being improved.',
              'Malicious traffic blocking', 'published'
            );
            INSERT INTO product_profiles (tool_id, profile_json)
              VALUES (318, '{"entity_decision":{"kind":"unresolved"}}');
            INSERT INTO classification_runs (
              tool_id, run_status, error, raw_output, created_at
            ) VALUES (
              318, 'partial', 'entity_unresolved', '{"error":"page_invalid"}',
              '2026-08-14T00:00:00.000Z'
            );
            INSERT INTO tool_sources (
              tool_id, source_type, source_url, verification_status, confidence_score, raw_payload
            ) VALUES (
              318, 'official_site', 'https://clean.example/janitorai', 'verified', 0.99,
              '{"taxonomy_evidence":1,"page_metadata":{"title":"Janitor - Build, share, and explore","description":"A platform for creators building immersive worlds and readers seeking living stories."}}'
            );
            """
        )

    async def asyncTearDown(self):
        self.connection.close()

    async def test_detection_requires_review_then_classification_only_request_runs(self):
        counts = await scan_classification_anomalies(self.d1, limit=20, lease_owner="test-worker")
        self.assertEqual(counts, {"scanned": 1, "candidates": 1, "skipped": 0})

        candidate = self.connection.execute(
            "SELECT id, score, severity, status, evidence_json FROM classification_anomaly_candidates"
        ).fetchone()
        self.assertIsNotNone(candidate)
        candidate_id, score, severity, status, evidence_json = candidate
        self.assertEqual((score, severity, status), (100, "high", "pending"))
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM classification_reprocess_requests").fetchone()[0],
            0,
        )
        self.assertEqual(
            json.loads(evidence_json)["matches"][0]["code"],
            "waf_access_blocked_firewall",
        )

        second = await scan_classification_anomalies(self.d1, limit=20, lease_owner="test-worker")
        self.assertEqual(second["skipped"], 1)

        self.connection.execute(
            """
            INSERT INTO classification_reprocess_requests (
              tool_id, anomaly_candidate_id, request_source, evidence_mode,
              reason, status, requested_by
            ) VALUES (318, ?, 'anomaly', 'official_url', 'admin approved', 'queued', 'test-admin')
            """,
            [candidate_id],
        )
        request_id = self.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.connection.execute(
            "UPDATE classification_anomaly_candidates SET status = 'approved', resolution_request_id = ? WHERE id = ?",
            [request_id, candidate_id],
        )
        self.connection.commit()

        queued = await load_queued_reclassification_tasks(self.d1, limit=10)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["taxonomy_evidence_url"], "https://janitorai.com/")

        lease_token = await claim_reclassification_request(
            self.d1,
            request_id,
            lease_owner="test-taxonomy-worker",
        )
        self.assertTrue(lease_token)
        result_status = await complete_reclassification_request(
            self.d1,
            request_id=request_id,
            lease_token=str(lease_token),
            result=SimpleNamespace(
                status="succeeded",
                run_id=1,
                primary_slug="ai-companions",
                primary_confidence=0.94,
                entity_kind="independent_product",
                error="",
            ),
            auto_accept_threshold=0.85,
        )
        self.assertEqual(result_status, "succeeded")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM classification_reprocess_requests WHERE id = ?",
                [request_id],
            ).fetchone()[0],
            "succeeded",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM classification_anomaly_candidates WHERE id = ?",
                [candidate_id],
            ).fetchone()[0],
            "resolved",
        )
        event = self.connection.execute(
            "SELECT action FROM classification_anomaly_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(event, "reclassification_succeeded")

        self.connection.execute(
            """
            INSERT INTO classification_reprocess_requests (
              tool_id, request_source, evidence_mode, reason, status, requested_by
            ) VALUES (318, 'manual', 'verified_source', 'prefer clean discovery evidence', 'queued', 'test-admin')
            """
        )
        self.connection.commit()
        verified_source_queue = await load_queued_reclassification_tasks(self.d1, limit=10)
        self.assertEqual(
            verified_source_queue[0]["taxonomy_evidence_url"],
            "https://clean.example/janitorai",
        )

    async def test_polluted_success_result_stays_approved_for_manual_attention(self):
        await scan_classification_anomalies(self.d1, limit=20, lease_owner="test-worker")
        candidate_id = self.connection.execute(
            "SELECT id FROM classification_anomaly_candidates WHERE tool_id = 318"
        ).fetchone()[0]
        self.connection.execute(
            """
            INSERT INTO classification_reprocess_requests (
              tool_id, anomaly_candidate_id, request_source, evidence_mode,
              reason, status, requested_by
            ) VALUES (318, ?, 'anomaly', 'official_url', 'admin approved', 'queued', 'test-admin')
            """,
            [candidate_id],
        )
        request_id = self.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.connection.execute(
            "UPDATE classification_anomaly_candidates SET status = 'approved' WHERE id = ?",
            [candidate_id],
        )
        self.connection.commit()

        lease_token = await claim_reclassification_request(
            self.d1,
            request_id,
            lease_owner="test-taxonomy-worker",
        )
        result_status = await complete_reclassification_request(
            self.d1,
            request_id=request_id,
            lease_token=str(lease_token),
            result=SimpleNamespace(
                status="succeeded",
                run_id=1,
                primary_slug="ai-security-compliance",
                primary_confidence=0.99,
                entity_kind="independent_product",
                error="",
                raw={"profile": "Generic example domain page for documentation examples"},
            ),
            auto_accept_threshold=0.85,
        )

        self.assertEqual(result_status, "needs_manual")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM classification_anomaly_candidates WHERE id = ?",
                [candidate_id],
            ).fetchone()[0],
            "approved",
        )
        payload = json.loads(
            self.connection.execute(
                "SELECT result_json FROM classification_reprocess_requests WHERE id = ?",
                [request_id],
            ).fetchone()[0]
        )
        self.assertEqual(
            payload["pollution_gate"]["code"],
            "neutral_transport_example_domain",
        )

    async def test_detector_cursor_advances_past_the_first_page_and_wraps(self):
        self.connection.executescript(
            """
            INSERT INTO tools (
              id, canonical_slug, normalized_domain, official_url, primary_category_id,
              category_classification_status, category_classification_raw,
              entity_kind, entity_kind_source, status, duplicate_of_tool_id
            ) VALUES (
              319, 'second-blocked', 'second.example', 'https://second.example/', 28,
              'legacy', NULL, 'unresolved', 'auto', 'published', NULL
            );
            INSERT INTO tool_localizations (
              tool_id, locale_code, name, tagline, short_description,
              long_description, feature_highlights, translation_status
            ) VALUES (
              319, 'en', 'Second blocked product', 'Just a moment...',
              'Checking your browser before accessing the site', '', '', 'published'
            );
            """
        )

        first = await scan_classification_anomalies(self.d1, limit=1, lease_owner="page-one")
        self.assertEqual(first, {"scanned": 1, "candidates": 1, "skipped": 0})
        first_result = json.loads(
            self.connection.execute(
                "SELECT last_result_json FROM classification_anomaly_detector_state"
            ).fetchone()[0]
        )
        self.assertEqual(first_result["scan_cursor_tool_id"], 0)
        self.assertEqual(first_result["next_cursor_tool_id"], 318)
        self.assertFalse(first_result["wrapped"])
        first_delay_minutes = self.connection.execute(
            """
            SELECT (julianday(next_scan_at) - julianday(last_completed_at)) * 24 * 60
            FROM classification_anomaly_detector_state
            """
        ).fetchone()[0]
        self.assertAlmostEqual(first_delay_minutes, 5, delta=0.1)

        self.connection.execute(
            "UPDATE classification_anomaly_detector_state SET next_scan_at = '2000-01-01T00:00:00.000Z'"
        )
        self.connection.commit()
        second = await scan_classification_anomalies(self.d1, limit=1, lease_owner="page-two")
        self.assertEqual(second, {"scanned": 1, "candidates": 1, "skipped": 0})
        self.assertEqual(
            self.connection.execute(
                "SELECT group_concat(tool_id, ',') FROM classification_anomaly_candidates ORDER BY tool_id"
            ).fetchone()[0],
            "318,319",
        )

        self.connection.execute(
            "UPDATE classification_anomaly_detector_state SET next_scan_at = '2000-01-01T00:00:00.000Z'"
        )
        self.connection.commit()
        wrapped = await scan_classification_anomalies(self.d1, limit=1, lease_owner="wrap")
        self.assertEqual(wrapped, {"scanned": 0, "candidates": 0, "skipped": 0})
        wrapped_result = json.loads(
            self.connection.execute(
                "SELECT last_result_json FROM classification_anomaly_detector_state"
            ).fetchone()[0]
        )
        self.assertEqual(wrapped_result["scan_cursor_tool_id"], 319)
        self.assertEqual(wrapped_result["next_cursor_tool_id"], 0)
        self.assertTrue(wrapped_result["wrapped"])
        wrapped_delay_minutes = self.connection.execute(
            """
            SELECT (julianday(next_scan_at) - julianday(last_completed_at)) * 24 * 60
            FROM classification_anomaly_detector_state
            """
        ).fetchone()[0]
        self.assertAlmostEqual(wrapped_delay_minutes, 360, delta=0.1)


if __name__ == "__main__":
    unittest.main()
