import json
import sqlite3
import unittest

from taxonomy_materialize import (
    LEGACY_CATEGORY_MATERIALIZATION_VERSION,
    LegacyMaterializationError,
    TaxonomyTermRef,
    materialize_legacy_category,
    materialize_effective_primary_assignments,
)


class AsyncSqliteD1:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    async def query(self, sql, params=None, **_kwargs):
        return [
            dict(row)
            for row in self.connection.execute(sql, params or []).fetchall()
        ]

    async def batch(self, statements, **_kwargs):
        results = []
        with self.connection:
            for sql, params in statements:
                cursor = self.connection.execute(sql, params or [])
                results.append({"meta": {"changes": cursor.rowcount}})
        return results


class MaterializeLegacyCategoryTests(unittest.TestCase):
    def test_leaf_with_parent(self) -> None:
        plan = materialize_legacy_category(
            TaxonomyTermRef(200, "video-generation-conversion", 100, 74),
            TaxonomyTermRef(100, "video-animation", None, 27),
        )
        self.assertEqual(plan.materialization_version, LEGACY_CATEGORY_MATERIALIZATION_VERSION)
        self.assertEqual(plan.primary_category_id, 27)
        self.assertEqual(plan.category_ids, [27, 74])

    def test_root_leaf(self) -> None:
        plan = materialize_legacy_category(TaxonomyTermRef(50, "orphan", None, 99))
        self.assertEqual(plan.primary_category_id, 99)
        self.assertEqual(plan.category_ids, [99])

    def test_missing_source_raises(self) -> None:
        with self.assertRaises(LegacyMaterializationError):
            materialize_legacy_category(TaxonomyTermRef(1, "x", None, None))


class EffectivePrimaryProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE tools (
              id INTEGER PRIMARY KEY,
              status TEXT NOT NULL,
              duplicate_of_tool_id INTEGER,
              primary_category_id INTEGER,
              category_classification_status TEXT,
              category_classification_attempts INTEGER DEFAULT 0,
              category_classification_raw TEXT,
              category_classification_last_error TEXT,
              category_classification_updated_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE taxonomy_terms (
              id INTEGER PRIMARY KEY,
              dimension TEXT NOT NULL,
              slug TEXT NOT NULL,
              parent_id INTEGER,
              source_category_id INTEGER,
              status TEXT NOT NULL
            );
            CREATE TABLE product_taxonomy_assignments (
              id INTEGER PRIMARY KEY,
              tool_id INTEGER NOT NULL,
              term_id INTEGER NOT NULL,
              is_primary INTEGER NOT NULL,
              confidence REAL,
              decision_status TEXT NOT NULL,
              source TEXT NOT NULL,
              assigned_at TEXT NOT NULL,
              reviewed_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE tool_categories (
              tool_id INTEGER NOT NULL,
              category_id INTEGER NOT NULL,
              source TEXT NOT NULL DEFAULT 'auto',
              raw_output TEXT,
              classified_at TEXT,
              UNIQUE(tool_id, category_id)
            );
            CREATE TABLE legacy_category_materializations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tool_id INTEGER NOT NULL,
              leaf_term_id INTEGER NOT NULL,
              materialization_version TEXT NOT NULL,
              primary_category_id INTEGER,
              category_ids_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        self.d1 = AsyncSqliteD1(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    async def test_projects_effective_leaf_and_preserves_manual_links(self) -> None:
        self.connection.executescript(
            """
            INSERT INTO tools (id, status, primary_category_id, updated_at)
            VALUES (1, 'pending_enrich', 5, '2026-08-01T00:00:00Z');
            INSERT INTO taxonomy_terms
              (id, dimension, slug, parent_id, source_category_id, status)
            VALUES
              (100, 'primary_category', 'video-animation', NULL, 27, 'active'),
              (200, 'primary_category', 'video-generation', 100, 74, 'active');
            INSERT INTO product_taxonomy_assignments
              (id, tool_id, term_id, is_primary, confidence, decision_status,
               source, assigned_at, updated_at)
            VALUES
              (10, 1, 200, 1, 0.91, 'auto_accepted', 'auto',
               '2026-08-30T00:00:00Z', '2026-08-30T00:00:00Z');
            INSERT INTO tool_categories (tool_id, category_id, source)
            VALUES (1, 5, 'auto'), (1, 99, 'manual');
            """
        )

        counts = await materialize_effective_primary_assignments(self.d1, 10)
        self.assertEqual(
            counts,
            {
                "legacy_projection_selected": 1,
                "legacy_projection_succeeded": 1,
                "legacy_projection_failed": 0,
            },
        )
        tool = self.connection.execute(
            "SELECT * FROM tools WHERE id = 1"
        ).fetchone()
        self.assertEqual(tool["primary_category_id"], 27)
        self.assertEqual(tool["category_classification_status"], "auto_ok")
        provenance = json.loads(tool["category_classification_raw"])
        self.assertEqual(provenance["mode"], "taxonomy_compatibility_projection")
        self.assertEqual(provenance["assignment_id"], 10)
        categories = self.connection.execute(
            "SELECT category_id, source FROM tool_categories WHERE tool_id = 1 ORDER BY category_id"
        ).fetchall()
        self.assertEqual(
            [(row["category_id"], row["source"]) for row in categories],
            [(27, "auto"), (74, "auto"), (99, "manual")],
        )
        audit = self.connection.execute(
            "SELECT * FROM legacy_category_materializations WHERE tool_id = 1"
        ).fetchone()
        self.assertEqual(json.loads(audit["category_ids_json"]), [27, 74])

        second = await materialize_effective_primary_assignments(self.d1, 10)
        self.assertEqual(second["legacy_projection_selected"], 0)

        self.connection.execute(
            """
            INSERT INTO product_taxonomy_assignments
              (id, tool_id, term_id, is_primary, confidence, decision_status,
               source, assigned_at, reviewed_at, updated_at)
            VALUES
              (11, 1, 200, 1, 1.0, 'verified', 'manual',
               '2026-08-31T00:00:00Z', '2026-08-31T00:00:00Z',
               '2026-08-31T00:00:00Z')
            """
        )
        reassigned = await materialize_effective_primary_assignments(self.d1, 10)
        self.assertEqual(reassigned["legacy_projection_selected"], 1)
        refreshed = self.connection.execute(
            "SELECT category_classification_raw FROM tools WHERE id = 1"
        ).fetchone()
        self.assertEqual(json.loads(refreshed["category_classification_raw"])["assignment_id"], 11)
        categories = self.connection.execute(
            "SELECT category_id, source FROM tool_categories WHERE tool_id = 1 ORDER BY category_id"
        ).fetchall()
        self.assertEqual(
            [(row["category_id"], row["source"]) for row in categories],
            [(27, "manual"), (74, "manual"), (99, "manual")],
        )


if __name__ == "__main__":
    unittest.main()
