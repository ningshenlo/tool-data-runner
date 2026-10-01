import unittest

import json

from taxonomy_incident_requeue_dry_run import build_report, classify_row, repair_scopes


def row(**overrides):
    base = {
        "tool_id": 1,
        "canonical_slug": "tool",
        "normalized_domain": "tool.ai",
        "official_url": "https://tool.ai/",
        "taxonomy_evidence_url": "https://tool.ai/",
        "tool_status": "published",
        "entity_kind": "unresolved",
        "entity_kind_source": "auto",
        "duplicate_of_tool_id": None,
        "has_manual_entity_decision": 0,
        "anomaly_candidate_id": 11,
        "anomaly_candidate_status": "pending",
        "anomaly_evidence_json": json.dumps(
            {
                "matches": [
                    {
                        "source": "localization",
                        "code": "neutral_transport_example_domain",
                        "matched_codes": ["neutral_transport_example_domain"],
                    }
                ]
            }
        ),
        "active_request_id": None,
        "latest_request_id": None,
    }
    base.update(overrides)
    return base


class CohortSafetyTests(unittest.TestCase):
    def test_clean_reset_member_is_eligible(self):
        self.assertEqual(classify_row(row()), "eligible")

    def test_only_an_active_request_is_excluded(self):
        self.assertEqual(
            classify_row(row(active_request_id=9)), "active_reprocess_request"
        )
        self.assertEqual(classify_row(row(latest_request_id=8)), "eligible")

    def test_manual_and_example_domain_are_excluded(self):
        self.assertEqual(classify_row(row(entity_kind_source="manual")), "manual_protected")
        self.assertEqual(
            classify_row(row(taxonomy_evidence_url="https://example.com/")),
            "neutral_transport_url",
        )

    def test_inactive_or_different_pollution_is_excluded(self):
        self.assertEqual(
            classify_row(row(anomaly_candidate_status="resolved")),
            "no_active_pollution_candidate",
        )
        self.assertEqual(
            classify_row(row(anomaly_evidence_json='{"matches":[]}')),
            "different_pollution_signature",
        )

    def test_repair_scopes_are_derived_from_evidence_sources(self):
        item = row(
            anomaly_evidence_json=json.dumps(
                {
                    "matches": [
                        {"source": "features", "code": "neutral_transport_example_domain"},
                        {"source": "classification_run", "code": "neutral_transport_example_domain"},
                    ]
                }
            )
        )
        self.assertEqual(repair_scopes(item), ["classification", "features"])

    def test_report_has_stable_sorted_manifest(self):
        report = build_report(
            [row(tool_id=2), row(tool_id=1), row(tool_id=3, active_request_id=7)],
            "INC-TEST",
        )
        self.assertEqual(report["summary"]["eligible_for_repair"], 2)
        self.assertEqual(report["summary"]["eligible_needing_content_repair"], 2)
        self.assertEqual(report["summary"]["model_calls"], 0)
        self.assertEqual([item["tool_id"] for item in report["records"]], [1, 2, 3])
        self.assertEqual(len(report["eligible_manifest_fingerprint"]), 64)

    def test_report_includes_projected_public_category_impact(self):
        report = build_report(
            [row()],
            "INC-TEST",
            {
                "current_visible_tools": 251,
                "projected_visible_tools": 96,
                "quarantined_from_category": 155,
            },
        )
        self.assertEqual(report["public_category_impact"]["current_visible_tools"], 251)
        self.assertEqual(report["public_category_impact"]["projected_visible_tools"], 96)


if __name__ == "__main__":
    unittest.main()
