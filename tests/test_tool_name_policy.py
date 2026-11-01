"""Streamlined name policy regression tests for AI SaaS competitive intelligence platform."""
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
import runner

class StreamlinedNamePolicyTests(unittest.TestCase):
    def test_waterfall_submitted_name_priority(self):
        result = runner.resolve_tool_name(
            '<title>Other Brand</title><meta property="og:site_name" content="Third Brand">',
            'shopos.ai',
            model_product_name='ShopOS',
        )
        self.assertEqual(result.product_name, 'ShopOS')
        self.assertEqual(result.source, 'submitted_name')
        self.assertEqual(result.review_status, 'auto_approved')

    def test_waterfall_og_site_name(self):
        html = '<meta property="og:site_name" content="Hypotenuse AI"><title>AI Writer | Hypotenuse</title>'
        result = runner.resolve_tool_name(html, 'hypotenuse.ai')
        self.assertEqual(result.product_name, 'Hypotenuse AI')
        self.assertEqual(result.source, 'open_graph_site_name')
        self.assertEqual(result.review_status, 'auto_approved')

    def test_waterfall_json_ld(self):
        html = '<title>Create stunning videos</title><script type="application/ld+json">{"@type":"WebSite","name":"Acme","url":"https://acme.ai/"}</script>'
        result = runner.resolve_tool_name(html, 'acme.ai')
        self.assertEqual(result.product_name, 'Acme')
        self.assertEqual(result.source, 'json_ld_product')
        self.assertEqual(result.review_status, 'auto_approved')

    def test_waterfall_title_segment_brand_match(self):
        html = '<title>AI Music Generator - Song Maker Royalty-Free | MelodyCraft</title>'
        result = runner.resolve_tool_name(html, 'melodycraft.ai')
        self.assertEqual(result.product_name, 'MelodyCraft')
        self.assertEqual(result.source, 'page_title_segment')
        self.assertEqual(result.review_status, 'auto_approved')

    def test_waterfall_domain_fallback_on_empty_or_anti_bot(self):
        html = '<title>Just a moment... Attention Required! | Cloudflare</title>'
        result = runner.resolve_tool_name(html, 'shopos.ai')
        self.assertEqual(result.product_name, 'Shopos')
        self.assertEqual(result.source, 'domain_fallback')
        self.assertEqual(result.review_status, 'auto_approved')

class NamePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def save(self, **kwargs):
        d1 = AsyncMock()
        d1.query.return_value = [{'locale_code': 'en'}]
        task = runner.AssetTask(
            tool_id=1,
            canonical_slug='acme',
            normalized_domain='acme.ai',
            official_url='https://acme.ai/',
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token='test',
        )
        result = runner.AssetFetchResult(final_url=task.official_url, **kwargs)
        await runner.D1AssetStore(d1).save_tool_localization(task, result)
        return d1.run.call_args.args

    async def test_localization_saves_auto_approved(self):
        sql, params = await self.save(
            title='ShopOS',
            description='All-in-one AI surface',
            name_source='discovery_direct',
            name_confidence=95,
            name_review_status='auto_approved',
        )
        self.assertEqual(params[3], 'ShopOS')
        self.assertEqual(params[11], 95)
        self.assertEqual(params[19], 'auto_approved')

    async def test_invalid_title_falls_back_to_clean_domain(self):
        _, params = await self.save(title='Just a moment...', name_confidence=40)
        self.assertEqual(params[3], 'Acme')
        self.assertEqual(params[7], 'domain_fallback')
        self.assertEqual(params[19], 'auto_approved')

if __name__ == '__main__':
    unittest.main()
