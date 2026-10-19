"""Name policy regression tests use fixtures and mocks, never a database."""
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
import runner

class NamePolicyTests(unittest.TestCase):
    def test_shared_cases(self):
        fixture = Path(__file__).resolve().parent.parent / 'sigpik/scripts/tests/fixtures/tool-name-quality.json'
        for case in json.loads(fixture.read_text(encoding='utf-8')):
            with self.subTest(case=case['id']):
                result = runner.resolve_tool_name(case['html'], case['hostname'])
                self.assertEqual(result.product_name, case['name'])
                self.assertEqual(result.review_status, case['status'])

    def test_model_cannot_invent_a_name_or_replace_original_title(self):
        result = runner.resolve_tool_name('<title>Acme | Create Videos</title>', 'acme.ai',
            model_product_name='Invented Brand', model_confidence=100, model_page_title='Invented title')
        self.assertEqual(result.product_name, 'Acme')
        self.assertEqual(result.page_title, 'Acme | Create Videos')

    def test_model_only_page_title_is_not_official_evidence(self):
        result = runner.resolve_tool_name('', 'acme.ai', model_page_title='Acme | Create videos',
            model_product_name='Acme', model_confidence=100)
        self.assertEqual(result.review_status, 'needs_review')

class NamePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def save(self, **kwargs):
        d1 = AsyncMock()
        d1.query.return_value = [{'locale_code': 'en'}]
        task = runner.AssetTask(tool_id=1, canonical_slug='acme', normalized_domain='acme.ai',
            official_url='https://acme.ai/', attempts=1, max_attempts=5, generation=1, lease_token='test')
        result = runner.AssetFetchResult(final_url=task.official_url, **kwargs)
        await runner.D1AssetStore(d1).save_tool_localization(task, result)
        return d1.run.call_args.args

    async def test_uncertain_name_does_not_gain_confidence_in_storage(self):
        sql, params = await self.save(title='Acme', name_source='domain_fallback',
            name_confidence=45, name_review_status='needs_review')
        self.assertEqual(params[11], 45)
        self.assertEqual(params[19], 'needs_review')
        self.assertEqual(sql.count("name_review_status <> 'human_approved'"), 5)

    async def test_conflicting_high_score_remains_pending(self):
        _, params = await self.save(title='Acme', name_source='json_ld_product',
            name_confidence=100, name_review_status='needs_review')
        self.assertEqual(params[19], 'needs_review')

    async def test_invalid_title_becomes_unverified_domain_candidate(self):
        _, params = await self.save(title='Acme | Marketing headline', name_confidence=99)
        self.assertEqual(params[3], 'Acme')
        self.assertEqual(params[7], 'domain_fallback')
        self.assertEqual(params[11], 45)
        self.assertEqual(params[19], 'needs_review')

if __name__ == '__main__':
    unittest.main()
