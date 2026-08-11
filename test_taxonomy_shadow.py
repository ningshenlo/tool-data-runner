"""Unit tests for P2A Shadow Mode pure helpers (no network / D1)."""

from __future__ import annotations

import sqlite3
import unittest

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
    classify_tool_shadow,
    decide_primary_status,
    load_shadow_tasks,
    merge_capability_decisions,
    normalize_evidenced_value,
    parse_capabilities,
    parse_entity_decision,
    parse_leaf_decision,
    parse_top2_l1,
    profile_has_signal,
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

    async def test_batch_query_skips_tools_already_run_by_current_prompt(self):
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
        self.assertIn("current_run.prompt_version = ?", sql)
        self.assertIn("current_run.run_status IN ('succeeded', 'partial', 'skipped')", sql)
        self.assertIn("failed_run.run_status = 'failed'", sql)
        self.assertIn("entity_kind_source", sql)
        self.assertEqual(
            observed["params"],
            [500, SHADOW_PROMPT_VERSION, SHADOW_PROMPT_VERSION, 100],
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
                SHADOW_PROMPT_VERSION,
                "deepseek/deepseek-v4-flash",
                20,
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


class PrimaryOnlyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_only_never_calls_capability_stages(self):
        stages: list[str] = []
        structured_domains: list[str] = []
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

            async def fetch_structured_asset_data(
                self,
                task,
                *,
                stage,
                prompt,
                json_schema,
                custom_ai,
                **kwargs,
            ):
                stages.append(stage)
                structured_domains.append(task.normalized_domain)
                prompts[stage] = prompt
                if stage == "shadow_profile_visible_text":
                    return task.official_url, {
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
                    return "https://example.com/", {
                        "l1_candidates": [
                            {"slug": "video", "confidence": 0.9, "reason": "main market"}
                        ]
                    }
                if stage == "shadow_leaf":
                    return "https://example.com/", {
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
            ["shadow_profile_visible_text", "shadow_l1_top2", "shadow_leaf"],
        )
        self.assertEqual(content_urls, ["https://product.test/"])
        self.assertEqual(structured_domains, ["example.com", "example.com", "example.com"])
        self.assertIn("Generate videos", prompts["shadow_l1_top2"])
        self.assertIn("Generate videos", prompts["shadow_leaf"])
        self.assertEqual(result.raw["profile_extraction_path"], "visible_text_primary")
        self.assertEqual(result.raw["classification_transport"], "neutral_profile_only")
        self.assertEqual(result.raw["capabilities_skipped"], "primary_only")

    async def test_profile_uses_direct_page_only_when_content_fetch_fails(self):
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
                if stage == "shadow_profile_direct_fallback":
                    return task.official_url, {
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
                    return task.official_url, {
                        "l1_candidates": [
                            {"slug": "video", "confidence": 0.9, "reason": "main market"}
                        ]
                    }
                if stage == "shadow_leaf":
                    return task.official_url, {
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

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.raw["profile_extraction_path"], "direct_page_fallback")
        self.assertEqual(
            calls,
            [
                ("shadow_profile_direct_fallback", "fallback.test"),
                ("shadow_l1_top2", "example.com"),
                ("shadow_leaf", "example.com"),
            ],
        )


class ProfileEvidenceTests(unittest.TestCase):
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


class CapabilityTests(unittest.TestCase):
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
