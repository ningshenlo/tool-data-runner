"""Unit tests for scripts/taxonomy_observability.py."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from scripts import taxonomy_observability
from shared.db.legacy import LegacyPostgresClient


class AsyncSqliteD1:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    async def query(self, sql: str, params=None, **_kwargs):
        return [dict(row) for row in self.connection.execute(sql, params or []).fetchall()]


class TaxonomyObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE tools (
              id INTEGER PRIMARY KEY
            );
            CREATE TABLE classification_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT
            );
            CREATE TABLE taxonomy_terms (
              id INTEGER PRIMARY KEY
            );
            CREATE TABLE product_taxonomy_assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tool_id INTEGER NOT NULL,
              term_id INTEGER NOT NULL,
              is_primary INTEGER NOT NULL,
              decision_status TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        migration_dir = (
            Path(__file__).resolve().parent / "fixtures" / "taxonomy-migrations"
        )
        self.connection.executescript(
            (migration_dir / "0077_openai_taxonomy_batch.sql").read_text(encoding="utf-8")
        )
        self.connection.executescript(
            (migration_dir / "0079_taxonomy_batch_retry.sql").read_text(encoding="utf-8")
        )
        self.connection.executemany(
            "INSERT INTO tools (id) VALUES (?)",
            [(1,), (2,), (3,)],
        )
        self.connection.executemany(
            """
            INSERT INTO taxonomy_batch_items (
              id, tool_id, pipeline_version, prompt_version, taxonomy_version,
              status, current_stage, error, created_at, updated_at
            ) VALUES (?, ?, 'p', 'v', 2, ?, ?, ?, ?, ?)
            """,
            [
                (1, 1, "succeeded", "complete", None, "2026-11-24T00:00:00.000Z", "2026-11-24T01:00:00.000Z"),
                (2, 2, "running", "leaf_escalation", None, "2026-11-24T00:00:00.000Z", "2026-11-24T02:00:00.000Z"),
                (3, 3, "needs_review", "complete", "taxonomy_gap:ai-world-models", "2026-11-24T00:00:00.000Z", "2026-11-24T03:00:00.000Z"),
            ],
        )
        single_req = json.dumps({"body": {"text": {"format": {"name": "taxonomy_single_call"}}}})
        legacy_req = json.dumps({"body": {"text": {"format": {"name": "taxonomy_profile"}}}})
        self.connection.executemany(
            """
            INSERT INTO taxonomy_batch_requests (
              item_id, custom_id, stage, attempt, model, reasoning_effort,
              max_output_tokens, request_json, status,
              input_tokens, cached_input_tokens, output_tokens, reasoning_tokens,
              created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, 'low', 4096, ?, 'succeeded', ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "c1", "profile", "gpt-5.6-luna", single_req, 5000, 4000, 300, 50, "2026-11-24T00:10:00.000Z", "2026-11-24T00:10:00.000Z"),
                (2, "c2", "profile", "gpt-5.6-luna", single_req, 5000, 4000, 350, 50, "2026-11-24T00:20:00.000Z", "2026-11-24T00:20:00.000Z"),
                (2, "c2e", "leaf_escalation", "gpt-5.6-terra", legacy_req, 2000, 0, 500, 200, "2026-11-24T00:30:00.000Z", "2026-11-24T00:30:00.000Z"),
                (3, "c3", "profile", "gpt-5.6-luna", legacy_req, 3000, 0, 1200, 600, "2026-11-23T00:00:00.000Z", "2026-11-23T00:00:00.000Z"),
            ],
        )
        self.connection.executemany(
            """
            INSERT INTO product_taxonomy_assignments (
              tool_id, term_id, is_primary, decision_status, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (1, 10, 1, "auto_accepted", "2026-11-24T01:00:00.000Z"),
                (1, 11, 0, "auto_accepted", "2026-11-24T01:00:00.000Z"),
                (2, 12, 0, "provisional", "2026-11-24T02:00:00.000Z"),
            ],
        )
        self.connection.commit()
        self.d1 = AsyncSqliteD1(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    async def test_collect_taxonomy_observability_computes_cache_escalation_gaps_and_secondaries(self):
        report = await taxonomy_observability.collect_taxonomy_observability(
            self.d1,
            since_iso="2026-11-24T00:00:00.000Z",
        )
        self.assertEqual(report["totals"]["request_count"], 3)
        self.assertEqual(report["totals"]["input_tokens"], 12000)
        self.assertEqual(report["totals"]["cached_input_tokens"], 8000)
        self.assertEqual(report["totals"]["cache_hit_rate"], 0.6667)
        self.assertEqual(report["totals"]["output_tokens"], 1150)
        self.assertEqual(report["totals"]["reasoning_tokens"], 300)
        self.assertEqual(report["totals"]["reasoning_ratio"], 0.2609)

        self.assertEqual(
            report["single_call_summary"],
            {"item_count": 2, "escalated_item_count": 1, "escalation_rate": 0.5},
        )
        self.assertEqual(
            report["taxonomy_gaps"],
            [
                {
                    "error": "taxonomy_gap:ai-world-models",
                    "gap_count": 1,
                    "latest_at": "2026-11-24T03:00:00.000Z",
                }
            ],
        )
        self.assertEqual(
            report["secondary_assignments"],
            [
                {"decision_status": "auto_accepted", "assignment_count": 1},
                {"decision_status": "provisional", "assignment_count": 1},
            ],
        )

    def test_all_observability_queries_translate_cleanly_for_postgres(self):
        client = LegacyPostgresClient("postgresql://u:p@localhost/db")
        for name, sql in taxonomy_observability.OBSERVABILITY_QUERIES:
            entry, pg_sql, params = client.prepare(sql, ["2026-11-24T00:00:00.000Z"])
            self.assertEqual(entry["verb"], "select", name)
            self.assertEqual(params, {"p1": "2026-11-24T00:00:00.000Z"}, name)
            self.assertIn("%(p1)s", pg_sql, name)
