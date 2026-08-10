import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from pricing.claim_states import (
    ClaimState,
    ClaimStateInvariantError,
    assert_claim_invariants,
    valid_claim_states,
)
from pricing.identity import (
    ClaimHistory,
    KnownSubject,
    SubjectCandidate,
    advance_claim_history,
    resolve_subject_identity,
)
from pricing.normalize import normalize_quantity, normalize_raw_claim
from pricing.bundle import build_pricing_snapshot_bundle
from pricing.dom import parse_pricing_dom
from pricing.feature_flags import PricingClaimFeatureFlagError, assert_safe_pricing_claim_flags
from pricing.raw_claims import extract_level1_raw_claims
from pricing.regions import detect_pricing_region
from pricing.snapshot import build_snapshot_artifact, plan_snapshot_capture
from pricing.validate import validate_raw_claim


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "ainav" / "d1" / "migrations"
SKIPPED_DATA_MIGRATIONS = {"0026_reject_catalog_fit_mismatches.sql"}


class ClaimStateInvariantTests(unittest.TestCase):
    def test_published_claim_requires_the_full_verified_active_combination(self) -> None:
        state = ClaimState("normalized", "entailed", "auto_verified", "active", "published")
        assert_claim_invariants(state, claim_type="starting_paid_price")

    def test_failed_normalization_cannot_be_published(self) -> None:
        with self.assertRaisesRegex(ClaimStateInvariantError, "failed normalization"):
            assert_claim_invariants(
                ClaimState("failed", "entailed", "human_verified", "active", "published"),
                claim_type="starting_paid_price",
            )

    def test_conflict_cannot_be_eligible(self) -> None:
        with self.assertRaisesRegex(ClaimStateInvariantError, "conflicting evidence"):
            assert_claim_invariants(
                ClaimState("normalized", "conflict", "unresolved", "active", "eligible"),
                claim_type="usage_rate",
            )

    def test_not_applicable_is_limited_to_presence_claims(self) -> None:
        assert_claim_invariants(
            ClaimState("not_applicable", "entailed", "human_verified", "active", "eligible"),
            claim_type="has_free_plan",
        )
        with self.assertRaisesRegex(ClaimStateInvariantError, "presence claims"):
            assert_claim_invariants(
                ClaimState("not_applicable", "entailed", "human_verified", "active", "eligible"),
                claim_type="usage_rate",
            )

    def test_generated_fixture_contains_only_valid_combinations(self) -> None:
        states = list(valid_claim_states(claim_type="starting_paid_price"))
        self.assertGreater(len(states), 0)
        self.assertLess(len(states), 1920)
        for state in states:
            assert_claim_invariants(state, claim_type="starting_paid_price")

    def test_aging_claim_can_remain_published_but_cannot_become_newly_eligible(self) -> None:
        assert_claim_invariants(
            ClaimState("normalized", "entailed", "human_verified", "aging", "published"),
            claim_type="starting_paid_price",
        )
        with self.assertRaisesRegex(ClaimStateInvariantError, "only active claims"):
            assert_claim_invariants(
                ClaimState("normalized", "entailed", "human_verified", "aging", "eligible"),
                claim_type="starting_paid_price",
            )


class PricingClaimFeatureFlagTests(unittest.TestCase):
    def test_publish_requires_shadow(self) -> None:
        with self.assertRaises(PricingClaimFeatureFlagError):
            assert_safe_pricing_claim_flags(shadow_enabled=False, publish_enabled=True)

    def test_shadow_only_is_safe(self) -> None:
        assert_safe_pricing_claim_flags(shadow_enabled=True, publish_enabled=False)


