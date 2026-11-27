import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from workloads.enrichment.clairic_pricing import (
    build_clairic_pricing_request,
    normalize_clairic_pricing_result,
    run_static_pricing_extraction,
)
from workloads.enrichment.pricing_locator import (
    LocatedPricingPage,
    discover_contact_sales_urls,
    discover_pricing_urls,
    is_bad_pricing_url,
    pricing_url_score,
)


class StaticPricingEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    def test_pricing_url_discovery_filters_noise_and_scores_canonical_paths(self):
        html = """
        <html><body>
          <a href="/blog/pricing-update">Blog</a>
          <a href="/privacy-policy">Privacy</a>
          <a href="/docs/api">Docs</a>
          <a href="/plans">Plans</a>
          <a href="/pricing">Pricing</a>
          <a href="/contact-sales">Contact Sales</a>
        </body></html>
        """
        urls = discover_pricing_urls("https://example.ai", html)
        self.assertTrue(urls[0].endswith("/pricing"))
        self.assertIn("https://example.ai/plans", urls)
        for bad in ("https://example.ai/blog/pricing-update", "https://example.ai/privacy-policy", "https://example.ai/docs/api"):
            self.assertNotIn(bad, urls)
        self.assertTrue(is_bad_pricing_url("https://example.ai/blog/pricing"))
        self.assertGreater(pricing_url_score("https://example.ai/pricing"), pricing_url_score("https://example.ai/plans"))

        contact = discover_contact_sales_urls("https://example.ai", html)
        self.assertIn("https://example.ai/contact-sales", contact)

    def test_clairic_normalization_enforces_p_null_threshold_and_formats_notes(self):
        locator = LocatedPricingPage(
            url="https://example.ai/pricing",
            final_url="https://example.ai/pricing",
            status="found",
            method="candidate_scored",
            quality_score=18,
            text="Starter Free forever. Pro €20/month.",
        )
        req = build_clairic_pricing_request(locator.text, locator.final_url)
        self.assertEqual(req["model"], "clairic")
        self.assertIn("starting_price_amount", req["fields"])

        good_raw = {
            "answers": {
                "pricing_model": {"type": "enum", "value": "freemium", "valid": True, "p_null": 0.01},
                "has_free_plan": {"type": "boolean", "value": True, "valid": True, "p_null": 0.01},
                "has_free_trial": {"type": "boolean", "value": False, "valid": True, "p_null": 0.05},
                "starting_price_amount": {"type": "number", "value": 20, "valid": True, "p_null": 0.02},
                "pricing_currency": {"type": "text", "value": "€", "valid": True, "p_null": 0.02},
                "billing_interval": {"type": "enum", "value": "month", "valid": True, "p_null": 0.01},
                "price_evidence": {"type": "text", "value": "Pro €20/month", "valid": True, "p_null": 0.01},
            }
        }
        res = normalize_clairic_pricing_result(good_raw, locator=locator, snapshot_month="2026-11")
        self.assertEqual(res.pricing_model, "freemium")
        self.assertEqual(res.pricing_currency_code, "EUR")
        self.assertEqual(res.starting_price_minor, 2000)
        self.assertEqual(res.starting_price_usd_minor, 2160)
        self.assertEqual(res.pricing_interval, "monthly")
        self.assertEqual(res.has_free_plan, 1)
        self.assertIn("20/mo", res.pricing_notes or "")

        uncertain_raw = {
            "answers": {
                "pricing_model": {"type": "enum", "value": "paid", "valid": True, "p_null": 0.1},
                "has_free_plan": {"type": "boolean", "value": False, "valid": True, "p_null": 0.1},
                "has_free_trial": {"type": "boolean", "value": False, "valid": True, "p_null": 0.1},
                "starting_price_amount": {"type": "number", "value": 99, "valid": True, "p_null": 0.72},
                "pricing_currency": {"type": "text", "value": "$", "valid": True, "p_null": 0.70},
                "billing_interval": {"type": "enum", "value": "month", "valid": True, "p_null": 0.6},
            }
        }
        uncertain = normalize_clairic_pricing_result(uncertain_raw, locator=locator, snapshot_month="2026-11")
        self.assertIsNone(uncertain.starting_price_minor)
        self.assertIsNone(uncertain.starting_price_usd_minor)

    async def test_dry_run_makes_no_database_writes(self):
        fake_pg = MagicMock()
        fake_pg.__aenter__ = AsyncMock(return_value=fake_pg)
        fake_pg.__aexit__ = AsyncMock(return_value=False)
        fake_pg.query = AsyncMock(
            return_value=[
                {
                    "tool_id": 11,
                    "canonical_slug": "acme-ai",
                    "normalized_domain": "acme.ai",
                    "official_url": "https://acme.ai/",
                    "seo_index_status": "indexable",
                    "known_pricing_url": "https://acme.ai/pricing",
                }
            ]
        )
        fake_pg.batch = AsyncMock()
        located = LocatedPricingPage(
            url="https://acme.ai/pricing",
            final_url="https://acme.ai/pricing",
            status="found",
            method="known_source",
            quality_score=20,
            text="Pro $19/month",
        )
        raw_resp = {
            "answers": {
                "pricing_model": {"type": "enum", "value": "paid", "valid": True, "p_null": 0.01},
                "has_free_plan": {"type": "boolean", "value": False, "valid": True, "p_null": 0.01},
                "has_free_trial": {"type": "boolean", "value": False, "valid": True, "p_null": 0.01},
                "starting_price_amount": {"type": "number", "value": 19, "valid": True, "p_null": 0.01},
                "pricing_currency": {"type": "text", "value": "$", "valid": True, "p_null": 0.01},
                "billing_interval": {"type": "enum", "value": "month", "valid": True, "p_null": 0.01},
                "price_evidence": {"type": "text", "value": "Pro $19/month", "valid": True, "p_null": 0.01},
            }
        }
        config = SimpleNamespace(cloudflare_account_id="acct", cloudflare_api_token="tok")
        with patch("workloads.enrichment.clairic_pricing.PostgresClient", return_value=fake_pg), \
             patch("workloads.enrichment.clairic_pricing.locate_pricing_page", AsyncMock(return_value=located)), \
             patch("workloads.enrichment.clairic_pricing.call_clairic_pricing", AsyncMock(return_value=raw_resp)):
            out = await run_static_pricing_extraction(
                config,
                limit=5,
                dry_run=True,
                database_url="postgresql://readonly@127.0.0.1:5432/sigpik",
            )

        fake_pg.batch.assert_not_called()
        self.assertEqual(out["extracted"], 1)
        self.assertEqual(out["updated"], 0)
        self.assertEqual(out["samples"][0]["starting_price_usd_minor"], 1900)


if __name__ == "__main__":
    unittest.main()
