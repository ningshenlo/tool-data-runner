import unittest

import runner
from pricing.allowances import (
    extract_fixed_allowance_quotes,
    merge_evidenced_rule_features,
    normalize_plan_feature,
    retain_evidenced_plan_features,
)


class PricingAllowanceNormalizationTests(unittest.TestCase):
    def test_numeric_credit_allowance_is_normalized(self) -> None:
        feature = normalize_plan_feature("Includes 10,000 credits per month")

        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertEqual(feature["feature_group"], "allowance")
        self.assertEqual(feature["normalized_key"], "allowance.credit")
        self.assertEqual(feature["state"], "limited")
        self.assertEqual(feature["value_number"], "10000")
        self.assertEqual(feature["unit"], "credit")
        self.assertEqual(feature["period"], "month")

    def test_scaled_token_and_unlimited_generation_allowances_are_normalized(self) -> None:
        tokens = normalize_plan_feature("1 million tokens / month")
        generations = normalize_plan_feature("Unlimited generations")

        assert tokens is not None
        assert generations is not None
        self.assertEqual(tokens["value_number"], "1000000")
        self.assertEqual(tokens["unit"], "token")
        self.assertEqual(tokens["period"], "month")
        self.assertEqual(generations["state"], "included")
        self.assertEqual(generations["value_text"], "unlimited")
        self.assertEqual(generations["unit"], "generation")

    def test_usage_charge_is_not_normalized_as_bundled_allowance(self) -> None:
        feature = normalize_plan_feature("Additional credits cost USD 5 per 1,000 credits")

        assert feature is not None
        self.assertEqual(feature["feature_group"], "usage_charge")
        self.assertIsNone(feature["value_number"])

    def test_price_local_extraction_keeps_allowance_but_not_metered_rate(self) -> None:
        fixed = extract_fixed_allowance_quotes(
            "Includes 20k credits per month and unlimited images"
        )
        metered = extract_fixed_allowance_quotes(
            "Additional credits cost USD 5 per 1,000 credits"
        )

        self.assertEqual(fixed, ["Includes 20k credits per month", "unlimited images"])
        self.assertEqual(metered, [])

    def test_model_features_require_literal_nearest_plan_evidence(self) -> None:
        plans = [
            {"name": "Free", "features": ["100 credits per month", "Made up feature"]},
            {"name": "Pro", "features": ["10,000 credits per month", "100 credits per month"]},
        ]
        source = (
            "Free USD 0 per month 100 credits per month "
            "Pro USD 29 per month 10,000 credits per month"
        )

        evidence = retain_evidenced_plan_features(plans, source)

        self.assertEqual(plans[0]["features"], ["100 credits per month"])
        self.assertEqual(plans[1]["features"], ["10,000 credits per month"])
        self.assertEqual(evidence, {"verified": True, "total": 4, "kept": 2, "dropped": 2})

    def test_rule_extractor_attaches_fixed_allowance_to_plan(self) -> None:
        html = (
            "<section><h1>Pricing plans</h1><h2>Pro</h2>"
            "<p>Pro USD 29 per month</p><p>Includes 10,000 credits per month</p>"
            "</section>"
        )

        payload, status, _confidence, errors = runner.extract_pricing_payload(
            html,
            "https://allowance.example/pricing",
            "https://allowance.example/pricing",
            200,
            "",
        )

        self.assertEqual(status, "approved")
        self.assertEqual(errors, [])
        self.assertEqual(payload["plans"][0]["features"], ["Includes 10,000 credits per month"])

    def test_rule_allowance_survives_agreeing_model_result(self) -> None:
        rule_payload = {
            "plans": [
                {
                    "name": "Pro",
                    "prices": [
                        {
                            "kind": "recurring",
                            "amount": "29",
                            "currency": "USD",
                            "billing_interval": "monthly",
                        }
                    ],
                    "features": ["Includes 10,000 credits per month"],
                }
            ]
        }
        model_payload = {
            "plans": [
                {
                    "name": "Professional",
                    "prices": [
                        {
                            "kind": "recurring",
                            "amount": "29",
                            "currency": "USD",
                            "billing_interval": "monthly",
                        }
                    ],
                    "features": [],
                }
            ],
            "quality": {
                "feature_evidence": {"verified": True, "total": 0, "kept": 0, "dropped": 0}
            },
        }

        merged = merge_evidenced_rule_features(rule_payload, model_payload)

        self.assertEqual(merged, 1)
        self.assertEqual(
            model_payload["plans"][0]["features"],
            ["Includes 10,000 credits per month"],
        )
        self.assertEqual(model_payload["quality"]["feature_evidence"]["rule_merged"], 1)


if __name__ == "__main__":
    unittest.main()