class PricingClaimsMigrationContractTests(unittest.TestCase):
    def test_additive_schema_accepts_a_pending_shadow_claim(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name in SKIPPED_DATA_MIGRATIONS:
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute("PRAGMA foreign_keys = ON")

            tool_id = connection.execute(
                "INSERT INTO tools (canonical_slug, official_url, normalized_domain) VALUES (?, ?, ?)",
                ("claims-contract", "https://claims-contract.example", "claims-contract.example"),
            ).lastrowid
            source_id = connection.execute(
                "INSERT INTO pricing_sources (tool_id, url) VALUES (?, ?)",
                (tool_id, "https://claims-contract.example/pricing"),
            ).lastrowid
            task_id = connection.execute(
                "INSERT INTO pricing_tasks (pricing_source_id, tool_id) VALUES (?, ?)",
                (source_id, tool_id),
            ).lastrowid
            snapshot_id = connection.execute(
                """
                INSERT INTO pricing_snapshots (
                  pricing_source_id, pricing_task_id, final_url, http_status,
                  raw_hash, semantic_hash
                ) VALUES (?, ?, ?, 200, ?, ?)
                """,
                (
                    source_id,
                    task_id,
                    "https://claims-contract.example/pricing",
                    "a" * 64,
                    "b" * 64,
                ),
            ).lastrowid
            subject_id = connection.execute(
                """
                INSERT INTO pricing_subjects (
                  tool_id, pricing_source_id, subject_type, subject_key,
                  first_seen_snapshot_id, last_seen_snapshot_id
                ) VALUES (?, ?, 'product', 'product:root', ?, ?)
                """,
                (tool_id, source_id, snapshot_id, snapshot_id),
            ).lastrowid
            claim_id = connection.execute(
                """
                INSERT INTO pricing_claims (
                  tool_id, pricing_source_id, snapshot_id, subject_id,
                  subject_key, claim_type, subject_type, raw_value_json,
                  claim_fingerprint, first_seen_snapshot_id,
                  last_seen_snapshot_id, extractor_version
                ) VALUES (?, ?, ?, ?, 'product:root', 'has_free_plan',
                          'product', 'true', ?, ?, ?, 'claims-shadow-v1')
                """,
                (tool_id, source_id, snapshot_id, subject_id, "c" * 64, snapshot_id, snapshot_id),
            ).lastrowid

            row = connection.execute(
                """
                SELECT claim.normalization_status, claim.validation_status,
                       claim.decision_status, claim.lifecycle_status,
                       claim.publication_status, snapshot.requested_locale,
                       snapshot.requested_region, snapshot.geo_mode,
                       claim.normalization_errors_json, claim.validation_result_json
                FROM pricing_claims claim
                JOIN pricing_snapshots snapshot ON snapshot.id = claim.snapshot_id
                WHERE claim.id = ?
                """,
                (claim_id,),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (
                    "pending",
                    "pending",
                    "unreviewed",
                    "active",
                    "not_eligible",
                    "en-US",
                    "US",
                    "default_egress",
                    "[]",
                    "{}",
                ),
            )
        finally:
            connection.close()


class SnapshotArtifactTests(unittest.TestCase):
    def test_content_address_is_stable_across_snapshots(self) -> None:
        first = build_snapshot_artifact(
            "html",
            "<main>Pricing</main>",
            content_type="text/html; charset=utf-8",
            retention_class="changed_snapshot",
        )
        second = build_snapshot_artifact(
            "html",
            b"<main>Pricing</main>",
            content_type="text/html; charset=utf-8",
            retention_class="diagnostic",
        )
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.object_key, second.object_key)

    def test_unchanged_region_reuses_artifacts_and_skips_model(self) -> None:
        html = build_snapshot_artifact(
            "html",
            "<main>Pricing</main>",
            content_type="text/html",
            retention_class="changed_snapshot",
        )
        screenshot = build_snapshot_artifact(
            "screenshot",
            b"png",
            content_type="image/png",
            retention_class="diagnostic",
        )
        plan = plan_snapshot_capture(
            [html, screenshot],
            previous_region_hash="same",
            current_region_hash="same",
            existing_artifact_keys=[html.object_key],
        )
        self.assertFalse(plan.region_changed)
        self.assertFalse(plan.run_extraction)
        self.assertFalse(plan.capture_screenshot)
        self.assertEqual(plan.upload_artifacts, ())

    def test_pipeline_upgrade_can_reextract_without_duplicate_storage(self) -> None:
        dom_map = build_snapshot_artifact(
            "dom_map",
            '{"nodes":[]}',
            content_type="application/json",
            retention_class="changed_snapshot",
        )
        plan = plan_snapshot_capture(
            [dom_map],
            previous_region_hash="same",
            current_region_hash="same",
            existing_artifact_keys=[dom_map.object_key],
            pipeline_version_changed=True,
        )
        self.assertTrue(plan.run_extraction)
        self.assertEqual(plan.upload_artifacts, ())


