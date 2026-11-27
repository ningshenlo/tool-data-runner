import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from taxonomy_shadow import TaxonomyCatalog, TaxonomyTerm
from workloads.enrichment.openai_profile_batch import (
    apply_profile_enrichment_batch,
    build_profile_enrichment_batch,
    enrichment_profile_schema,
    parse_and_validate_enrichment_output,
)


class OpenAIProfileBatchTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_and_validation_filter_capabilities_and_pricing_mentions(self):
        schema = enrichment_profile_schema()
        self.assertIn("capabilities", schema["required"])
        self.assertIn("use_cases", schema["required"])
        manifest_item = {
            "tool_id": 10,
            "domain": "acme.ai",
            "allowed_capabilities": {"text-to-video": 501, "lip-sync": 502},
        }
        raw = {
            "capabilities": [
                {"slug": "text-to-video", "role": "core", "confidence": 0.91},
                {"slug": "lip-sync", "role": "supporting", "confidence": 0.40},
                {"slug": "invented-slug", "role": "core", "confidence": 0.99},
            ],
            "long_description": "Acme turns written scripts into narrated videos for product teams. Paid plans start at $19 per month.",
            "use_cases": [
                "Educators producing explainer videos from lecture notes",
                "Subscription tier unlocked for $19 per month",
            ],
            "key_features": [
                {"name": "Script to video", "description": "Generates scenes directly from markdown drafts."},
                {"name": "Free tier credits", "description": "Includes $0 starter plan."},
            ],
        }
        cleaned = parse_and_validate_enrichment_output(raw, manifest_item)
        self.assertEqual([c["slug"] for c in cleaned["capabilities"]], ["text-to-video"])
        self.assertNotIn("$19", cleaned["long_description"])
        self.assertIn("written scripts", cleaned["long_description"])
        self.assertEqual(cleaned["use_cases"], ["Educators producing explainer videos from lecture notes"])
        self.assertEqual([kf["name"] for kf in cleaned["key_features"]], ["Script to video"])

    async def test_build_and_dry_run_apply_roundtrip(self):
        fake_pg = MagicMock()
        fake_pg.__aenter__ = AsyncMock(return_value=fake_pg)
        fake_pg.__aexit__ = AsyncMock(return_value=False)
        fake_pg.query = AsyncMock(
            return_value=[
                {
                    "tool_id": 77,
                    "canonical_slug": "clipmaker",
                    "normalized_domain": "clipmaker.ai",
                    "official_url": "https://clipmaker.ai",
                    "seo_index_status": "indexable",
                    "name": "ClipMaker",
                    "short_description": "AI video clip generator for podcasts.",
                    "long_description": "",
                    "use_cases": "[]",
                    "feature_highlights": "[]",
                    "primary_term_id": 10,
                    "primary_slug": "video-animation",
                    "primary_name": "Video & Animation",
                    "profile_json": json.dumps({"primary_job": {"value": "Clip long podcasts into shorts"}}),
                    "sw_desc": "AI video clipping platform.",
                    "key_feature_count": 0,
                    "accepted_capability_count": 0,
                }
            ]
        )
        fake_catalog = TaxonomyCatalog(
            terms=[
                TaxonomyTerm(term_id=10, dimension="primary_category", slug="video-animation", name="Video & Animation", includes="video editing, clipping"),
                TaxonomyTerm(term_id=501, dimension="capability", slug="video-editing", name="Video Editing", definition="Cut and edit video clips"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("workloads.enrichment.openai_profile_batch.PostgresClient", return_value=fake_pg), \
                 patch("workloads.enrichment.openai_profile_batch.load_pg_taxonomy_catalog", AsyncMock(return_value=fake_catalog)), \
                 patch("workloads.enrichment.openai_profile_batch.fetch_static_evidence_text", AsyncMock(return_value=("Clip long podcasts into social videos automatically.", "static_http"))):
                built = await build_profile_enrichment_batch(
                    None,
                    batch_dir=tmpdir,
                    limit=5,
                    database_url="postgresql://ro@127.0.0.1:5432/sigpik",
                )

            self.assertEqual(built["queued_requests"], 1)
            line = json.loads((Path(tmpdir) / "requests.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(line["custom_id"], "enrich-tool-77")
            self.assertEqual(line["url"], "/v1/responses")

            # Simulate completed Batch output.jsonl
            output_line = {
                "custom_id": "enrich-tool-77",
                "response": {
                    "status_code": 200,
                    "body": {
                        "output_text": json.dumps(
                            {
                                "capabilities": [{"slug": "video-editing", "role": "core", "confidence": 0.92}],
                                "long_description": "ClipMaker turns long podcast recordings into ready-to-publish vertical clips with automated speaker framing and captions.",
                                "use_cases": ["Podcasters repurposing long episodes into social clips"],
                                "key_features": [{"name": "Auto Highlight Detection", "description": "Finds shareable moments across multi-hour recordings."}],
                            }
                        ),
                        "usage": {"input_tokens": 500, "output_tokens": 120, "total_tokens": 620},
                    },
                },
            }
            (Path(tmpdir) / "output.jsonl").write_text(json.dumps(output_line) + "\n", encoding="utf-8")

            applied = await apply_profile_enrichment_batch(None, batch_dir=tmpdir, dry_run=True)
            self.assertEqual(applied["parsed_ok"], 1)
            self.assertEqual(applied["samples"][0]["capabilities"][0]["slug"], "video-editing")


if __name__ == "__main__":
    unittest.main()
