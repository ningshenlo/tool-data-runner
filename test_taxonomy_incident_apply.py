import unittest

from taxonomy_incident_apply import build_member_statements, validate_plan


class PlanValidationTests(unittest.TestCase):
    def valid_report(self):
        return {
            "mode": "dry-run-read-only-no-model-calls",
            "incident_id": "INC-TEST",
            "rollback_plan_hash": "a" * 64,
            "apply_ready": True,
            "invariants": {"paid_model_calls": 0, "manual_entity_changed": 0},
            "apply_blockers": {"ambiguous_members": 0, "active_reprocess_requests": 0},
            "summary": {"cohort_total": 1},
            "members": [
                {
                    "tool_id": 1,
                    "incident_run_id": 10,
                    "action": "reset_entity_to_unresolved",
                    "current_entity": {"kind": "non_product", "source": "auto"},
                    "desired_entity": {"kind": "unresolved", "source": "auto"},
                    "blockers": [],
                    "effects": [
                        {
                            "effect_type": "entity_current_state",
                            "effect_id": 1,
                            "before_state": {"kind": "non_product", "source": "auto"},
                            "after_state": {"kind": "unresolved", "source": "auto"},
                        }
                    ],
                }
            ],
        }

    def test_valid_plan_passes(self):
        validate_plan(
            self.valid_report(),
            expected_incident_id="INC-TEST",
            expected_plan_hash="a" * 64,
        )

    def test_plan_with_manual_mutation_is_rejected(self):
        report = self.valid_report()
        report["members"][0]["current_entity"]["source"] = "manual"
        with self.assertRaisesRegex(RuntimeError, "manual entity mutation"):
            validate_plan(
                report,
                expected_incident_id="INC-TEST",
                expected_plan_hash="a" * 64,
            )

    def test_plan_hash_mismatch_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            validate_plan(
                self.valid_report(),
                expected_incident_id="INC-TEST",
                expected_plan_hash="b" * 64,
            )


class StatementGuardTests(unittest.TestCase):
    def test_entity_and_profile_mutations_have_guards_and_audit_copy(self):
        member = {
            "tool_id": 1,
            "incident_run_id": 10,
            "action": "reset_entity_to_unresolved",
            "effects": [
                {
                    "effect_type": "entity_current_state",
                    "effect_id": 1,
                    "before_state": {"kind": "non_product", "source": "auto"},
                    "after_state": {"kind": "unresolved", "source": "auto"},
                },
                {
                    "effect_type": "product_profile",
                    "effect_id": 1,
                    "before_state": {
                        "profile_version": 1,
                        "extracted_at": "2026-08-12T00:00:00Z",
                        "updated_at": "2026-08-12T00:00:01Z",
                    },
                    "after_state": "invalidated_needs_revalidation",
                },
            ],
        }
        statements = build_member_statements(
            member,
            incident_id="INC-TEST",
            member_id=99,
            actor="test",
            now="2026-08-15T00:00:00Z",
            frozen_at="2026-08-14T23:59:00Z",
        )
        sql = "\n".join(statement for statement, _ in statements)
        for statement, params in statements:
            self.assertEqual(statement.count("?"), len(params))
        self.assertGreaterEqual(sql.count("taxonomy_incident_guard_failed"), 3)
        self.assertIn("classification_reprocess_requests", sql)
        self.assertIn("newer.created_at > ?", sql)
        self.assertIn("decision.decision LIKE 'set_entity_kind:%'", sql)
        self.assertIn("INSERT INTO taxonomy_incident_invalidations", sql)
        self.assertIn("json(p.profile_json)", sql)
        self.assertIn("DELETE FROM product_profiles", sql)
        self.assertIn("UPDATE tools", sql)
        self.assertNotIn("UPDATE classification_runs", sql)


if __name__ == "__main__":
    unittest.main()
