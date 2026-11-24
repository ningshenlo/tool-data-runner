"""Regression tests for Scheme A deterministic naming, clean domain slugs, and hard gates."""

from __future__ import annotations

import unittest

import runner
from libs.domains import extract_domain_brand_parts, format_domain_brand_name
from tests import test_runner_stores as fixtures
from workloads.assets.browser import CloudflareBrowserRunAssetClient
from workloads.assets.content import (
    invalid_tool_name_reason,
    public_tool_slug_candidates,
    resolve_tool_name,
)
from workloads.assets.types import AssetFetchResult, AssetTask, PageQualityAssessment


class DomainBrandSchemeATests(unittest.TestCase):
    def test_extract_and_format_domain_brand_parts(self) -> None:
        self.assertEqual(extract_domain_brand_parts("imagetopsd.com"), ("imagetopsd", "com"))
        self.assertEqual(extract_domain_brand_parts("imagetopsd.ai"), ("imagetopsd", "ai"))
        self.assertEqual(format_domain_brand_name("imagetopsd.com"), "Imagetopsd")
        self.assertEqual(format_domain_brand_name("imagetopsd.com", include_tld=True), "Imagetopsd.com")
        self.assertEqual(format_domain_brand_name("imagetopsd.ai", include_tld=True), "Imagetopsd.ai")

        self.assertEqual(format_domain_brand_name("d-id.com"), "D-ID")
        self.assertEqual(format_domain_brand_name("www.cursor.com"), "Cursor")
        self.assertEqual(format_domain_brand_name("app.notion.so"), "Notion")
        self.assertEqual(format_domain_brand_name("chat.deepseek.com"), "Deepseek")

        self.assertEqual(extract_domain_brand_parts("mytool.vercel.app"), ("mytool", "vercel-app"))
        self.assertEqual(format_domain_brand_name("mytool.vercel.app"), "Mytool")
        self.assertEqual(extract_domain_brand_parts("demo.lovable.app"), ("demo", "lovable-app"))

        self.assertEqual(extract_domain_brand_parts("gemini.google.com"), ("google-gemini", "com"))
        self.assertEqual(format_domain_brand_name("gemini.google.com"), "Google Gemini")
        self.assertEqual(format_domain_brand_name("copilot.microsoft.com"), "Microsoft Copilot")

        self.assertEqual(extract_domain_brand_parts("cooltool.co.uk"), ("cooltool", "co-uk"))
        self.assertEqual(format_domain_brand_name("cooltool.co.uk"), "Cooltool")
        self.assertEqual(format_domain_brand_name("cooltool.co.uk", include_tld=True), "Cooltool.co.uk")


class ResolveToolNameSchemeATests(unittest.TestCase):
    def test_scheme_a_priority_and_strict_title_guards(self) -> None:
        # 1. og:site_name beats model_product_name and marketing <title>
        og_res = resolve_tool_name(
            '<html><head><meta property="og:site_name" content="Acme Studio">'
            '<title>Best Free AI Video Tool | Blog</title></head></html>',
            "acme.ai",
            model_product_name="ModelHint",
        )
        self.assertEqual(og_res.product_name, "Acme Studio")
        self.assertEqual(og_res.source, "open_graph_site_name")

        # 2. JSON-LD beats <title> when og:site_name is absent
        ld_res = resolve_tool_name(
            '<html><head><title>Generate images fast - Home</title>'
            '<script type="application/ld+json">{"@type":"SoftwareApplication","name":"Acme Pro","url":"https://acme.ai"}</script>'
            "</head></html>",
            "acme.ai",
            model_product_name="ModelHint",
        )
        self.assertEqual(ld_res.product_name, "Acme Pro")
        self.assertEqual(ld_res.source, "json_ld_product")

        # 3. <title> only accepts a domain-matched segment (rejects blind shortest segment like "Blog")
        title_match = resolve_tool_name(
            "<title>Free AI Writing Assistant | Acme AI</title>",
            "acme.ai",
        )
        self.assertEqual(title_match.product_name, "Acme AI")
        self.assertEqual(title_match.source, "page_title_segment")

        blind_title = resolve_tool_name(
            "<title>How To Write Prompts Fast - Blog</title>",
            "acme.ai",
        )
        self.assertEqual(blind_title.product_name, "Acme")
        self.assertEqual(blind_title.source, "domain_fallback")

        # 4. Clean model_product_name is used when structured HTML & domain-matched title are absent
        hint_res = resolve_tool_name(
            "<title>Create faster workflows today</title>",
            "acme.ai",
            model_product_name="AcmeFlow",
        )
        self.assertEqual(hint_res.product_name, "AcmeFlow")
        self.assertEqual(hint_res.source, "submitted_name")

    def test_invalid_tool_name_guards_and_domain_aware_untitled(self) -> None:
        self.assertEqual(invalid_tool_name_reason("Just a moment...", "acme.ai"), "anti_bot_name")
        self.assertEqual(invalid_tool_name_reason("Access Denied", "acme.ai"), "access_denied_name")
        self.assertEqual(invalid_tool_name_reason("Generate anything easily...", "acme.ai"), "truncated_page_title")
        self.assertEqual(invalid_tool_name_reason("Acme | Best AI Tool", "acme.ai"), "unsplit_page_title")
        self.assertEqual(invalid_tool_name_reason("Six Words In One Product Title", "acme.ai"), "name_too_many_words")
        self.assertEqual(invalid_tool_name_reason("A" * 51, "acme.ai"), "name_too_long")

        # Domain-aware Untitled check
        self.assertEqual(invalid_tool_name_reason("Untitled", "acme.ai"), "generic_page_name")
        self.assertEqual(invalid_tool_name_reason("Untitled", "untitled.ai"), "")


