import copy
import unittest

import runner
from pricing.auto_approval import (
    evaluate_strict_auto_approval,
    pricing_payloads_agree,
)


def simple_payload(display_text: str = "Pro USD 29 per month") -> dict:
    return {
        "plans": [
            {
                "source_plan_key": "pro",
                "name": "Pro",
                "prices": [
                    {
                        "kind": "recurring",
                        "amount": "29",
                        "currency": "USD",
                        "billing_interval": "monthly",
                        "commitment_interval": None,
                        "unit": None,
                        "custom_quote": False,
                        "starting_at": False,
                        "display_text": display_text,
                    }
                ],
            }
        ],
        "quality": {"text_score": 30},
        "extraction_method": "python_rule",
    }


def decision(payload: dict, **overrides):
    values = {
        "review_status": "approved",
        "confidence": 82,
        "validation_errors": [],
        "http_status": 200,
        "page_status": "found",
        "strict_source_context": True,
        "model_used": False,
        "rule_model_agreement": False,
        "min_confidence": 82,
    }
    values.update(overrides)
    return evaluate_strict_auto_approval(payload, **values)


class StrictPricingAutoApprovalTests(unittest.TestCase):
    def test_simple_explicit_iso_monthly_price_is_eligible(self) -> None:
        result = decision(simple_payload())

        self.assertTrue(result.eligible)
        self.assertEqual(result.reasons, ())

    def test_bare_dollar_price_stays_in_review(self) -> None:
        result = decision(simple_payload("Pro $29 per month"))

        self.assertFalse(result.eligible)
        self.assertIn("plan_0_currency_not_explicit", result.reasons)

    def test_complex_commitment_and_unit_price_stays_in_review(self) -> None:
        payload = simple_payload("Pro USD 29 per user per month, billed annually")
        price = payload["plans"][0]["prices"][0]
        price["unit"] = "user"
        price["commitment_interval"] = "yearly"

        result = decision(payload)

        self.assertFalse(result.eligible)
        self.assertIn("plan_0_unit_pricing", result.reasons)
        self.assertIn("plan_0_commitment_pricing", result.reasons)
        self.assertIn("plan_0_high_risk_billing_language", result.reasons)

    def test_fixed_ai_allowance_does_not_block_package_price(self) -> None:
        result = decision(
            simple_payload("Pro USD 29 per month includes 10,000 credits per month")
        )

        self.assertTrue(result.eligible)

    def test_money_charged_per_usage_unit_stays_in_review(self) -> None:
        result = decision(
            simple_payload(
                "Pro USD 29 per month; additional credits cost USD 5 per 1,000 credits"
            )
        )

        self.assertFalse(result.eligible)
        self.assertIn("plan_0_metered_usage_charge", result.reasons)

    def test_metered_usage_feature_stays_in_review(self) -> None:
        payload = simple_payload()
        payload["plans"][0]["features"] = [
            "Additional credits cost USD 5 per 1,000 credits"
        ]

        result = decision(payload)

        self.assertFalse(result.eligible)
        self.assertIn("plan_0_metered_usage_feature", result.reasons)

    def test_model_features_require_evidence_checkpoint(self) -> None:
        payload = simple_payload()
        payload["plans"][0]["features"] = ["10,000 credits per month"]

        rejected = decision(payload, model_used=True, rule_model_agreement=True)
        payload["quality"]["feature_evidence"] = {
            "verified": True,
            "total": 1,
            "kept": 1,
            "dropped": 0,
        }
        accepted = decision(payload, model_used=True, rule_model_agreement=True)

        self.assertFalse(rejected.eligible)
        self.assertIn("plan_0_feature_evidence_unverified", rejected.reasons)
        self.assertTrue(accepted.eligible)

    def test_ai_allowance_requests_feature_extraction_without_becoming_usage_price(self) -> None:
        payload = simple_payload("Pro USD 29 per month with 1 million tokens per month")

        needs_model, reasons = runner.should_verify_rule_pricing_with_openai(
            payload,
            text_score=30,
            page_status="found",
        )

        self.assertTrue(needs_model)
        self.assertIn("fixed_ai_allowance_present", reasons)

    def test_model_output_requires_independent_rule_agreement(self) -> None:
        rejected = decision(simple_payload(), model_used=True, rule_model_agreement=False)
        accepted = decision(simple_payload(), model_used=True, rule_model_agreement=True)

        self.assertFalse(rejected.eligible)
        self.assertIn("rule_model_price_facts_disagree", rejected.reasons)
        self.assertTrue(accepted.eligible)

    def test_price_fact_agreement_ignores_marketing_copy_but_not_amount(self) -> None:
        rule_payload = simple_payload()
        model_payload = copy.deepcopy(rule_payload)
        model_payload["plans"][0]["name"] = "Professional"
        model_payload["plans"][0]["description"] = "For growing teams"

        self.assertTrue(pricing_payloads_agree(rule_payload, model_payload))

        model_payload["plans"][0]["prices"][0]["amount"] = "39"
        self.assertFalse(pricing_payloads_agree(rule_payload, model_payload))

    def test_free_plan_can_be_verified_without_inventing_currency_context(self) -> None:
        payload = simple_payload("Free plan")
        payload["plans"][0]["name"] = "Free"
        payload["plans"][0]["prices"][0]["amount"] = "0"

        self.assertTrue(decision(payload).eligible)


class RuleModelAgreementTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_override_disagreeing_rule_price(self) -> None:
        rule_payload = simple_payload()
        model_payload = copy.deepcopy(rule_payload)
        model_payload["plans"][0]["prices"][0]["amount"] = "39"

        class Extractor:
            model = "pricing-test-model"

            async def extract(self, *_args):
                return model_payload, "approved", 90, []

        task = runner.PricingTask(
            task_id=1,
            pricing_source_id=1,
            tool_id=1,
            canonical_slug="agreement-test",
            source_url="https://agreement-test.example/pricing",
            official_url="https://agreement-test.example",
            attempts=1,
            max_attempts=3,
            generation=1,
            lease_token="test",
        )
        result = runner.PricingFetchResult(
            url=task.source_url,
            final_url=task.source_url,
            status=200,
            content_type="text/html",
            html="<section>Pro USD 29 per month</section>",
        )

        payload, status, confidence, errors, model = await runner.run_openai_pricing_extraction(
            task,
            result,
            rule_payload,
            "approved",
            82,
            [],
            [Extractor()],
            True,
            ["generic_plan_name"],
        )

        self.assertEqual(status, "manual_review")
        self.assertLessEqual(confidence, 65)
        self.assertIn("Rule and model price facts disagree", errors)
        self.assertFalse(payload["verification"]["rule_model_agreement"])
        self.assertEqual(model, "pricing-test-model")


class StrictAutoPublishIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, enabled: bool):
        source_url = "https://strict-auto.example/pricing"
        html = (
            "<section id='pricing'><h1>Pricing plans</h1>"
            "<p>Pricing plans monthly. Pricing plans monthly. Pricing plans monthly. "
            "Pricing plans monthly. Pricing plans monthly.</p>"
            "<h2>Pro</h2><p>Pro USD 29 per month includes 10,000 credits per month</p></section>"
        )
        result = runner.PricingFetchResult(
            url=source_url,
            final_url=source_url,
            status=200,
            content_type="text/html",
            html=html,
        )

        class Client:
            async def choose_pricing_page(self, _task):
                return result

        class Store:
            def __init__(self):
                self.review_status = None
                self.saved = False
                self.finished_status = None

            async def renew_lease(self, _task):
                return True

            async def insert_snapshot(self, _task, _result, _bundle=None):
                return 1

            async def insert_extraction(
                self,
                _snapshot_id,
                _payload,
                review_status,
                _confidence,
                _validation_errors,
                **_kwargs,
            ):
                self.review_status = review_status
                return 1

            async def save_catalog(self, _task, _result, _plans):
                self.saved = True
                return 1

            async def update_summary(self, _task, _plans):
                return None

            async def finish_task(self, _task, status, _error, _result):
                self.finished_status = status
                return True

        task = runner.PricingTask(
            task_id=1,
            pricing_source_id=1,
            tool_id=1,
            canonical_slug="strict-auto",
            source_url=source_url,
            official_url="https://strict-auto.example",
            attempts=1,
            max_attempts=3,
            generation=1,
            lease_token="test",
        )
        store = Store()
        status = await runner.process_pricing_task(
            task,
            Client(),
            [],
            None,
            store,
            0,
            approve_pricing=False,
            dry_run=False,
            strict_auto_publish_enabled=enabled,
            strict_auto_publish_min_confidence=82,
        )
        return status, store

    async def test_default_off_policy_never_auto_publishes(self) -> None:
        status, store = await self._run(False)

        self.assertEqual(status, "manual_review")
        self.assertEqual(store.review_status, "manual_review")
        self.assertFalse(store.saved)
        self.assertEqual(store.finished_status, "manual_review")

    async def test_opt_in_policy_publishes_only_strictly_eligible_price(self) -> None:
        status, store = await self._run(True)

        self.assertEqual(status, "succeeded")
        self.assertEqual(store.review_status, "approved")
        self.assertTrue(store.saved)
        self.assertEqual(store.finished_status, "succeeded")


if __name__ == "__main__":
    unittest.main()
