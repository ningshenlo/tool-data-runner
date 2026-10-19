import unittest

from taxonomy_precision import primary_acceptance, catalog_fingerprint
from taxonomy_shadow import (TaxonomyCatalog, TaxonomyTerm, build_product_profile,
    profile_classification_context, parse_leaf_decision, decide_primary_status)


class PrimaryPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.root = TaxonomyTerm(28, 'primary_category', 'ai-security-compliance', 'Security')
        self.leaf = TaxonomyTerm(304, 'primary_category', 'content-detection-authenticity', 'Detection', parent_id=28)
        self.hits = [{'term': self.root, 'confidence': 0.97}]
        self.catalog = TaxonomyCatalog([self.root, self.leaf])

    def profile(self, text, negative=None):
        return {
            'entity_decision': {'accepted': True, 'kind': 'independent_product'},
            'primary_job': {'value': text, 'evidence': [{'quote': text}]},
            'negative_claims': [{'value': negative, 'evidence': [{'quote': negative}]}] if negative else [],
        }

    def evaluate(self, text, negative=None, confidence=0.95):
        return primary_acceptance({'term': self.leaf, 'confidence': confidence,
            'evidence': [{'quote': text}]}, self.profile(text, negative), self.hits)

    def test_detection_positive_controls(self):
        for text in ['Detect AI-generated text and deepfakes.', 'An AI content detector for publishers.',
                     'Plagiarism checker for original writing.', '识别深度伪造和AI生成内容',
                     'GLTR represents a visually forensic tool to detect text that was automatically generated from large language models.']:
            with self.subTest(text=text):
                self.assertTrue(self.evaluate(text)['eligible'])

    def test_source_attribution_and_rewriting_are_not_accepted_as_detection(self):
        for text in ['Trace each answer back to its exact source sentence.',
                     'Reveal hidden artifacts. Then rewrite in your own voice.',
                     'Detect AI-generated writing and humanize it to bypass detectors.']:
            with self.subTest(text=text):
                result = self.evaluate(text)
                self.assertFalse(result['eligible'])
                self.assertEqual(result['status'], 'provisional')

    def test_explicit_denial_blocks_even_positive_detection_phrase(self):
        result = self.evaluate('Detect AI-generated text.', 'This is not an AI detector or an authorship verdict.')
        self.assertIn('explicit_detection_denial', result['reasons'])

    def test_detection_denial_in_main_workflow_also_blocks(self):
        result = self.evaluate('We do not detect AI-generated text; we trace citations.')
        self.assertIn('explicit_detection_denial', result['reasons'])
        self.assertFalse(result['eligible'])

    def test_missing_leaf_evidence_cannot_be_accepted(self):
        result = primary_acceptance({'term': self.leaf, 'confidence': 0.99, 'evidence': []},
                                    self.profile('Detect AI-generated text.'), self.hits)
        self.assertIn('missing_grounded_leaf_evidence', result['reasons'])
        self.assertFalse(result['eligible'])

    def test_validated_half_confidence_is_accepted_with_grounded_evidence(self):
        for confidence, expected in [(0.49, False), (0.50, True), (0.74, True), (0.75, True)]:
            with self.subTest(confidence=confidence):
                result = primary_acceptance({'term': self.leaf, 'confidence': confidence,
                    'evidence': [{'quote': 'AI text detector'}]}, self.profile('AI text detector'),
                    [{'term': self.root, 'confidence': 0.5}])
                self.assertEqual(result['eligible'], expected)
                self.assertEqual(decide_primary_status(confidence), 'auto_accepted' if expected else 'provisional')

    def test_half_confidence_does_not_bypass_missing_or_contradictory_evidence(self):
        result = primary_acceptance({'term': self.leaf, 'confidence': 0.5, 'evidence': []},
                                    self.profile('AI text detector'), self.hits)
        self.assertIn('missing_grounded_leaf_evidence', result['reasons'])
        result = self.evaluate('Trace answers to source sentences.', confidence=0.5)
        self.assertIn('detection_main_workflow_not_established', result['reasons'])

    def test_invalid_or_missing_confidence_fails_closed(self):
        for value in [None, float('nan'), float('inf'), -1, 2, True, 'broken']:
            with self.subTest(value=value):
                decision = parse_leaf_decision({'leaf_slug': self.leaf.slug, 'confidence': value},
                                               [self.leaf], self.catalog)
                self.assertEqual(decision['confidence'], 0)
                self.assertEqual(decide_primary_status(value), 'unresolved')

    def test_entity_and_parent_ambiguity_are_rechecked(self):
        decision = {'term': self.leaf, 'confidence': 0.99, 'evidence': [{'quote': 'AI text detector'}]}
        profile = self.profile('AI text detector')
        profile['entity_decision']['kind'] = 'company_site'
        other = TaxonomyTerm(5, 'primary_category', 'writing', 'Writing')
        result = primary_acceptance(decision, profile, [{'term': self.root, 'confidence': 0.93}, {'term': other, 'confidence': 0.95}])
        self.assertIn('entity_not_eligible', result['reasons'])
        self.assertIn('unresolved_market_ambiguity', result['reasons'])
        result = primary_acceptance(decision, self.profile('AI text detector'), [{'term': self.root, 'confidence': 0.49}])
        self.assertIn('unsupported_parent_market', result['reasons'])

    def test_profile_keeps_grounded_negation_and_drops_invented_negation(self):
        source = 'Rewrite toward your voice. This is not an authorship detector.'
        raw = {'main_sold_workflow': {'value': 'Rewrite', 'evidence': [{'quote': 'Rewrite toward your voice.'}]},
               'negative_claims': [{'value': 'Not a detector', 'evidence': [{'quote': 'This is not an authorship detector.'}]},
                                   {'value': 'No safety', 'evidence': [{'quote': 'We do not provide security.'}]}]}
        profile = build_product_profile(raw, source_url='https://test.invalid', source_text=source)
        self.assertEqual(len(profile['negative_claims']), 1)
        context = profile_classification_context(profile)
        self.assertIn('not an authorship detector', context)
        self.assertIn('main_sold_workflow', context)
        self.assertNotIn('No safety', context)

    def test_openai_profile_schema_matches_batch_strict_contract_without_mutating_source(self):
        import copy
        from runner import build_browser_response_format
        from taxonomy_shadow import profile_extract_schema
        from taxonomy_schema import strict_json_schema
        schema = profile_extract_schema()
        original = copy.deepcopy(schema)
        for value in (schema, {"name": "profile", "strict": True, "schema": schema}):
            result = build_browser_response_format(value, model="openai/test", stage="profile")
            strict = result["json_schema"]["schema"]
            self.assertEqual(strict, strict_json_schema(schema))
            evidence = strict["properties"]["entity_evidence"]["items"]
            self.assertEqual(set(evidence["required"]), set(evidence["properties"]))
            self.assertFalse(evidence["additionalProperties"])
            self.assertEqual(schema, original)

    def test_detection_boundary_is_not_imposed_on_unrelated_leaf_pools(self):
        from taxonomy_shadow import leaf_adjudication_prompt
        writing = TaxonomyTerm(5, 'primary_category', 'writing-text', 'Writing')
        editing = TaxonomyTerm(6, 'primary_category', 'text-editing', 'Editing', parent_id=5)
        catalog = TaxonomyCatalog([self.root, self.leaf, writing, editing])
        prompt = leaf_adjudication_prompt([editing], [writing.slug], catalog, {})
        self.assertNotIn('CATEGORY-SPECIFIC RULE', prompt)
        self.assertIn('value fields are summaries', prompt)
        prompt = leaf_adjudication_prompt([self.leaf, editing], [self.root.slug, writing.slug], catalog, {})
        self.assertIn('never to other markets', prompt)
        result = primary_acceptance({'term': editing, 'confidence': 0.95, 'evidence': [{'quote': 'Rewrite text'}]},
            self.profile('Rewrite text'), [{'term': writing, 'confidence': 0.99}])
        self.assertTrue(result['eligible'])

    def test_entity_and_l1_invalid_confidence_cannot_become_positive(self):
        from taxonomy_shadow import parse_entity_decision, parse_top2_l1
        for confidence in [None, float('nan'), float('inf'), True, 95]:
            entity = parse_entity_decision({'entity_kind': 'independent_product',
                'entity_confidence': confidence, 'entity_evidence': [{'quote': 'A product'}]}, source_text='A product')
            self.assertFalse(entity['accepted'])
            hits = parse_top2_l1({'l1_candidates': [{'slug': self.root.slug, 'confidence': confidence}]}, self.catalog)
            self.assertEqual(hits[0]['confidence'], 0)

    def test_full_excludes_and_catalog_fingerprint(self):
        long_excludes = 'Generic exclusion. ' * 20 + 'Never treat citation attribution as detection.'
        leaf = TaxonomyTerm(304, 'primary_category', self.leaf.slug, 'Detection', parent_id=28, excludes=long_excludes)
        changed = TaxonomyCatalog([self.root, leaf])
        self.assertIn(long_excludes, changed.render_terms([leaf]))
        self.assertNotEqual(catalog_fingerprint(self.catalog), catalog_fingerprint(changed))



class DirectPrimaryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_direct_results_do_not_write_or_supersede_assignments(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        import taxonomy_shadow as shadow
        root = TaxonomyTerm(1, 'primary_category', 'video', 'Video')
        leaf = TaxonomyTerm(2, 'primary_category', 'text-to-video', 'Video generation', parent_id=1)
        catalog = TaxonomyCatalog([root, leaf])
        text = 'Generate videos from text'
        task = SimpleNamespace(tool_id=123, official_url='https://product.test/')
        browser = SimpleNamespace(
            taxonomy_custom_ai=lambda: [{'model': 'fake-model'}],
            fetch_homepage_content=AsyncMock(return_value=(task.official_url,
                '<html><body><h1>Generate videos from text</h1><p>Start for free</p></body></html>')),
        )
        profile = {'entity_kind': 'independent_product', 'entity_confidence': 0.95,
            'entity_evidence': [{'quote': 'Start for free'}],
            'primary_job': {'value': text, 'evidence': [{'quote': text}]}}
        l1 = {'l1_candidates': [{'slug': root.slug, 'confidence': 0.95}]}
        for confidence, quotes, reason in [
            (0.49, [{'quote': text}], 'low_primary_confidence'),
            (0.99, [], 'missing_grounded_leaf_evidence'),
            (0.99, [{'quote': 'Invented claim absent from source'}], 'missing_grounded_leaf_evidence'),
        ]:
            with self.subTest(confidence=confidence, quotes=quotes):
                leaf_raw = {'leaf_slug': leaf.slug, 'confidence': confidence, 'evidence': quotes}
                responses = [(task.official_url, value) for value in (profile, l1, leaf_raw)]
                with patch.object(shadow, 'fetch_cleaned_text_structured', AsyncMock(side_effect=responses)), \
                     patch.object(shadow, 'upsert_product_profile', AsyncMock()), \
                     patch.object(shadow, 'insert_classification_run', AsyncMock(return_value=9)) as save_run, \
                     patch.object(shadow, 'upsert_assignment', AsyncMock()) as write, \
                     patch.object(shadow, 'supersede_auto_assignments', AsyncMock()) as supersede:
                    result = await shadow.classify_tool_shadow(d1=object(), browser_client=browser,
                        task=task, catalog=catalog, dry_run=False, include_capabilities=False,
                        existing_entity_kind='independent_product', existing_entity_source='manual')
                self.assertEqual(result.status, 'partial')
                self.assertIn(reason, result.error)
                self.assertEqual(result.primary_slug, '')
                self.assertEqual(save_run.await_args.kwargs['run_status'], 'partial')
                write.assert_not_awaited()
                supersede.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