class PricingDomAndRawClaimTests(unittest.TestCase):
    PRICING_HTML = """
    <html><body>
      <nav>Documentation add-on $1</nav>
      <section id="pricing" aria-label="Pricing plans">
        <h2>Pricing</h2>
        <article class="plan"><h3>Free</h3><p>Free plan $0/month</p></article>
        <article class="plan"><h3>Pro</h3><p><span>$29</span> / month</p></article>
        <p>20,000 requests per month included</p>
        <p>$0.002 per request after the allowance</p>
        <a href="/sales">Contact sales for custom pricing</a>
        <table><tr><th>Plan</th><th>Monthly</th></tr><tr><td>Pro</td><td>$29</td></tr></table>
      </section>
      <script type="application/ld+json">{"@type":"Product","name":"Example"}</script>
    </body></html>
    """

    def test_dom_map_is_deterministic_and_preserves_structured_and_table_evidence(self) -> None:
        first = parse_pricing_dom(self.PRICING_HTML)
        second = parse_pricing_dom(self.PRICING_HTML)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.structured_data[0].parsed["@type"], "Product")
        pro_cell = next(node for node in first.nodes if node.tag == "td" and node.text == "Pro")
        self.assertEqual((pro_cell.table_row, pro_cell.table_column), (1, 0))

    def test_region_hash_ignores_unrelated_navigation_changes(self) -> None:
        first = detect_pricing_region(parse_pricing_dom(self.PRICING_HTML))
        changed_navigation = self.PRICING_HTML.replace(
            "Documentation add-on $1",
            "Documentation add-on $999 with unrelated navigation copy",
        )
        second = detect_pricing_region(parse_pricing_dom(changed_navigation))
        self.assertTrue(first.root_node_ids)
        self.assertEqual(first.region_hash, second.region_hash)

    def test_level1_claims_are_positive_evidence_bound_and_keep_currency_raw(self) -> None:
        dom_map = parse_pricing_dom(self.PRICING_HTML)
        region = detect_pricing_region(dom_map)
        claims = extract_level1_raw_claims(dom_map, region)
        by_type = {claim.claim_type: claim for claim in claims}
        self.assertTrue(by_type["has_free_plan"].raw_value)
        self.assertTrue(by_type["has_paid_pricing"].raw_value)
        self.assertTrue(by_type["has_custom_quote"].raw_value)
        self.assertTrue(by_type["has_usage_pricing"].raw_value)
        self.assertEqual(by_type["starting_paid_price"].raw_value["amount_raw"], "29")
        self.assertEqual(by_type["starting_paid_price"].raw_value["currency_symbol_raw"], "$")
        self.assertNotIn("currency_code_raw", by_type["starting_paid_price"].raw_value)
        self.assertEqual(by_type["usage_rate"].raw_value["unit_raw"], "request")
        self.assertEqual(by_type["free_allowance"].raw_value["quantity_raw"], "20,000")
        self.assertEqual(by_type["starting_price_period"].raw_value, "month")
        self.assertEqual(
            by_type["pricing_models"].raw_value,
            ["custom_quote", "hybrid", "subscription", "usage_based"],
        )
        self.assertTrue(all(claim.evidence[0].node_id for claim in claims))

    def test_trial_and_missing_facts_do_not_create_negative_or_free_plan_claims(self) -> None:
        html = """
        <main><section id="pricing"><h1>Pricing</h1><p>Start a 14 day free trial.</p>
        <p>Usage costs $0.01 per token.</p></section></main>
        """
        dom_map = parse_pricing_dom(html)
        claims = extract_level1_raw_claims(dom_map, detect_pricing_region(dom_map))
        by_type = {claim.claim_type: claim for claim in claims}
        self.assertNotIn("has_free_plan", by_type)
        self.assertNotIn("starting_paid_price", by_type)
        self.assertNotIn(False, [claim.raw_value for claim in claims])
        self.assertIn("usage_rate", by_type)

    def test_explicit_zero_recurring_price_is_free_plan_evidence(self) -> None:
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing plans</h1>
          <p>$0/month</p><p>Pro costs USD 10 per month.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        free_claim = next(claim for claim in bundle.raw_claims if claim.claim_type == "has_free_plan")
        normalization = normalize_raw_claim(free_claim)
        self.assertEqual(normalization.status, "not_applicable")
        self.assertEqual(
            validate_raw_claim(free_claim, normalization, bundle.dom_map, bundle.region).status,
            "entailed",
        )

    def test_discount_amount_without_price_relationship_is_not_a_paid_price(self) -> None:
        html = """
        <main><section id="pricing"><h1>Pricing plans</h1>
        <p>Save $10 when you choose annual billing.</p><p>Contact sales.</p>
        </section></main>
        """
        dom_map = parse_pricing_dom(html)
        claims = extract_level1_raw_claims(dom_map, detect_pricing_region(dom_map))
        claim_types = {claim.claim_type for claim in claims}
        self.assertNotIn("has_paid_pricing", claim_types)
        self.assertNotIn("starting_paid_price", claim_types)

    def test_per_seat_subscription_is_not_mislabeled_as_usage_or_hybrid(self) -> None:
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing plans</h1>
          <p>Team costs USD 12 per user per month.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        by_type = {claim.claim_type: claim for claim in bundle.raw_claims}
        self.assertNotIn("has_usage_pricing", by_type)
        self.assertNotIn("usage_rate", by_type)
        self.assertEqual(by_type["pricing_models"].raw_value, ["per_seat", "subscription"])
        normalization = normalize_raw_claim(by_type["pricing_models"])
        self.assertEqual(
            validate_raw_claim(
                by_type["pricing_models"],
                normalization,
                bundle.dom_map,
                bundle.region,
            ).status,
            "entailed",
        )

    def test_credit_rate_has_credit_and_usage_labels_without_false_hybrid(self) -> None:
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing plans</h1>
          <p>Usage costs USD 0.01 per credit.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        by_type = {claim.claim_type: claim for claim in bundle.raw_claims}
        self.assertEqual(by_type["pricing_models"].raw_value, ["credit_based", "usage_based"])
        self.assertTrue(by_type["has_usage_pricing"].raw_value)
        usage_normalization = normalize_raw_claim(by_type["usage_rate"])
        self.assertEqual(usage_normalization.status, "normalized")
        self.assertEqual(usage_normalization.normalized_value["unit_family"], "credit")

    def test_snapshot_bundle_uses_content_addressed_artifacts(self) -> None:
        bundle = build_pricing_snapshot_bundle(self.PRICING_HTML)
        artifact_types = {payload.artifact.artifact_type for payload in bundle.artifacts}
        self.assertEqual(artifact_types, {"html", "text", "structured_data", "dom_map"})
        self.assertTrue(bundle.region.region_hash)
        self.assertIn('"diagnostic_only":true', bundle.observed_currency_context)

    def test_rendered_snapshot_keeps_original_and_rendered_html(self) -> None:
        bundle = build_pricing_snapshot_bundle(
            self.PRICING_HTML.replace("$29", "$39"),
            rendered=True,
            original_html=self.PRICING_HTML,
        )
        artifact_types = {payload.artifact.artifact_type for payload in bundle.artifacts}
        self.assertEqual(
            artifact_types,
            {"html", "rendered_html", "text", "structured_data", "dom_map"},
        )


