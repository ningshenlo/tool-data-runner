import unittest
from types import SimpleNamespace

from classification_anomalies import (
    build_anti_bot_anomaly_candidate,
    reclassification_result_pollution,
)


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
                "assignment_decision_status": "auto_accepted",
                "assignment_source": "auto",
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

    def test_official_source_block_page_is_detected(self):
        candidate = build_anti_bot_anomaly_candidate(
            {
                "tool_id": 2,
                "localization_text": "",
                "feature_text": "",
                "profile_text": "",
                "latest_run_text": "",
                "assignment_decision_status": "auto_accepted",
                "assignment_source": "auto",
                "current_primary_slug": "writing-text",
                "source_text": '{"page_metadata":{"title":"Just a moment...","description":"Checking your browser before accessing the site"}}',
            }
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["score"], 60)
        matches = candidate["evidence"]["matches"]
        self.assertEqual(matches[0]["source"], "official_source")
        self.assertEqual(matches[0]["provider"], "cloudflare")

    def test_neutral_transport_page_pollution_is_high_priority(self):
        candidate = build_anti_bot_anomaly_candidate(
            {
                "tool_id": 35,
                "latest_run_text": (
                    '{"entity_decision":{"kind":"non_product","evidence":['
                    '{"quote":"This domain is for use in documentation examples without needing permission."}]}}'
                ),
                "assignment_decision_status": "auto_accepted",
                "assignment_source": "auto",
                "current_primary_slug": "coding-development",
            }
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["score"], 85)
        self.assertEqual(candidate["severity"], "high")
        self.assertEqual(
            candidate["evidence"]["matches"][0]["code"],
            "neutral_transport_example_domain",
        )

    def test_reclassification_result_gate_rejects_neutral_transport_raw_output(self):
        detected = reclassification_result_pollution(
            SimpleNamespace(
                raw={
                    "profile": {
                        "primary_job": "Generic example domain page for documentation examples"
                    }
                }
            )
        )
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected["code"], "neutral_transport_example_domain")

    def test_reclassification_result_gate_accepts_clean_product_evidence(self):
        detected = reclassification_result_pollution(
            SimpleNamespace(
                raw={
                    "profile": {
                        "primary_job": "Generate product demo videos from scripts"
                    }
                }
            )
        )
        self.assertIsNone(detected)


if __name__ == "__main__":
    unittest.main()
