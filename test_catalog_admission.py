"""Publication admission regressions using the existing in-memory store fixture."""
import json
import unittest
from unittest.mock import patch

import runner
import test_runner_stores as fixtures


class CatalogAdmissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RunnerStoreLifecycleTests.setUp
    tearDown = fixtures.RunnerStoreLifecycleTests.tearDown
    add_tool = fixtures.RunnerStoreLifecycleTests.add_tool
    seed_primary_taxonomy = fixtures.RunnerStoreLifecycleTests.seed_primary_taxonomy
    seed_complete_enrichment = fixtures.RunnerStoreLifecycleTests.seed_complete_enrichment
    seed_publishable_tool = fixtures.RunnerStoreLifecycleTests.seed_publishable_tool

    def set_admission(self, tool_id, allowed):
        self.connection.execute(
            "UPDATE tool_sources SET raw_payload = ? WHERE tool_id = ?",
            [json.dumps({"qualification": {"catalog_fit": {"decision": {
                "policyVersion": "ai-market-admission-v2-2026-09-20",
                "aiPositioning": "ai_enhanced" if not allowed else "ai_core",
                "autoPublishAllowed": allowed,
            }}}}), tool_id],
        )
        self.connection.commit()

    async def test_enhanced_enriches_but_waits_for_publication_review(self):
        enhanced = self.add_tool("enhanced")
        self.seed_publishable_tool(enhanced, "enhanced")
        self.set_admission(enhanced, False)
        self.assertEqual(await runner.D1EnrichmentStore(self.d1).evaluate_tool(enhanced), "ready")
        self.assertEqual(self.connection.execute("SELECT status FROM tools WHERE id = ?", [enhanced]).fetchone()[0], "pending_review")
        core = self.add_tool("native", status="pending_review")
        self.seed_publishable_tool(core, "native")
        self.set_admission(core, True)
        legacy = self.add_tool("legacy", status="pending_review")
        self.seed_publishable_tool(legacy, "legacy")
        result = await runner.D1CatalogPublisher(self.d1, "test").publish_ready(10)
        self.assertEqual(result, {"selected": 2, "published": 2, "skipped": 0})
        self.assertEqual(self.connection.execute("SELECT status FROM tools WHERE id = ?", [enhanced]).fetchone()[0], "pending_review")

    async def test_commit_rechecks_new_admission_hold(self):
        tool_id = self.add_tool("race", status="pending_review")
        self.seed_publishable_tool(tool_id, "race")
        original_batch = self.d1.batch

        async def concurrent_hold(*args, **kwargs):
            self.set_admission(tool_id, False)
            return await original_batch(*args, **kwargs)

        with patch.object(self.d1, "batch", concurrent_hold):
            result = await runner.D1CatalogPublisher(self.d1, "test").publish_ready(10)
        self.assertEqual(result, {"selected": 1, "published": 0, "skipped": 1})
        self.assertEqual(self.connection.execute("SELECT count(*) FROM tool_change_log WHERE tool_id = ?", [tool_id]).fetchone()[0], 0)

    async def test_legacy_payloads_do_not_break_publication(self):
        # The production schema normally constrains JSON; exercise legacy data defensively.
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        for i, payload in enumerate(["null", "{}", "not-json"]):
            tool_id = self.add_tool(f"legacy-json-{i}", status="pending_review")
            self.seed_publishable_tool(tool_id, f"legacy-json-{i}")
            self.connection.execute("UPDATE tool_sources SET raw_payload = ? WHERE tool_id = ?", [payload, tool_id])
        self.connection.commit()
        result = await runner.D1CatalogPublisher(self.d1, "test").publish_ready(10)
        self.assertEqual(result["published"], 3)


if __name__ == "__main__":
    unittest.main()