class PublicSlugCandidatesTests(unittest.TestCase):
    def test_slug_candidates_and_reserved_handling(self) -> None:
        task_ai = AssetTask(1, "imagetopsd-deadbeef", "imagetopsd.ai", "https://imagetopsd.ai/", 1, 5, 1, "l1")
        cands = public_tool_slug_candidates(task_ai)
        self.assertEqual(cands[:4], ["imagetopsd", "imagetopsd-ai", "imagetopsd-ai-2", "imagetopsd-ai-3"])
        self.assertTrue(all(len(c) <= 40 for c in cands))

        task_sub = AssetTask(2, "app-hocoos-12345678", "app.hocoos.com", "https://app.hocoos.com/", 1, 5, 1, "l2")
        sub_cands = public_tool_slug_candidates(task_sub)
        self.assertEqual(sub_cands[:4], ["hocoos", "hocoos-com", "app-hocoos-com", "hocoos-com-2"])

        for domain, first_expected in [("home.ai", "home-ai"), ("admin.com", "admin-com"), ("pro.ai", "pro-ai")]:
            t = AssetTask(3, "reserved-deadbeef", domain, f"https://{domain}/", 1, 5, 1, "l3")
            res = public_tool_slug_candidates(t)
            self.assertEqual(res[0], first_expected)
            self.assertNotIn(domain.split(".")[0], res)


class SchemeAStoreSafetyAndGateTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RunnerStoreLifecycleTests.setUp
    tearDown = fixtures.RunnerStoreLifecycleTests.tearDown
    add_tool = fixtures.RunnerStoreLifecycleTests.add_tool
    seed_primary_taxonomy = fixtures.RunnerStoreLifecycleTests.seed_primary_taxonomy
    seed_complete_enrichment = fixtures.RunnerStoreLifecycleTests.seed_complete_enrichment
    seed_publishable_tool = fixtures.RunnerStoreLifecycleTests.seed_publishable_tool

    async def test_domain_fallback_collision_appends_tld_to_display_name_and_slug(self) -> None:
        existing_id = self.add_tool("imagetopsd-com-11112222")
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, feature_highlights,
              translation_status, published_at, name_source, name_review_status
            ) VALUES (?, 'en', 'imagetopsd', 'Imagetopsd', '[]', 'published', ?, 'domain_fallback', 'auto_approved')
            """,
            [existing_id, runner.utc_now_iso()],
        )
        new_id = self.add_tool("imagetopsd-ai-33334444")
        self.connection.execute(
            "UPDATE tools SET normalized_domain = 'imagetopsd.ai', official_url = 'https://imagetopsd.ai/' WHERE id = ?",
            [new_id],
        )
        self.connection.commit()

        task = AssetTask(new_id, "imagetopsd-ai-33334444", "imagetopsd.ai", "https://imagetopsd.ai/", 1, 5, 1, "lease")
        result = AssetFetchResult(
            final_url=task.official_url,
            title="Imagetopsd",
            page_title="Best Online Image Converter | Home",
            description="Convert images to PSD.",
            name_source="domain_fallback",
            name_confidence=80,
            name_review_status="auto_approved",
        )
        await runner.D1AssetStore(self.d1).save_tool_localization(task, result)

        row = self.connection.execute(
            "SELECT localized_slug, name FROM tool_localizations WHERE tool_id = ? AND locale_code = 'en'",
            [new_id],
        ).fetchone()
        self.assertEqual(row["localized_slug"], "imagetopsd-ai")
        self.assertEqual(row["name"], "Imagetopsd.ai")

    async def test_static_homepage_core_metadata_flags_explicit_nsfw(self) -> None:
        client = CloudflareBrowserRunAssetClient.__new__(CloudflareBrowserRunAssetClient)
        client._validated_pages = {
            8967: (
                "https://joi.com/",
                "<html><head><title>Gay Porn Generator — Free AI NSFW</title>"
                '<meta name="description" content="Create AI gay porn videos">'
                "</head><body><h1>Gay Porn Generator</h1></body></html>",
                PageQualityAssessment("valid_product_page", "ok", ""),
            )
        }
        task = AssetTask(8967, "joi-deadbeef", "joi.com", "https://joi.com/", 1, 5, 1, "lease-nsfw")
        result = await client.fetch_homepage_core_metadata(task)
        self.assertEqual(result.content_safety_status, "nsfw")
        self.assertGreaterEqual(result.content_safety_score, 95)

    async def test_auto_publish_blocks_dirty_name_hex8_slug_and_unsafe_status(self) -> None:
        clean_id = self.add_tool("clean-tool", status="pending_review")
        self.seed_publishable_tool(clean_id, "clean-tool")

        dirty_name_id = self.add_tool("dirty-name", status="pending_review")
        self.seed_publishable_tool(dirty_name_id, "dirty-name")
        self.connection.execute(
            "UPDATE tool_localizations SET name = 'Foo | Best Free AI Tool' WHERE tool_id = ?",
            [dirty_name_id],
        )

        hex8_slug_id = self.add_tool("cooltool-a1b2c3d4", status="pending_review")
        self.seed_publishable_tool(hex8_slug_id, "cooltool")
        self.connection.execute(
            "UPDATE tool_localizations SET localized_slug = 'cooltool-a1b2c3d4' WHERE tool_id = ?",
            [hex8_slug_id],
        )

        unsafe_id = self.add_tool("nsfw-tool", status="pending_review")
        self.seed_publishable_tool(unsafe_id, "nsfw-tool")
        self.connection.execute(
            "UPDATE tools SET content_safety_status = 'nsfw' WHERE id = ?",
            [unsafe_id],
        )

        review_id = self.add_tool("review-safety-tool", status="pending_review")
        self.seed_publishable_tool(review_id, "review-safety-tool")
        self.connection.execute(
            "UPDATE tools SET content_safety_status = 'needs_review' WHERE id = ?",
            [review_id],
        )
        self.connection.commit()

        publish_summary = await runner.D1CatalogPublisher(self.d1, "test").publish_ready(20)
        self.assertEqual(publish_summary, {"selected": 1, "published": 1, "skipped": 0})

        statuses = {
            row["id"]: row["status"]
            for row in self.connection.execute(
                "SELECT id, status FROM tools WHERE id IN (?, ?, ?, ?, ?)",
                [clean_id, dirty_name_id, hex8_slug_id, unsafe_id, review_id],
            ).fetchall()
        }
        self.assertEqual(statuses[clean_id], "published")
        self.assertEqual(statuses[dirty_name_id], "pending_review")
        self.assertEqual(statuses[hex8_slug_id], "pending_review")
        self.assertEqual(statuses[unsafe_id], "pending_review")
        self.assertEqual(statuses[review_id], "pending_review")


class PlanNameSlugCleanupTests(unittest.TestCase):
    def test_build_cleanup_plan_scheme_a_rules_and_collisions(self) -> None:
        from ops.plan_name_slug_cleanup import build_cleanup_plan

        rows = [
            {
                "tool_id": 101,
                "status": "published",
                "normalized_domain": "imagetopsd.com",
                "official_url": "https://imagetopsd.com/",
                "canonical_slug": "imagetopsd-com-11112222",
                "localized_slug": "imagetopsd",
                "name": "Imagetopsd",
                "name_source": "domain_fallback",
                "name_review_status": "auto_approved",
                "monthly_visits": 50000,
            },
            {
                "tool_id": 102,
                "status": "published",
                "normalized_domain": "imagetopsd.ai",
                "official_url": "https://imagetopsd.ai/",
                "canonical_slug": "imagetopsd-ai-33334444",
                "localized_slug": "best-psd-converter-deadbeef",
                "name": "Best Free PSD Converter Online | ImageToPSD",
                "name_source": "discovery_direct",
                "name_review_status": "auto_approved",
                "monthly_visits": 2000,
            },
            {
                "tool_id": 184,
                "status": "published",
                "normalized_domain": "character.ai",
                "official_url": "https://character.ai/",
                "canonical_slug": "character-ai",
                "localized_slug": "access-denied-2",
                "name": "Character Ai",
                "name_source": "domain_fallback",
                "name_review_status": "auto_approved",
                "monthly_visits": 190000000,
            },
            {
                "tool_id": 6059,
                "status": "published",
                "normalized_domain": "maxai.me",
                "official_url": "https://maxai.me/",
                "canonical_slug": "maxai-me",
                "localized_slug": "maxai-me-2",
                "name": "MaxAI",
                "name_source": "open_graph_site_name",
                "name_review_status": "auto_approved",
                "monthly_visits": 2400000,
            },
            {
                "tool_id": 518,
                "status": "published",
                "normalized_domain": "maxai.co",
                "official_url": "https://maxai.co/",
                "canonical_slug": "maxai-co",
                "localized_slug": "maxai-2",
                "name": "MaxAI",
                "name_source": "open_graph_site_name",
                "name_review_status": "auto_approved",
                "monthly_visits": 1300000,
            },
            {
                "tool_id": 3207,
                "status": "published",
                "normalized_domain": "app.maxai.me",
                "official_url": "https://app.maxai.me/",
                "canonical_slug": "app-maxai-me",
                "localized_slug": "maxai",
                "name": "MaxAI.me",
                "name_source": "submitted_name",
                "name_review_status": "auto_approved",
                "monthly_visits": 900000,
            },
            {
                "tool_id": 8779,
                "status": "published",
                "normalized_domain": "easymusic.ai",
                "official_url": "https://easymusic.ai/",
                "canonical_slug": "easymusic-99887766",
                "localized_slug": "ai-rap-video-generator-99887766",
                "name": "AI Rap Video Generator",
                "name_source": "discovery_direct",
                "name_review_status": "auto_approved",
                "monthly_visits": 768121,
            },
            {
                "tool_id": 9991,
                "status": "pending_review",
                "normalized_domain": "unlocalized.ai",
                "official_url": "https://unlocalized.ai/",
                "canonical_slug": "unlocalized-ai",
                "localized_slug": "",
                "name": "",
                "name_source": "",
                "name_review_status": "",
                "monthly_visits": 1000,
            },
            {
                "tool_id": 9992,
                "status": "published",
                "normalized_domain": "handcrafted.io",
                "official_url": "https://handcrafted.io/",
                "canonical_slug": "handcrafted-io",
                "localized_slug": "handcrafted",
                "name": "HandCrafted Studio | Custom Reviewed",
                "name_source": "manual",
                "name_review_status": "human_approved",
                "monthly_visits": 500,
            },
        ]
        aliases = [{"slug": "character", "tool_id": 99999, "created_at": "2026-09-01T00:00:00Z"}]
        inactive = [{"tool_id": 88888, "status": "rejected", "normalized_domain": "old-easy.com", "localized_slug": "easymusic"}]

        plan = build_cleanup_plan(rows, alias_rows=aliases, inactive_slug_rows=inactive)
        by_id = {r["tool_id"]: r for r in plan["plan_rows"]}

        self.assertEqual(by_id[101]["proposed_name"], "Imagetopsd.com")
        self.assertEqual(by_id[101]["proposed_slug"], "imagetopsd")
        self.assertEqual(by_id[102]["proposed_name"], "ImageToPSD.ai")
        self.assertEqual(by_id[102]["proposed_slug"], "imagetopsd-ai")

        self.assertEqual(by_id[184]["proposed_slug"], "character")
        self.assertEqual(by_id[6059]["proposed_name"], "MaxAI.me")
        self.assertEqual(by_id[518]["proposed_name"], "MaxAI.co")
        self.assertEqual(by_id[3207]["proposed_name"], "App.maxai.me")

        proposed_active_names = [r["proposed_name"].lower() for r in plan["plan_rows"] if r["proposed_name"]]
        self.assertEqual(len(proposed_active_names), len(set(proposed_active_names)))

        self.assertEqual(by_id[8779]["proposed_name"], "Easymusic")
        self.assertEqual(by_id[8779]["proposed_slug"], "easymusic")
        self.assertEqual(by_id[9991]["name_action"], "skip_unlocalized")
        self.assertEqual(by_id[9992]["proposed_name"], "HandCrafted Studio | Custom Reviewed")

        conflict_slugs = {(c["conflict_type"], c["desired_slug"], c["winner_tool_id"]) for c in plan["squatter_conflicts"]}
        self.assertIn(("tool_slug_alias", "character", 184), conflict_slugs)
        self.assertIn(("inactive_tool_localization", "easymusic", 8779), conflict_slugs)

    def test_planner_avoids_canonical_slug_and_active_alias_owners_and_validates(self) -> None:
        from ops.apply_name_slug_cleanup import validate_cleanup_plan
        from ops.plan_name_slug_cleanup import build_cleanup_plan

        rows = [
            {
                "tool_id": 8662,
                "status": "pending_review",
                "normalized_domain": "chat.crushon.ai",
                "official_url": "https://chat.crushon.ai/",
                "canonical_slug": "chat-crushon-ai-deadbeef",
                "localized_slug": "ai-boyfriend-deadbeef",
                "name": "Crushon AI Chat",
                "name_source": "discovery_direct",
                "name_review_status": "auto_approved",
                "monthly_visits": 100000,
            },
            {
                "tool_id": 2259,
                "status": "published",
                "normalized_domain": "nanopic.app",
                "official_url": "https://nanopic.app/",
                "canonical_slug": "nanopic-app",
                "localized_slug": "nano-banana-pro",
                "name": "Nano Banana Pro",
                "name_source": "open_graph_site_name",
                "name_review_status": "auto_approved",
                "monthly_visits": 50000,
            },
            {
                "tool_id": 9170,
                "status": "published",
                "normalized_domain": "nano-banana-pro.ai",
                "official_url": "https://nano-banana-pro.ai/",
                "canonical_slug": "nano-banana-pro-ai-11223344",
                "localized_slug": "nano-banana-pro-11223344",
                "name": "Nano Banana Pro AI",
                "name_source": "open_graph_site_name",
                "name_review_status": "auto_approved",
                "monthly_visits": 30000,
            },
        ]
        canon_owners = {"crushon": 190, "nano-banana-pro-ai-11223344": 9170, "nanopic-app": 2259}
        active_aliases = {"nano-banana-pro": {2259}}
        active_slugs = {
            "ai-boyfriend-deadbeef": 8662,
            "nano-banana-pro": 2259,
            "nano-banana-pro-11223344": 9170,
        }

        plan = build_cleanup_plan(
            rows,
            canonical_slug_owners=canon_owners,
            active_alias_owners=active_aliases,
        )
        by_id = {r["tool_id"]: r for r in plan["plan_rows"]}
        # Rejected tool #190 holds canonical_slug 'crushon', so chat.crushon.ai falls back cleanly to 'crushon-ai'.
        self.assertEqual(by_id[8662]["proposed_slug"], "crushon-ai")
        # Published active tool #2259 holds 'nano-banana-pro', so nano-banana-pro.ai falls back to 'nano-banana-pro-ai'.
        self.assertEqual(by_id[9170]["proposed_slug"], "nano-banana-pro-ai")

        summary = validate_cleanup_plan(
            plan,
            canonical_slug_owners=canon_owners,
            active_slug_owners=active_slugs,
            active_alias_owners=active_aliases,
        )
        self.assertEqual(summary["summary"]["changed_tools"], 2)
        self.assertEqual(summary["summary"]["reclaim_aliases"], 0)

        # Pre-flight guard rejects conflicting active alias overrides.
        bad_plan = {"plan_rows": [dict(by_id[9170], proposed_slug="nano-banana-pro")], "squatter_conflicts": []}
        with self.assertRaises(ValueError):
            validate_cleanup_plan(
                bad_plan,
                canonical_slug_owners=canon_owners,
                active_slug_owners=active_slugs,
                active_alias_owners=active_aliases,
            )
