"""Unit tests for P2A Shadow Mode pure helpers (no network / D1)."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from runner import AssetTask

from taxonomy_shadow import (
    SHADOW_PROMPT_VERSION,
    TAXONOMY_TERMS_SQL,
    TaxonomyTerm,
    auto_primary_write_state,
    build_leaf_candidate_pool,
    build_product_profile,
    capability_term_chunks,
    capability_retry_terms,
    capabilities_prompt,
    catalog_from_rows,
    classify_capability_profile_shadow,
    classify_tool_shadow,
    decide_primary_status,
    evidence_quote_is_grounded,
    load_capability_backfill_tasks,
    load_shadow_tasks,
    merge_capability_decisions,
    normalize_evidence_items,
    normalize_evidenced_value,
    parse_capabilities,
    parse_entity_decision,
    parse_leaf_decision,
    parse_secondary_leaf_decisions,
    parse_top2_l1,
    profile_has_signal,
    recall_capability_candidates,
    resolve_entity_decision,
)


def _catalog():
    rows = [
        {
            "id": 1,
            "dimension": "primary_category",
            "slug": "video",
            "name": "video",
            "parent_id": None,
            "taxonomy_version": 1,
        },
        {
            "id": 2,
            "dimension": "primary_category",
            "slug": "text-to-video",
            "name": "text to video",
            "parent_id": 1,
            "parent_slug": "video",
            "includes": "text to video; image to video",
            "taxonomy_version": 1,
        },
        {
            "id": 3,
            "dimension": "primary_category",
            "slug": "image",
            "name": "image",
            "parent_id": None,
            "taxonomy_version": 1,
        },
        {
            "id": 4,
            "dimension": "primary_category",
            "slug": "image-generation",
            "name": "image generation",
            "parent_id": 3,
            "parent_slug": "image",
            "includes": "image generation; background removal",
            "taxonomy_version": 1,
        },
        {
            "id": 10,
            "dimension": "capability",
            "slug": "text-to-video",
            "name": "text to video",
            "parent_id": None,
            "taxonomy_version": 1,
        },
        {
            "id": 11,
            "dimension": "capability",
            "slug": "image-to-video",
            "name": "image to video",
            "parent_id": None,
            "taxonomy_version": 1,
        },
        {
            "id": 12,
            "dimension": "capability",
            "slug": "code-generation",
            "name": "code generation",
            "parent_id": None,
            "taxonomy_version": 1,
        },
    ]
    return catalog_from_rows(rows)


class CatalogVersionTests(unittest.TestCase):
    def test_catalog_sql_selects_only_latest_active_version(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE taxonomy_terms (
              id INTEGER PRIMARY KEY,
              dimension TEXT NOT NULL,
              slug TEXT NOT NULL,
              name TEXT NOT NULL,
              parent_id INTEGER,
              definition TEXT,
              includes TEXT,
              excludes TEXT,
              examples TEXT,
              taxonomy_version INTEGER NOT NULL,
              source_category_id INTEGER,
              status TEXT NOT NULL,
              display_order INTEGER NOT NULL
            );

            INSERT INTO taxonomy_terms VALUES
              (1, 'primary_category', 'legacy-root', 'Legacy', NULL,
               NULL, NULL, NULL, NULL, 1, NULL, 'active', 10),
              (2, 'primary_category', 'current-root', 'Current', NULL,
               NULL, NULL, NULL, NULL, 2, NULL, 'active', 10),
              (3, 'capability', 'current-capability', 'Current capability', NULL,
               NULL, NULL, NULL, NULL, 2, NULL, 'active', 20);
            """
        )

        rows = connection.execute(TAXONOMY_TERMS_SQL).fetchall()
        connection.close()

        self.assertEqual(
            {row[2] for row in rows}, {"current-root", "current-capability"}
        )
        self.assertEqual({row[10] for row in rows}, {2})


class ShadowTaskSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_backfill_reuses_profiles_and_targets_undercoverage(self):
        observed: dict[str, object] = {}

        class FakeD1:
            async def query(self, sql, params):
                observed["sql"] = sql
                observed["params"] = params
                return []

        await load_capability_backfill_tasks(
            FakeD1(),
            limit=25,
            retry_model_name="test-model",
        )

        sql = str(observed["sql"])
        self.assertIn("JOIN product_profiles", sql)
        self.assertIn("'capability_backfill' AS selection_reason", sql)
        self.assertIn("< 3", sql)
        self.assertIn("'app_or_extension'", sql)
        self.assertIn("failed_run.model_name = ?", sql)

    async def test_task_query_prefers_explicit_verified_taxonomy_evidence_source(self):
        observed: dict[str, object] = {}

        class FakeD1:
            async def query(self, sql, params):
                observed["sql"] = sql
                observed["params"] = params
                return []

        await load_shadow_tasks(FakeD1(), limit=1, tool_ids=[32])

        sql = str(observed["sql"])
        self.assertIn("AS taxonomy_evidence_url", sql)
        self.assertIn("$.taxonomy_evidence", sql)
        self.assertIn("source.verification_status = 'verified'", sql)

    async def test_standard_backlog_skips_any_prior_terminal_shadow_run(self):
        observed: dict[str, object] = {}

        class FakeD1:
            async def query(self, sql, params):
                observed["sql"] = sql
                observed["params"] = params
                return []

        await load_shadow_tasks(
            FakeD1(),
            limit=100,
            after_tool_id=500,
            allow_unresolved_entity=True,
        )

        sql = str(observed["sql"])
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("terminal_run.prompt_version LIKE 'shadow-%'", sql)
        self.assertIn("terminal_run.run_status IN ('succeeded', 'partial', 'skipped')", sql)
        self.assertIn("failed_run.run_status = 'failed'", sql)
        self.assertIn("entity_kind_source", sql)
        self.assertEqual(
            observed["params"],
            [500, SHADOW_PROMPT_VERSION, 100],
        )

    async def test_batch_query_scopes_failure_budget_to_active_primary_model(self):
        observed: dict[str, object] = {}

        class FakeD1:
            async def query(self, sql, params):
                observed["sql"] = sql
                observed["params"] = params
                return []

        await load_shadow_tasks(
            FakeD1(),
            limit=20,
            allow_unresolved_entity=True,
            retry_model_name="deepseek/deepseek-v4-flash",
        )

        self.assertIn("failed_run.model_name = ?", str(observed["sql"]))
        self.assertEqual(
            observed["params"],
            [
                0,
                SHADOW_PROMPT_VERSION,
                "deepseek/deepseek-v4-flash",
                20,
            ],
        )

    async def test_auto_non_product_recheck_is_explicit_prioritized_and_manual_safe(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tools (
              id INTEGER PRIMARY KEY,
              canonical_slug TEXT,
              normalized_domain TEXT,
              official_url TEXT,
              entity_kind TEXT,
              entity_kind_source TEXT,
              status TEXT,
              duplicate_of_tool_id INTEGER
            );
            CREATE TABLE tool_sources (
              id INTEGER PRIMARY KEY,
              tool_id INTEGER,
              source_url TEXT,
              source_type TEXT,
              verification_status TEXT,
              confidence_score REAL,
              raw_payload TEXT
            );
            CREATE TABLE classification_runs (
              id INTEGER PRIMARY KEY,
              tool_id INTEGER,
              prompt_version TEXT,
              run_status TEXT,
              model_name TEXT,
              raw_output TEXT
            );
            INSERT INTO tools VALUES
              (1, 'product', 'product.test', 'https://product.test/',
               'independent_product', 'auto', 'published', NULL),
              (2, 'unknown', 'unknown.test', 'https://unknown.test/',
               'unresolved', 'auto', 'published', NULL),
              (3, 'incident', 'incident.test', 'https://incident.test/',
               'non_product', 'auto', 'published', NULL),
              (4, 'manual-reject', 'manual.test', 'https://manual.test/',
               'non_product', 'manual', 'published', NULL),
              (5, 'already-rechecked', 'done.test', 'https://done.test/',
               'non_product', 'auto', 'published', NULL),
              (6, 'duplicate', 'duplicate.test', 'https://duplicate.test/',
               'non_product', 'auto', 'published', 3),
              (7, 'no-domain', '', 'https://no-domain.test/',
               'non_product', 'auto', 'published', NULL),
              (8, 'pending-incident', 'pending.test', 'https://pending.test/',
               'non_product', 'auto', 'pending_review', NULL),
              (9, 'old-terminal', 'old-terminal.test', 'https://old-terminal.test/',
               'independent_product', 'auto', 'published', NULL),
              (10, 'unsafe-incident', 'unsafe-incident.test', 'https://unsafe-incident.test/',
               'independent_product', 'auto', 'published', NULL);
            """
        )
        connection.execute(
            """
            INSERT INTO classification_runs
              (id, tool_id, prompt_version, run_status, model_name)
            VALUES (1, 5, ?, 'partial', 'test-model')
            """,
            [SHADOW_PROMPT_VERSION],
        )
        connection.execute(
            """
            INSERT INTO classification_runs
              (id, tool_id, prompt_version, run_status, model_name)
            VALUES (2, 9, 'shadow-older-prompt', 'succeeded', 'test-model')
            """
        )
        connection.execute(
            """
            INSERT INTO classification_runs
              (id, tool_id, prompt_version, run_status, model_name, raw_output)
            VALUES (
              3,
              10,
              'shadow-unsafe-workers-ai',
              'succeeded',
              'deepseek/deepseek-v4-flash',
              '{"auto_non_product_recheck":1,"model_policy":{"downstream_models":["workers-ai/test"]}}'
            )
            """
        )

        class SqliteD1:
            async def query(self, sql, params):
                return [dict(row) for row in connection.execute(sql, params).fetchall()]

        default_rows = await load_shadow_tasks(
            SqliteD1(),
            limit=20,
            allow_unresolved_entity=True,
        )
        incident_rows = await load_shadow_tasks(
            SqliteD1(),
            limit=20,
            allow_unresolved_entity=True,
            include_auto_non_product_recheck=True,
        )
        connection.close()

        self.assertEqual([row["tool_id"] for row in default_rows], [1, 2])
        self.assertEqual([row["tool_id"] for row in incident_rows], [3, 10, 1, 2])
        self.assertEqual(
            [row["selection_reason"] for row in incident_rows],
            [
                "auto_non_product_recheck",
                "auto_non_product_recheck",
                "standard",
                "standard",
            ],
        )


class EntityDecisionTests(unittest.TestCase):
    def test_accepts_high_confidence_evidenced_independent_product(self):
        decision = parse_entity_decision(
            {
                "entity_kind": "independent_product",
                "entity_confidence": 0.91,
                "entity_reason": "Dedicated product and signup",
                "entity_evidence": [{"quote": "Start building for free"}],
            },
            source_url="https://example.com/",
        )

        self.assertEqual(decision["kind"], "independent_product")
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["evidence"][0]["source_url"], "https://example.com/")

    def test_low_confidence_candidate_fails_closed_to_unresolved(self):
        decision = parse_entity_decision(
            {
                "entity_kind": "company_site",
                "entity_confidence": 0.6,
                "entity_reason": "Possibly a company portfolio",
                "entity_evidence": [{"quote": "Meet our portfolio of products"}],
            }
        )

        self.assertEqual(decision["candidate_kind"], "company_site")
        self.assertEqual(decision["kind"], "unresolved")
        self.assertFalse(decision["accepted"])

    def test_missing_entity_evidence_fails_closed(self):
        decision = parse_entity_decision(
            {
                "entity_kind": "independent-product",
                "entity_confidence": 0.99,
                "entity_reason": "No quoted evidence",
                "entity_evidence": [],
            }
        )

        self.assertEqual(decision["candidate_kind"], "independent_product")
        self.assertEqual(decision["kind"], "unresolved")

    def test_provider_collapsed_entity_evidence_quotes_are_split_and_grounded(self):
        source_text = (
            "Build Knowledgeable AI. Pinecone is a fully managed vector database built for AI. "
            "Start Building. Get a Demo."
        )
        evidence = normalize_evidence_items(
            '"Build Knowledgeable AI"; "Pinecone is a fully managed vector database built for AI."',
            source_url="https://www.pinecone.io/",
            source_text=source_text,
        )
        self.assertEqual(
            [item["quote"] for item in evidence],
            [
                "Build Knowledgeable AI",
                "Pinecone is a fully managed vector database built for AI.",
            ],
        )
        decision = parse_entity_decision(
            {
                "entity_kind": "independent_product",
                "entity_confidence": 0.96,
                "entity_reason": "A dedicated product with signup and demo paths.",
                "entity_evidence": (
                    '"Build Knowledgeable AI"; '
                    '"Pinecone is a fully managed vector database built for AI."'
                ),
            },
            source_url="https://www.pinecone.io/",
            source_text=source_text,
        )
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["kind"], "independent_product")

    def test_entity_evidence_is_a_minimal_grounded_profile_fallback(self):
        profile = build_product_profile(
            {
                "entity_kind": "independent_product",
                "entity_confidence": 0.95,
                "entity_reason": "Dedicated product.",
                "entity_evidence": [{"quote": "Build what's next on the AI Native Cloud"}],
                "primary_job": "Accelerate AI development with a full-stack cloud.",
                "primary_outputs": "AI model outputs and fine-tuned models.",
                "capabilities_raw": "Inference; compute; fine-tuning",
            },
            source_url="https://www.together.ai/",
            source_text="Build what's next on the AI Native Cloud. The Together AI Platform.",
        )
        self.assertTrue(profile_has_signal(profile))
        self.assertEqual(
            profile["primary_job"]["value"],
            "Build what's next on the AI Native Cloud",
        )

    def test_error_page_can_never_be_accepted_as_non_product(self):
        decision = parse_entity_decision(
            {
                "entity_kind": "non_product",
                "entity_confidence": 1.0,
                "entity_reason": "Cloudflare SSL error page",
                "entity_evidence": [
                    {"quote": "Invalid SSL certificate Error code 526"}
                ],
            }
        )

        self.assertEqual(decision["candidate_kind"], "non_product")
        self.assertEqual(decision["kind"], "unresolved")
        self.assertFalse(decision["accepted"])
        self.assertTrue(decision["error_page_detected"])

    def test_neutral_transport_page_can_never_be_accepted_as_non_product(self):
        decision = parse_entity_decision(
            {
                "entity_kind": "non_product",
                "entity_confidence": 1.0,
                "entity_reason": "The webpage does not describe a product or service.",
                "entity_evidence": [
                    {
                        "quote": (
                            "This domain is for use in documentation examples "
                            "without needing permission."
                        )
                    }
                ],
            },
            source_url="https://www.cursor.com/",
        )

        self.assertEqual(decision["candidate_kind"], "non_product")
        self.assertEqual(decision["kind"], "unresolved")
        self.assertFalse(decision["accepted"])
        self.assertTrue(decision["neutral_transport_detected"])

    def test_ungrounded_entity_quote_fails_closed(self):
        decision = parse_entity_decision(
            {
                "entity_kind": "non_product",
                "entity_confidence": 0.8,
                "entity_reason": "The webpage does not describe a product or service.",
                "entity_evidence": [
                    {
                        "quote": (
                            "This domain is for use in documentation examples "
                            "without needing permission."
                        )
                    }
                ],
            },
            source_url="https://www.kimi.com/products/kimi-work",
            source_text="Kimi Work is your AI productivity assistant for professional tasks.",
        )

        self.assertEqual(decision["candidate_kind"], "non_product")
        self.assertEqual(decision["kind"], "unresolved")
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["evidence"], [])
        self.assertTrue(decision["ungrounded_evidence_detected"])

    def test_existing_auto_entity_can_be_corrected_by_new_prediction(self):
        predicted = {
            "kind": "unresolved",
            "candidate_kind": "non_product",
            "confidence": 0.99,
            "accepted": False,
            "source": "auto",
            "error_page_detected": True,
        }
        decision = resolve_entity_decision(
            predicted,
            existing_kind="non_product",
            existing_source="auto",
        )

        self.assertIs(decision, predicted)
        self.assertEqual(decision["kind"], "unresolved")

    def test_existing_manual_entity_overrides_prediction(self):
        decision = resolve_entity_decision(
            {
                "kind": "company_site",
                "candidate_kind": "company_site",
                "confidence": 0.95,
                "accepted": True,
                "source": "auto",
            },
            existing_kind="independent_product",
            existing_source="manual",
        )

        self.assertEqual(decision["kind"], "independent_product")
        self.assertEqual(decision["source"], "manual")


class CapabilityBackfillPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_reuses_profile_and_makes_only_one_model_call(self):
        stages: list[str] = []

        class FakeD1:
            async def query(self, sql, params):
                return [{"term_id": 2}]

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "candidate-model"}]

            async def fetch_homepage_content(self, task):
                raise AssertionError("capability backfill must not refetch the homepage")

            async def fetch_structured_text_data(
                self, *, source_url, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                stages.append(stage)
                return source_url, {
                    "capability_slugs": [
                        {
                            "slug": "text-to-video",
                            "role": "core",
                            "confidence": 0.9,
                            "evidence": [{"quote": "Turn text into video"}],
                        }
                    ]
                }

        result = await classify_capability_profile_shadow(
            d1=FakeD1(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=122,
                canonical_slug="stored-profile",
                normalized_domain="stored.test",
                official_url="https://stored.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            profile={
                "source_url": "https://stored.test/",
                "capabilities_raw": [
                    {
                        "value": "text to video",
                        "evidence": [{"quote": "Turn text into video"}],
                    }
                ],
            },
            dry_run=True,
            capability_candidate_limit=24,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(stages, ["shadow_capabilities_backfill"])
        self.assertEqual(result.capability_slugs, ["text-to-video"])


class PrimaryOnlyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_only_never_calls_capability_stages(self):
        stages: list[str] = []
        structured_urls: list[str] = []
        prompts: dict[str, str] = {}
        content_urls: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                content_urls.append(task.official_url)
                return task.official_url, (
                    "<html><body><h1>Generate videos from text</h1>"
                    "<a href='/signup'>Start for free</a></body></html>"
                )

            async def fetch_structured_text_data(
                self,
                *,
                source_url,
                stage,
                prompt,
                json_schema,
                custom_ai,
                **kwargs,
            ):
                stages.append(stage)
                structured_urls.append(source_url)
                prompts[stage] = prompt
                if stage == "shadow_profile_main_content":
                    return source_url, {
                        "entity_kind": "independent_product",
                        "entity_confidence": 0.95,
                        "entity_reason": "Dedicated product",
                        "entity_evidence": [{"quote": "Start for free"}],
                        "primary_job": {
                            "value": "Generate videos",
                            "evidence": [{"quote": "Generate videos from text"}],
                        },
                        "primary_outputs": [],
                        "capabilities_raw": [],
                    }
                if stage == "shadow_l1_top2":
                    return source_url, {
                        "l1_candidates": [
                            {"slug": "video", "confidence": 0.9, "reason": "main market"}
                        ]
                    }
                if stage == "shadow_leaf":
                    return source_url, {
                        "leaf_slug": "text-to-video",
                        "confidence": 0.88,
                        "reason": "primary job",
                        "evidence": [{"quote": "Generate videos from text"}],
                    }
                raise AssertionError(f"unexpected stage: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=123,
                canonical_slug="example",
                normalized_domain="product.test",
                official_url="https://product.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=False,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.primary_slug, "text-to-video")
        self.assertEqual(
            stages,
            ["shadow_profile_main_content", "shadow_l1_top2", "shadow_leaf"],
        )
        self.assertEqual(content_urls, ["https://product.test/"])
        self.assertEqual(
            structured_urls,
            ["https://product.test/", "https://product.test/", "https://product.test/"],
        )
        self.assertIn("Generate videos", prompts["shadow_l1_top2"])
        self.assertIn("Generate videos", prompts["shadow_leaf"])
        self.assertEqual(result.raw["profile_extraction_path"], "cleaned_main_content")
        self.assertEqual(result.raw["classification_transport"], "cleaned_main_content_only")
        self.assertEqual(result.raw["capabilities_skipped"], "primary_only")

    async def test_capabilities_use_one_bounded_call_and_keep_roles(self):
        stages: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                return task.official_url, (
                    "<html><body><h1>Turn text into video</h1>"
                    "<p>Generate videos from text in one click.</p>"
                    "<a href='/signup'>Start for free</a></body></html>"
                )

            async def fetch_structured_text_data(
                self, *, source_url, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                stages.append(stage)
                if stage == "shadow_profile_main_content":
                    return source_url, {
                        "entity_kind": "independent_product",
                        "entity_confidence": 0.95,
                        "entity_reason": "Dedicated product",
                        "entity_evidence": [{"quote": "Start for free"}],
                        "primary_job": {
                            "value": "Generate videos",
                            "evidence": [{"quote": "Generate videos from text in one click."}],
                        },
                        "primary_outputs": [],
                        "capabilities_raw": [
                            {
                                "value": "text to video",
                                "evidence": [{"quote": "Turn text into video"}],
                            }
                        ],
                    }
                if stage == "shadow_l1_top2":
                    return source_url, {
                        "l1_candidates": [
                            {"slug": "video", "confidence": 0.9, "reason": "market"}
                        ]
                    }
                if stage == "shadow_leaf":
                    return source_url, {
                        "leaf_slug": "text-to-video",
                        "confidence": 0.88,
                        "reason": "primary job",
                        "evidence": [{"quote": "Generate videos from text in one click."}],
                        "secondary_leaves": [],
                    }
                if stage == "shadow_capabilities":
                    if "text-to-video" not in prompt:
                        raise AssertionError("bounded whitelist omitted text-to-video")
                    return source_url, {
                        "capability_slugs": [
                            {
                                "slug": "text-to-video",
                                "role": "core",
                                "confidence": 0.9,
                                "evidence": [{"quote": "Turn text into video"}],
                            }
                        ]
                    }
                raise AssertionError(f"unexpected stage: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=124,
                canonical_slug="capability-example",
                normalized_domain="capability.test",
                official_url="https://capability.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=True,
            capability_candidate_limit=24,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(stages.count("shadow_capabilities"), 1)
        self.assertEqual(result.capability_slugs, ["text-to-video"])
        self.assertEqual(result.raw["capabilities_accepted"][0]["role"], "core")
        self.assertLessEqual(
            result.raw["capability_candidate_recall"]["candidate_count"], 24
        )

    async def test_leaf_transport_failure_is_retryable_and_skips_capability_cost(self):
        stages: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                return task.official_url, (
                    "<html><body><h1>Turn text into video</h1>"
                    "<a href='/signup'>Start for free</a></body></html>"
                )

            async def fetch_structured_text_data(
                self, *, source_url, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                stages.append(stage)
                if stage == "shadow_profile_main_content":
                    return source_url, {
                        "entity_kind": "independent_product",
                        "entity_confidence": 0.95,
                        "entity_reason": "Dedicated product",
                        "entity_evidence": [{"quote": "Start for free"}],
                        "primary_job": {
                            "value": "Generate videos",
                            "evidence": [{"quote": "Turn text into video"}],
                        },
                        "primary_outputs": [],
                        "capabilities_raw": [
                            {
                                "value": "text to video",
                                "evidence": [{"quote": "Turn text into video"}],
                            }
                        ],
                    }
                if stage == "shadow_l1_top2":
                    return source_url, {
                        "l1_candidates": [
                            {"slug": "video", "confidence": 0.9, "reason": "market"}
                        ]
                    }
                if stage == "shadow_leaf":
                    raise RuntimeError("provider connection reset")
                raise AssertionError(f"unexpected stage: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=129,
                canonical_slug="retry-leaf",
                normalized_domain="retry.test",
                official_url="https://retry.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=True,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("leaf_failed", result.error)
        self.assertNotIn("shadow_capabilities", stages)

    async def test_cursor_kimi_example_domain_evidence_fails_closed(self):
        stages: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                return task.official_url, (
                    "<html><body><h1>Cursor: AI coding agent</h1>"
                    "<p>Build software faster with an intelligent coding assistant.</p>"
                    "</body></html>"
                )

            async def fetch_structured_text_data(
                self, *, source_url, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                stages.append(stage)
                return source_url, {
                    "entity_kind": "non_product",
                    "entity_confidence": 1.0,
                    "entity_reason": "The webpage does not describe a product or service.",
                    "entity_evidence": [
                        {
                            "quote": (
                                "This domain is for use in documentation examples "
                                "without needing permission."
                            )
                        }
                    ],
                    "primary_job": {"value": "", "evidence": []},
                    "primary_outputs": [],
                    "capabilities_raw": [],
                }

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=35,
                canonical_slug="cursor",
                normalized_domain="cursor.com",
                official_url="https://www.cursor.com/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=False,
        )

        self.assertEqual(stages, ["shadow_profile_main_content"])
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.error, "entity_unresolved")
        self.assertEqual(result.entity_kind, "unresolved")
        decision = result.raw["entity_decision"]
        self.assertEqual(decision["candidate_kind"], "non_product")
        self.assertFalse(decision["accepted"])
        self.assertTrue(decision["ungrounded_evidence_detected"])

    async def test_missing_prompt_only_transport_never_falls_back_to_asset_navigation(self):
        asset_calls: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                return task.official_url, (
                    "<html><body><h1>AI product</h1><p>Start for free today.</p></body></html>"
                )

            async def fetch_structured_asset_data(self, task, *, stage, **kwargs):
                asset_calls.append(stage)
                raise AssertionError("asset navigation must not be used for taxonomy classification")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=126,
                canonical_slug="no-transport",
                normalized_domain="product.test",
                official_url="https://product.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=False,
        )

        self.assertEqual(asset_calls, [])
        self.assertEqual(result.status, "failed")
        self.assertIn("structured_text_transport_unavailable", result.error)

    async def test_profile_never_uses_direct_page_when_content_fetch_fails(self):
        calls: list[tuple[str, str]] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                raise RuntimeError("content unavailable")

            async def fetch_structured_asset_data(
                self, task, *, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                calls.append((stage, task.normalized_domain))
                raise AssertionError(f"unexpected structured call: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=124,
                canonical_slug="fallback",
                normalized_domain="fallback.test",
                official_url="https://fallback.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=False,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("profile_extract_failed", result.error)
        self.assertIn("content unavailable", result.error)
        self.assertEqual(calls, [])
        self.assertNotIn("profile_direct_fallback_raw", result.raw)

    async def test_auto_non_product_fetch_failure_demotes_once_without_retry_status(self):
        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                raise RuntimeError("network connection closed")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=127,
                canonical_slug="stale-auto-reject",
                normalized_domain="stale.test",
                official_url="https://stale.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            existing_entity_kind="non_product",
            existing_entity_source="auto",
            include_capabilities=False,
        )

        self.assertEqual(result.entity_kind, "unresolved")
        self.assertEqual(result.status, "partial")
        self.assertIn("profile_extract_failed", result.error)
        self.assertTrue(result.raw["auto_non_product_recheck"])
        self.assertTrue(result.raw["auto_non_product_safely_demoted"])

    async def test_auto_non_product_safe_demotion_is_persisted_as_partial(self):
        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                raise RuntimeError("network connection closed")

        before = {"primary_category_id": 10}
        d1 = object()
        update_entity = AsyncMock()
        insert_run = AsyncMock(return_value=901)
        with (
            patch(
                "taxonomy_shadow.snapshot_legacy_category_state",
                new=AsyncMock(side_effect=[before, before]),
            ),
            patch("taxonomy_shadow.update_tool_entity_kind", new=update_entity),
            patch("taxonomy_shadow.upsert_product_profile", new=AsyncMock()),
            patch("taxonomy_shadow.insert_classification_run", new=insert_run),
        ):
            result = await classify_tool_shadow(
                d1=d1,
                browser_client=FakeBrowser(),
                task=AssetTask(
                    tool_id=128,
                    canonical_slug="persist-demotion",
                    normalized_domain="persist.test",
                    official_url="https://persist.test/",
                    attempts=0,
                    max_attempts=1,
                    generation=0,
                    lease_token="test-shadow",
                ),
                catalog=_catalog(),
                dry_run=False,
                existing_entity_kind="non_product",
                existing_entity_source="auto",
                include_capabilities=False,
            )

        update_entity.assert_awaited_once_with(d1, 128, "unresolved")
        self.assertEqual(insert_run.await_args.kwargs["run_status"], "partial")
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.run_id, 901)

    async def test_taxonomy_excludes_workers_ai_and_uses_deepseek_for_all_stages(self):
        stage_models: dict[str, list[str]] = {}

        class FakeBrowser:
            def category_custom_ai(self):
                return [
                    {"model": "deepseek/deepseek-v4-flash"},
                    {"model": "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
                ]

            async def fetch_homepage_content(self, task):
                return task.official_url, (
                    "<html><body><h1>Generate videos from text</h1>"
                    "<a href='/signup'>Start for free</a></body></html>"
                )

            async def fetch_structured_text_data(
                self, *, source_url, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                stage_models[stage] = [item["model"] for item in custom_ai]
                if stage == "shadow_profile_main_content":
                    return source_url, {
                        "entity_kind": "independent_product",
                        "entity_confidence": 0.95,
                        "entity_reason": "Dedicated product",
                        "entity_evidence": [{"quote": "Start for free"}],
                        "primary_job": {
                            "value": "Generate videos",
                            "evidence": [{"quote": "Generate videos from text"}],
                        },
                        "primary_outputs": [],
                        "capabilities_raw": [],
                    }
                if stage == "shadow_l1_top2":
                    return source_url, {
                        "l1_candidates": [
                            {"slug": "video", "confidence": 0.9, "reason": "market"}
                        ]
                    }
                if stage == "shadow_leaf":
                    return source_url, {
                        "leaf_slug": "text-to-video",
                        "confidence": 0.88,
                        "reason": "primary job",
                        "evidence": [{"quote": "Generate videos from text"}],
                    }
                raise AssertionError(f"unexpected stage: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=129,
                canonical_slug="cost-guard",
                normalized_domain="cost.test",
                official_url="https://cost.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            existing_entity_kind="non_product",
            existing_entity_source="auto",
            include_capabilities=False,
        )

        workers_model = "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            stage_models["shadow_profile_main_content"],
            ["deepseek/deepseek-v4-flash"],
        )
        self.assertEqual(stage_models["shadow_l1_top2"], ["deepseek/deepseek-v4-flash"])
        self.assertEqual(stage_models["shadow_leaf"], ["deepseek/deepseek-v4-flash"])
        self.assertFalse(result.raw["model_policy"]["workers_ai_allowed"])
        self.assertFalse(result.raw["model_policy"]["deepseek_profile_only"])
        self.assertEqual(result.raw["model_policy"]["excluded_models"], [workers_model])

    async def test_taxonomy_never_falls_back_to_workers_ai(self):
        stages: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [
                    {"model": "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast"}
                ]

            async def fetch_homepage_content(self, task):
                raise AssertionError("homepage fetch must not start without a trusted model")

            async def fetch_structured_text_data(
                self, *, source_url, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                stages.append(stage)
                raise AssertionError(f"unexpected stage: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=130,
                canonical_slug="no-fallback",
                normalized_domain="no-fallback.test",
                official_url="https://no-fallback.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            existing_entity_kind="non_product",
            existing_entity_source="auto",
            include_capabilities=False,
        )

        self.assertEqual(stages, [])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "trusted_taxonomy_model_unavailable")
        self.assertFalse(result.raw["model_policy"]["workers_ai_allowed"])

    async def test_antibot_page_is_gated_before_any_model_call(self):
        calls: list[str] = []

        class FakeBrowser:
            def category_custom_ai(self):
                return [{"model": "fake-model"}]

            async def fetch_homepage_content(self, task):
                return (
                    "https://blocked.test/",
                    "<html><title>Just a moment...</title><body>"
                    "Access has been blocked by the firewall."
                    "</body></html>",
                )

            async def fetch_structured_asset_data(
                self, task, *, stage, prompt, json_schema, custom_ai, **kwargs
            ):
                calls.append(stage)
                raise AssertionError(f"unexpected structured call: {stage}")

        result = await classify_tool_shadow(
            d1=object(),
            browser_client=FakeBrowser(),
            task=AssetTask(
                tool_id=125,
                canonical_slug="blocked",
                normalized_domain="blocked.test",
                official_url="https://blocked.test/",
                attempts=0,
                max_attempts=1,
                generation=0,
                lease_token="test-shadow",
            ),
            catalog=_catalog(),
            dry_run=True,
            include_capabilities=False,
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.error, "entity_unresolved")
        self.assertEqual(result.raw["profile_extraction_path"], "page_quality_gate")
        self.assertEqual(result.raw["page_quality"]["state"], "access_denied")
        self.assertEqual(calls, [])


class ProfileEvidenceTests(unittest.TestCase):
    def test_grounding_tolerates_only_rendering_punctuation_drift(self):
        source = "Build — deploy, and monitor AI models in production."
        self.assertTrue(
            evidence_quote_is_grounded(
                "Build - deploy and monitor AI models in production",
                source,
            )
        )
        self.assertFalse(
            evidence_quote_is_grounded(
                "Monitor models before building and deploying them",
                source,
            )
        )

    def test_rejects_bare_string_without_evidence(self):
        self.assertIsNone(normalize_evidenced_value("Generate videos"))

    def test_rejects_value_without_evidence(self):
        self.assertIsNone(
            normalize_evidenced_value({"value": "Generate videos", "evidence": []})
        )

    def test_accepts_value_with_quote(self):
        result = normalize_evidenced_value(
            {
                "value": "Generate videos from text",
                "evidence": [{"quote": "Turn text into video"}],
            },
            source_url="https://example.com/",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["value"], "Generate videos from text")
        self.assertEqual(result["evidence"][0]["quote"], "Turn text into video")
        self.assertEqual(result["evidence"][0]["source_url"], "https://example.com/")

    def test_salvages_inline_evidence_string(self):
        result = normalize_evidenced_value(
            "Create studio-quality videos. Evidence: 'Create studio-quality videos with AI avatars'",
            source_url="https://example.com/",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("studio-quality", result["value"])
        self.assertIn("AI avatars", result["evidence"][0]["quote"])

    def test_build_profile_strips_unevidenced_fields(self):
        profile = build_product_profile(
            {
                "primary_job": {
                    "value": "Make videos",
                    "evidence": [{"quote": "AI video generator"}],
                },
                "primary_outputs": [{"value": "video"}],  # no evidence
                "capabilities_raw": [
                    {
                        "value": "text-to-video",
                        "evidence": [{"quote": "Text to Video"}],
                    }
                ],
            },
            source_url="https://example.com/",
        )
        self.assertIsNotNone(profile["primary_job"])
        self.assertIsNone(profile["primary_outputs"])
        self.assertEqual(len(profile["capabilities_raw"] or []), 1)
        self.assertTrue(profile_has_signal(profile))

    def test_build_profile_accepts_provider_collapsed_singleton_objects(self):
        profile = build_product_profile(
            {
                "primary_job": {
                    "value": "Build computer vision models",
                    "evidence": ["Build and deploy computer vision models"],
                },
                "primary_outputs": {
                    "value": "Deployed computer vision applications",
                    "evidence": ["Go from idea to deployed application"],
                },
                "capabilities_raw": {
                    "value": "AI-assisted labeling; edge inference",
                    "evidence": [
                        "Label images fast with AI-assisted data annotation",
                        "Run inference across a fleet of edge devices",
                    ],
                },
            },
            source_url="https://example.com/",
        )

        self.assertEqual(len(profile["primary_outputs"] or []), 1)
        self.assertEqual(len(profile["capabilities_raw"] or []), 1)
        capability = (profile["capabilities_raw"] or [])[0]
        self.assertEqual(len(capability["evidence"]), 2)

    def test_build_profile_drops_product_facts_not_grounded_in_source_text(self):
        profile = build_product_profile(
            {
                "primary_job": {
                    "value": "Placeholder documentation site",
                    "evidence": [
                        {
                            "quote": (
                                "This domain is for use in documentation examples "
                                "without needing permission."
                            )
                        }
                    ],
                },
                "primary_outputs": [],
                "capabilities_raw": [],
            },
            source_url="https://www.cursor.com/",
            source_text="Cursor: AI coding agent. Build software faster.",
        )

        self.assertIsNone(profile["primary_job"])
        self.assertFalse(profile_has_signal(profile))


class PrimaryTop2Tests(unittest.TestCase):
    def test_shadow_primary_is_non_effective_when_human_primary_is_locked(self):
        self.assertEqual(auto_primary_write_state(0.92, 123), (False, "superseded"))

    def test_confident_shadow_primary_is_auto_accepted_without_human_lock(self):
        self.assertEqual(auto_primary_write_state(0.92, None), (True, "auto_accepted"))

    def test_below_auto_accept_threshold_stays_in_exception_review(self):
        self.assertEqual(auto_primary_write_state(0.49, None), (True, "provisional"))

    def test_parse_top2_whitelist_and_limit(self):
        catalog = _catalog()
        hits = parse_top2_l1(
            {
                "l1_candidates": [
                    {"slug": "video", "confidence": 0.9, "reason": "main"},
                    {"slug": "image", "confidence": 0.4, "reason": "secondary"},
                    {"slug": "not-a-real", "confidence": 0.9, "reason": "junk"},
                    {"slug": "video", "confidence": 0.1, "reason": "dup"},
                ]
            },
            catalog,
        )
        self.assertEqual([h["term"].slug for h in hits], ["video", "image"])

    def test_leaf_pool_merges_children(self):
        catalog = _catalog()
        hits = parse_top2_l1(
            {
                "l1_candidates": [
                    {"slug": "video", "confidence": 0.8, "reason": "a"},
                    {"slug": "image", "confidence": 0.5, "reason": "b"},
                ]
            },
            catalog,
        )
        pool = build_leaf_candidate_pool(hits, catalog)
        slugs = {t.slug for t in pool}
        self.assertEqual(slugs, {"text-to-video", "image-generation"})

    def test_parse_leaf_accepts_pool_member(self):
        catalog = _catalog()
        hits = parse_top2_l1(
            {"l1_candidates": [{"slug": "video", "confidence": 0.9, "reason": "x"}]},
            catalog,
        )
        pool = build_leaf_candidate_pool(hits, catalog)
        decision = parse_leaf_decision(
            {
                "leaf_slug": "text-to-video",
                "confidence": 0.77,
                "reason": "homepage hero",
                "evidence": [{"quote": "Text to Video AI"}],
            },
            pool,
            catalog,
            source_url="https://example.com/",
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["term"].slug, "text-to-video")
        self.assertAlmostEqual(decision["confidence"], 0.77)
        self.assertEqual(decide_primary_status(0.77), "auto_accepted")
        self.assertEqual(decide_primary_status(0.49), "provisional")
        self.assertEqual(decide_primary_status(0.1), "unresolved")

    def test_parse_leaf_rejects_unknown(self):
        catalog = _catalog()
        hits = parse_top2_l1(
            {"l1_candidates": [{"slug": "video", "confidence": 0.9, "reason": "x"}]},
            catalog,
        )
        pool = build_leaf_candidate_pool(hits, catalog)
        self.assertIsNone(
            parse_leaf_decision(
                {"leaf_slug": "image-generation", "confidence": 0.9, "reason": "wrong pool"},
                pool,
                catalog,
            )
        )

    def test_parses_distinct_grounded_secondary_markets(self):
        catalog = _catalog()
        hits = parse_top2_l1(
            {
                "l1_candidates": [
                    {"slug": "video", "confidence": 0.9, "reason": "main"},
                    {"slug": "image", "confidence": 0.7, "reason": "secondary"},
                ]
            },
            catalog,
        )
        pool = build_leaf_candidate_pool(hits, catalog)
        decisions = parse_secondary_leaf_decisions(
            {
                "secondary_leaves": [
                    {
                        "slug": "image-generation",
                        "confidence": 0.72,
                        "reason": "also generates images",
                        "evidence": [{"quote": "Generate images and videos"}],
                    },
                    {
                        "slug": "text-to-video",
                        "confidence": 0.8,
                        "reason": "duplicate primary",
                        "evidence": [{"quote": "Generate images and videos"}],
                    },
                ]
            },
            pool,
            catalog,
            primary_slug="text-to-video",
            source_text="Generate images and videos",
        )
        self.assertEqual(
            [decision["term"].slug for decision in decisions],
            ["image-generation"],
        )


class CapabilityTests(unittest.TestCase):
    def test_recalls_market_atomic_tasks_and_profile_matches_with_a_hard_limit(self):
        catalog = _catalog()
        market = catalog.get("primary_category", "text-to-video")
        assert market is not None
        candidates = recall_capability_candidates(
            {
                "capabilities_raw": [
                    {
                        "value": "Generate code and video",
                        "evidence": [{"quote": "Generate code and video"}],
                    }
                ]
            },
            catalog,
            markets=[market],
            limit=12,
        )
        slugs = [term.slug for term in candidates]
        self.assertIn("text-to-video", slugs)
        self.assertIn("image-to-video", slugs)
        self.assertIn("code-generation", slugs)
        self.assertLessEqual(len(slugs), 12)

    def test_capability_terms_are_chunked_without_loss(self):
        terms = [
            TaxonomyTerm(
                term_id=1000 + index,
                dimension="capability",
                slug=f"capability-{index:03d}",
                name=f"Capability {index}",
            )
            for index in range(791)
        ]

        chunks = capability_term_chunks(terms, chunk_size=160)

        self.assertEqual([len(chunk) for chunk in chunks], [160, 160, 160, 160, 151])
        self.assertEqual([term.slug for chunk in chunks for term in chunk], [term.slug for term in terms])

    def test_capability_chunk_results_merge_by_confidence_and_limit(self):
        catalog = _catalog()
        text_to_video = catalog.get("capability", "text-to-video")
        image_to_video = catalog.get("capability", "image-to-video")
        assert text_to_video is not None and image_to_video is not None

        merged = merge_capability_decisions(
            [
                [{"term": text_to_video, "confidence": 0.6, "evidence": []}],
                [
                    {"term": text_to_video, "confidence": 0.9, "evidence": [{"quote": "best"}]},
                    {"term": image_to_video, "confidence": 0.7, "evidence": []},
                ],
            ]
        )

        self.assertEqual([item["term"].slug for item in merged], ["text-to-video", "image-to-video"])
        self.assertEqual(merged[0]["confidence"], 0.9)

    def test_capability_prompt_renders_full_v2_sized_whitelist(self):
        catalog = _catalog()
        capabilities = [
            TaxonomyTerm(
                term_id=1000 + index,
                dimension="capability",
                slug=f"capability-{index:03d}",
                name=f"Capability name {index:03d}",
                taxonomy_version=2,
            )
            for index in range(791)
        ]
        prompt = capabilities_prompt(capabilities, catalog, {})

        self.assertIn("capability-000", prompt)
        self.assertIn("capability-790", prompt)
        self.assertIn("name=Capability name 790", prompt)

    def test_capability_prompt_includes_profile_evidence_and_object_contract(self):
        catalog = _catalog()
        prompt = capabilities_prompt(
            catalog.capabilities(),
            catalog,
            {
                "primary_job": {"value": "Deploy vision models"},
                "capabilities_raw": [
                    {
                        "value": "edge inference",
                        "evidence": [
                            {
                                "quote": "Run inference across a fleet of edge devices"
                            }
                        ],
                    }
                ],
            },
        )

        self.assertIn("capabilities_raw_json=", prompt)
        self.assertIn("Run inference across a fleet of edge devices", prompt)
        self.assertIn("never bare strings", prompt)

    def test_capabilities_require_evidence_and_whitelist(self):
        catalog = _catalog()
        caps = parse_capabilities(
            {
                "capability_slugs": [
                    {
                        "slug": "text-to-video",
                        "confidence": 0.8,
                        "evidence": [{"quote": "text to video"}],
                    },
                    {
                        "slug": "code-generation",
                        "confidence": 0.9,
                        # no evidence -> drop
                    },
                    {
                        "slug": "not-real",
                        "confidence": 0.9,
                        "evidence": [{"quote": "x"}],
                    },
                    {
                        "slug": "image-to-video",
                        "confidence": 0.2,  # below threshold
                        "evidence": [{"quote": "image to video"}],
                    },
                ]
            },
            catalog,
            source_url="https://example.com/",
        )
        self.assertEqual([c["term"].slug for c in caps], ["text-to-video"])
        self.assertEqual(caps[0]["role"], "supporting")

    def test_capability_role_and_grounded_evidence_are_preserved(self):
        catalog = _catalog()
        caps = parse_capabilities(
            {
                "capability_slugs": [
                    {
                        "slug": "text-to-video",
                        "role": "core",
                        "confidence": 0.91,
                        "evidence": [{"quote": "Turn text into video"}],
                    },
                    {
                        "slug": "image-to-video",
                        "role": "supporting",
                        "confidence": 0.8,
                        "evidence": [{"quote": "Not on the homepage"}],
                    },
                ]
            },
            catalog,
            source_text="Turn text into video in one click",
        )
        self.assertEqual(
            [(cap["term"].slug, cap["role"]) for cap in caps],
            [("text-to-video", "core")],
        )

    def test_bare_string_capabilities_are_retry_candidates_not_assignments(self):
        catalog = _catalog()
        raw = {
            "capability_slugs": [
                "text-to-video",
                "not-real",
                "text-to-video",
            ]
        }

        self.assertEqual(parse_capabilities(raw, catalog), [])
        self.assertEqual(
            [term.slug for term in capability_retry_terms(raw, catalog)],
            ["text-to-video"],
        )


if __name__ == "__main__":
    unittest.main()
