import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from taxonomy_batch import try_recover_source_with_upstream_evidence
from taxonomy_precision import primary_acceptance
from taxonomy_shadow import TaxonomyTerm


class PrecisionSafetyTests(unittest.TestCase):
    def test_keyword_negation_and_wrong_parent_never_bypass_ambiguity(self):
        video = TaxonomyTerm(1, 'primary_category', 'video-animation', 'Video')
        image = TaxonomyTerm(3, 'primary_category', 'image-generation', 'Image')
        hits = [{'term': video, 'confidence': .98}, {'term': image, 'confidence': .95}]
        for text, parent in [('Not a video or animation tool; create still pictures.', 1),
                             ('Generate videos and clips.', 3)]:
            with self.subTest(text=text):
                profile = {'entity_decision': {'accepted': True, 'kind': 'independent_product'},
                           'primary_job': {'evidence': [{'quote': text}]}}
                decision = {'term': TaxonomyTerm(8, 'primary_category', 'leaf', 'Leaf', parent_id=parent),
                            'confidence': .89, 'evidence': [{'quote': text}]}
                result = primary_acceptance(decision, profile, hits)
                self.assertFalse(result['eligible'])
                self.assertIn('unresolved_market_ambiguity', result['reasons'])

    def test_old_and_new_fallback_profiles_cannot_auto_accept(self):
        root = TaxonomyTerm(1, 'primary_category', 'video-animation', 'Video')
        leaf = TaxonomyTerm(2, 'primary_category', 'video-generation', 'Video', parent_id=1)
        for marker in [{'evidence_tier': 'upstream_metadata'},
                       {'extractor_version': 'old-upstream-fallback'}]:
            profile = {**marker, 'entity_decision': {'accepted': True, 'kind': 'independent_product'},
                       'primary_job': {'evidence': [{'quote': 'Generate video'}]}}
            result = primary_acceptance({'term': leaf, 'confidence': .99, 'evidence': [{'quote': 'Generate video'}]},
                                        profile, [{'term': root, 'confidence': .99}])
            self.assertFalse(result['eligible'])
            self.assertIn('upstream_metadata_requires_review', result['reasons'])


class RecoverySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def recover(self, row):
        db = SimpleNamespace(query=AsyncMock(return_value=[row]), run=AsyncMock())
        with patch('taxonomy_batch.upsert_product_profile', new_callable=AsyncMock) as save, \
             patch('taxonomy_batch.enqueue_stage', new_callable=AsyncMock):
            result = await try_recover_source_with_upstream_evidence(db, {'id': 9, 'tool_id': 1}, None, None, 'failed')
            return result, save, db

    async def test_descriptions_and_untraceable_claims_do_not_become_evidence(self):
        for payload in [None, {'qualification': {'catalog_fit': {'decision': {
                'verifiedStrongEvidence': [{'quote': 'Generate videos'}]}}}}]:
            result, save, db = await self.recover({'loc_desc': 'Rejected or generated description',
                'cand_desc': 'AI platform', 'cand_payload': payload})
            self.assertFalse(result)
            save.assert_not_awaited()
            db.run.assert_not_awaited()

    async def test_traceable_claims_keep_original_url_and_require_review(self):
        payload = {'qualification': {'catalog_fit': {'decision': {
            'outcome': 'rejected', 'verifiedStrongEvidence': [
                {'quote': 'Generate videos', 'pageUrl': 'https://example.com/features'}]}}}}
        for raw in [payload, json.dumps(payload)]:
            result, save, _ = await self.recover({'official_url': 'https://example.com/', 'cand_payload': raw})
            self.assertTrue(result)
            profile = save.await_args.args[2]
            self.assertFalse(profile['entity_decision']['accepted'])
            self.assertEqual(profile['entity_decision']['kind'], 'unresolved')
            self.assertIsNone(profile['main_sold_workflow'])
            self.assertEqual(profile['primary_job']['evidence'][0]['source_url'], 'https://example.com/features')