class DeterministicNormalizerValidatorTests(unittest.TestCase):
    def test_explicit_iso_currency_normalizes_to_decimal_and_minor_units(self) -> None:
        html = """
        <main><section id="pricing" aria-label="Pricing plans">
          <h1>Pricing plans</h1><p>Pro costs USD 29 per month.</p>
        </section></main>
        """
        bundle = build_pricing_snapshot_bundle(html)
        price_claim = next(
            claim for claim in bundle.raw_claims if claim.claim_type == "starting_paid_price"
        )
        normalization = normalize_raw_claim(price_claim)
        validation = validate_raw_claim(price_claim, normalization, bundle.dom_map, bundle.region)
        self.assertEqual(normalization.status, "normalized")
        self.assertEqual(
            normalization.normalized_value,
            {"amount": "29", "amount_minor": 2900, "currency": "USD", "currency_exponent": 2},
        )
        self.assertEqual(validation.status, "entailed")

    def test_ambiguous_dollar_symbol_is_not_defaulted_but_raw_claim_is_entailed(self) -> None:
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing</h1>
          <p>Pro starts at $29 per month.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        price_claim = next(
            claim for claim in bundle.raw_claims if claim.claim_type == "starting_paid_price"
        )
        normalization = normalize_raw_claim(price_claim)
        validation = validate_raw_claim(price_claim, normalization, bundle.dom_map, bundle.region)
        self.assertEqual(normalization.status, "failed")
        self.assertIn("ambiguous_currency_symbol", normalization.errors)
        self.assertEqual(normalization.normalized_value, {"amount": "29"})
        self.assertEqual(validation.status, "entailed")

    def test_quantity_suffixes_and_allowance_units_are_deterministic(self) -> None:
        self.assertEqual(str(normalize_quantity("1k")), "1000")
        self.assertEqual(str(normalize_quantity("1.5 million")), "1500000.0")
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing</h1>
          <p>1M tokens per month included.</p><p>Plans start at USD 10 per month.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        allowance = next(claim for claim in bundle.raw_claims if claim.claim_type == "free_allowance")
        normalization = normalize_raw_claim(allowance)
        self.assertEqual(normalization.status, "normalized")
        self.assertEqual(normalization.normalized_value["quantity"], "1000000")
        self.assertEqual(normalization.normalized_value["unit"], "token")
        self.assertEqual(normalization.normalized_value["period"], "month")

    def test_explicit_trial_and_no_card_claims_normalize_and_validate(self) -> None:
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing plans</h1>
          <p>Start a 14 day free trial. No credit card required.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        by_type = {claim.claim_type: claim for claim in bundle.raw_claims}
        trial_normalization = normalize_raw_claim(by_type["has_free_trial"])
        self.assertEqual(
            trial_normalization.normalized_value,
            {"available": True, "duration": "14", "duration_unit": "day"},
        )
        self.assertEqual(normalize_raw_claim(by_type["card_required"]).status, "not_applicable")
        self.assertFalse(by_type["card_required"].raw_value)
        self.assertEqual(
            validate_raw_claim(
                by_type["card_required"],
                normalize_raw_claim(by_type["card_required"]),
                bundle.dom_map,
                bundle.region,
            ).status,
            "entailed",
        )

    def test_validator_rejects_a_quote_that_cannot_be_relocated(self) -> None:
        html = """
        <section id="pricing" aria-label="Pricing plans"><h1>Pricing</h1>
          <p>Plans start at USD 29 per month.</p>
        </section>
        """
        bundle = build_pricing_snapshot_bundle(html)
        price_claim = next(
            claim for claim in bundle.raw_claims if claim.claim_type == "starting_paid_price"
        )
        broken_evidence = replace(price_claim.evidence[0], quote="USD 999 per month")
        broken_claim = replace(price_claim, evidence=(broken_evidence,))
        validation = validate_raw_claim(
            broken_claim,
            normalize_raw_claim(broken_claim),
            bundle.dom_map,
            bundle.region,
        )
        self.assertEqual(validation.status, "unsupported")
        self.assertIn("cannot be located", validation.reason)


class SubjectIdentityResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pro = KnownSubject.build(
            subject_id=11,
            subject_key="plan:11",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            current_name="Pro",
            name_aliases=("Professional",),
            audience="growing teams",
            card_signature=("unlimited search", "api access", "priority support"),
            anchor_signature=("most popular",),
            price_signature=("29 usd monthly",),
            position_hint=1,
        )

    def test_structured_id_is_strongest_signal(self) -> None:
        known = KnownSubject.build(
            subject_id=12,
            subject_key="plan:12",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            current_name="Anything",
            structured_ids=("stripe_price_pro",),
        )
        candidate = SubjectCandidate.build(
            candidate_key="candidate-pro",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            name="Changed completely",
            structured_id="stripe_price_pro",
        )
        result = resolve_subject_identity(candidate, [self.pro, known])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.subject_id, 12)
        self.assertEqual(result.score, 1.0)

    def test_rename_matches_existing_subject_from_stable_card_and_price_signals(self) -> None:
        candidate = SubjectCandidate.build(
            candidate_key="candidate-growth",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            name="Growth",
            audience="growing teams",
            card_signature=("api access", "priority support", "unlimited search"),
            anchor_signature=("most popular",),
            price_signature=("29 usd monthly",),
            position_hint=1,
        )
        result = resolve_subject_identity(candidate, [self.pro])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.subject_id, self.pro.subject_id)

    def test_reorder_does_not_break_identity(self) -> None:
        candidate = SubjectCandidate.build(
            candidate_key="candidate-pro-reordered",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            name="Pro",
            audience="growing teams",
            card_signature=("unlimited search", "api access", "priority support"),
            price_signature=("29 usd monthly",),
            position_hint=4,
        )
        result = resolve_subject_identity(candidate, [self.pro])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.subject_id, self.pro.subject_id)

    def test_similar_candidates_enter_identity_conflict(self) -> None:
        twin = KnownSubject.build(
            subject_id=13,
            subject_key="plan:13",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            current_name="Pro",
            audience="growing teams",
            card_signature=("unlimited search", "api access", "priority support"),
            anchor_signature=("most popular",),
            price_signature=("29 usd monthly",),
            position_hint=1,
        )
        candidate = SubjectCandidate.build(
            candidate_key="ambiguous-pro",
            subject_type="plan",
            source_scope="individual",
            region_identity="pricing-main",
            name="Pro",
            audience="growing teams",
            card_signature=("unlimited search", "api access", "priority support"),
            anchor_signature=("most popular",),
            price_signature=("29 usd monthly",),
            position_hint=1,
        )
        result = resolve_subject_identity(candidate, [self.pro, twin])
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.subject_id)
        self.assertEqual(result.best_candidate_subject_id, self.pro.subject_id)
        self.assertEqual(result.competing_subject_id, twin.subject_id)

    def test_toggle_offers_can_share_plan_but_keep_distinct_offer_subjects(self) -> None:
        monthly = KnownSubject.build(
            subject_id=21,
            subject_key="offer:pro:monthly",
            subject_type="offer",
            source_scope="individual",
            region_identity="pricing-main",
            current_name="Pro",
            billing_context="monthly",
            price_signature=("29 usd monthly",),
        )
        annual = KnownSubject.build(
            subject_id=22,
            subject_key="offer:pro:annual",
            subject_type="offer",
            source_scope="individual",
            region_identity="pricing-main",
            current_name="Pro",
            billing_context="annual",
            price_signature=("240 usd yearly",),
        )
        candidate = SubjectCandidate.build(
            candidate_key="pro-annual-state",
            subject_type="offer",
            source_scope="individual",
            region_identity="pricing-main",
            name="Pro",
            billing_context="annual",
            price_signature=("240 usd yearly",),
        )
        result = resolve_subject_identity(candidate, [monthly, annual])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.subject_id, annual.subject_id)

    def test_plan_rename_does_not_reset_unchanged_price_claim_lifecycle(self) -> None:
        previous_price = ClaimHistory(301, 11, "plan_price", "29-usd-monthly", 100, 104, 5)
        price_continuity = advance_claim_history(
            previous_price,
            subject_id=11,
            claim_type="plan_price",
            value_fingerprint="29-usd-monthly",
            snapshot_id=105,
        )
        self.assertEqual(price_continuity.action, "continue")
        self.assertEqual(price_continuity.first_seen_snapshot_id, 100)
        self.assertEqual(price_continuity.consecutive_seen_count, 6)

        previous_name = ClaimHistory(302, 11, "plan_name", "pro", 100, 104, 5)
        name_continuity = advance_claim_history(
            previous_name,
            subject_id=11,
            claim_type="plan_name",
            value_fingerprint="growth",
            snapshot_id=105,
        )
        self.assertEqual(name_continuity.action, "supersede")
        self.assertEqual(name_continuity.first_seen_snapshot_id, 105)
        self.assertEqual(name_continuity.superseded_claim_id, previous_name.claim_id)


if __name__ == "__main__":
    unittest.main()
