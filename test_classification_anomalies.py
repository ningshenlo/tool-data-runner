import unittest

from classification_anomalies import build_anti_bot_anomaly_candidate


class ClassificationAnomalyScoringTests(unittest.TestCase):
    def test_janitorai_shape_is_high_priority(self):
        candidate = build_anti_bot_anomaly_candidate(
            {
                "tool_id": 318,
                "localization_text": "Access has been blocked by the firewall. The company is improving security measures.",
                "feature_text": "Security Measures Legitimate Access Malicious Traffic Blocking",
                "profile_text": '{"entity_decision":{"kind":"unresolved","reason":"access restricted page"}}',
                "latest_run_id": 262,
                "latest_run_status": "partial",
                "latest_run_error": "entity_unresolved",
                "latest_run_text": '{"error":"entity_unresolved","entity_reason":"Access Restricted"}',
                "assignment_decision_status": "legacy",
                "assignment_source": "legacy",
                "category_classification_raw": None,
                "current_primary_term_id": 28,
                "current_primary_slug": "ai-security-compliance",
                "source_text": '{"page_metadata":{"title":"janitor - Build, share, and explore","description":"A platform for creators building immersive worlds and readers seeking living stories."}}',
            }
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["severity"], "high")
        self.assertEqual(candidate["score"], 100)
        signal_codes = {item["code"] for item in candidate["evidence"]["signals"]}
        self.assertIn("legacy_category_without_provenance", signal_codes)
        self.assertIn("new_pipeline_unresolved", signal_codes)
        self.assertIn("valid_discovery_metadata_conflicts_with_block_page", signal_codes)
        self.assertIn("security_category_may_reflect_waf_copy", signal_codes)

    def test_real_security_product_is_not_an_antibot_anomaly(self):
        candidate = build_anti_bot_anomaly_candidate(
            {
                "tool_id": 1,
                "localization_text": "AI security and compliance platform for defending models.",
                "feature_text": "Threat detection Governance Policy monitoring",
                "profile_text": "",
                "latest_run_text": "",
                "current_primary_slug": "ai-security-compliance",
            }
        )
        self.assertIsNone(candidate)


if __name__ == "__main__":
    unittest.main()
