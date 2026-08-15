import json
from pathlib import Path
import tempfile
import unittest

from taxonomy_incident_rollback import (
    IncidentMember,
    build_rollback_plan,
    load_frozen_cohort,
)


def run_row(
    run_id: int,
    tool_id: int,
    created_at: str,
    kind: str,
    *,
    accepted: int = 1,
    neutral: int = 0,
    blocked: int = 0,
    error: str = "",
):
    return {
        "id": run_id,
        "tool_id": tool_id,
        "created_at": created_at,
        "entity_kind": kind,
        "entity_source": "auto",
        "entity_accepted": accepted,
        "neutral_transport": neutral,
        "blocked_evidence": blocked,
        "error": error,
        "prompt_version": "v2",
    }


class IncidentManifestTests(unittest.TestCase):
    def test_frozen_manifest_keeps_repaired_members_and_captured_run(self):
        payload = {
            "generated_at": "2026-08-14T09:13:04Z",
            "prompt_version": "v2",
            "summary": {"cohort_total": 2},
            "entity_decisions": [
                {"tool_id": 1, "tool_name": "A", "canonical_slug": "a"},
                {"tool_id": 2, "tool_name": "B", "canonical_slug": "b"},
            ],
            "records": [
                {
                    "tool_id": 1,
                    "latest_run_id": 101,
                    "matches": [{"provider": "neutral_transport", "code": "example_domain"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            metadata, members = load_frozen_cohort(path)
        self.assertEqual(metadata["cohort_total"], 2)
        self.assertEqual(members[0].incident_run_id, 101)
        self.assertEqual(members[1].provenance_tier, "missing_run_pointer")


class RuntimeFreezeContractTests(unittest.TestCase):
    def test_paid_incident_recheck_is_disabled_by_default(self):
        runner_source = Path("runner.py").read_text(encoding="utf-8")
        env_example = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn(
            '"TAXONOMY_RECHECK_AUTO_NON_PRODUCT", False', runner_source
        )
        self.assertIn("TAXONOMY_RECHECK_AUTO_NON_PRODUCT=0", env_example)


class RollbackPlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "captured_at": "2026-08-14T09:13:04Z",
            "prompt_version": "v2",
            "member_fingerprint": "frozen",
        }

    def build(self, members, *, tools, runs, assignments=None, decisions=None, requests=None):
        return build_rollback_plan(
            incident_id="INC-TEST",
            manifest=self.manifest,
            members=members,
            snapshot={
                "tools": tools,
                "runs": runs,
                "assignments": assignments or [],
                "decisions": decisions or [],
                "requests": requests or [],
            },
        )

    def member(self, tool_id=1, run_id=20):
        return IncidentMember(tool_id, run_id, "Tool", "tool", "neutral", "captured_run")

    def tool(self, *, kind="non_product", source="auto", profile_neutral=0):
        return {
            "tool_id": 1,
            "entity_kind": kind,
            "entity_kind_source": source,
            "profile_neutral_transport": profile_neutral,
            "profile_json": "{}",
        }

    def incident(self):
        return run_row(
            20,
            1,
            "2026-08-14T09:00:00Z",
            "non_product",
            neutral=1,
            error="entity_not_eligible:non_product",
        )

    def test_manual_entity_is_never_changed(self):
        report = self.build(
            [self.member()],
            tools=[self.tool(kind="independent_product", source="manual")],
            runs=[self.incident()],
            decisions=[
                {
                    "id": 8,
                    "tool_id": 1,
                    "dimension": "entity_kind",
                    "decision": "set_entity_kind:independent_product",
                }
            ],
        )
        plan = report["members"][0]
        self.assertEqual(plan["action"], "manual_protected_untouched")
        self.assertEqual(plan["effects"], [])
        self.assertEqual(report["invariants"]["manual_entity_changed"], 0)

    def test_later_clean_run_wins_without_rollback(self):
        later = run_row(30, 1, "2026-08-15T09:00:00Z", "independent_product")
        report = self.build(
            [self.member()],
            tools=[self.tool(kind="independent_product", source="auto")],
            runs=[self.incident(), later],
        )
        plan = report["members"][0]
        self.assertEqual(plan["action"], "already_repaired_by_later_clean_run")
        self.assertEqual(plan["surviving_run_id"], 30)
        self.assertEqual(plan["effects"], [])

    def test_previous_clean_auto_is_restored(self):
        previous = run_row(10, 1, "2026-08-13T09:00:00Z", "independent_product")
        report = self.build(
            [self.member()],
            tools=[self.tool()],
            runs=[previous, self.incident()],
        )
        plan = report["members"][0]
        self.assertEqual(plan["action"], "restore_previous_clean_auto")
        self.assertEqual(plan["desired_entity"]["kind"], "independent_product")
        self.assertEqual(plan["effects"][0]["surviving_run_id"], 10)

    def test_no_previous_state_resets_to_unresolved(self):
        report = self.build(
            [self.member()], tools=[self.tool()], runs=[self.incident()]
        )
        plan = report["members"][0]
        self.assertEqual(plan["action"], "reset_entity_to_unresolved")
        self.assertEqual(plan["desired_entity"]["kind"], "unresolved")

    def test_missing_captured_pointer_is_ambiguous_and_has_no_effects(self):
        member = IncidentMember(1, None, "Tool", "tool", "captured", "missing_run_pointer")
        report = self.build([member], tools=[self.tool()], runs=[self.incident()])
        plan = report["members"][0]
        self.assertEqual(plan["action"], "ambiguous_provenance")
        self.assertFalse(plan["apply_eligible"])
        self.assertEqual(plan["effects"], [])

    def test_active_request_blocks_apply(self):
        report = self.build(
            [self.member()],
            tools=[self.tool()],
            runs=[self.incident()],
            requests=[{"id": 9, "tool_id": 1, "status": "queued"}],
        )
        plan = report["members"][0]
        self.assertIn("active_reprocess_request", plan["blockers"])
        self.assertFalse(plan["apply_eligible"])

    def test_only_exact_incident_auto_assignment_is_superseded(self):
        assignments = [
            {
                "id": 1,
                "tool_id": 1,
                "run_id": 20,
                "source": "auto",
                "decision_status": "auto_accepted",
                "dimension": "primary_category",
                "is_primary": 1,
            },
            {
                "id": 2,
                "tool_id": 1,
                "run_id": None,
                "source": "legacy",
                "decision_status": "legacy",
                "dimension": "primary_category",
                "is_primary": 0,
            },
        ]
        report = self.build(
            [self.member()],
            tools=[self.tool()],
            runs=[self.incident()],
            assignments=assignments,
        )
        effects = report["members"][0]["effects"]
        assignment_effects = [e for e in effects if e["effect_type"] == "taxonomy_assignment"]
        self.assertEqual([e["effect_id"] for e in assignment_effects], [1])
        self.assertEqual(report["summary"]["legacy_mapping_retained"], 1)


if __name__ == "__main__":
    unittest.main()
