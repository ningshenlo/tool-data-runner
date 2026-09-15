"""Unit tests for P2B Gold evaluation pure helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from taxonomy_eval import (
    EvalBundle,
    GoldRow,
    Prediction,
    build_evaluation_report,
    is_auto_accepted,
    load_gold_csv,
    prediction_from_run_row,
    primary_match,
    render_markdown_report,
    write_report_files,
)


class GoldLoadTests(unittest.TestCase):
    def test_load_csv_roundtrip(self):
        raw = (
            "tool_id,canonical_slug,official_url,entity_kind,primary_leaf_slug,"
            "primary_acceptable_alternates,capabilities_ok,use_cases_ok,user_types_ok,"
            "primary_must_not,notes,reviewer,reviewed_at\n"
            "43,synthesia,https://www.synthesia.io/,independent_product,video-generation-conversion,,"
            "text-to-video|avatar-video,,,music-generation,DRAFT sample,,\n"
            "48,elevenlabs,https://elevenlabs.io/,independent_product,voice-generation-conversion,"
            "speech-text-conversion,text-to-speech|voice-cloning,,,music-generation,reviewed,alice,2026-08-06\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gold.csv"
            path.write_text(raw, encoding="utf-8")
            rows = load_gold_csv(path)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].is_draft)
        self.assertFalse(rows[1].is_draft)
        self.assertEqual(rows[1].primary_acceptable_alternates, ["speech-text-conversion"])
        self.assertEqual(rows[0].capabilities_ok, ["text-to-video", "avatar-video"])


class MetricTests(unittest.TestCase):
    def test_prediction_comes_from_latest_run_payload(self):
        pred = prediction_from_run_row(
            {
                "run_id": 99,
                "tool_id": 54,
                "taxonomy_version": 2,
                "prompt_version": "shadow-top2-v2-2026-08-08",
                "model_name": "test-model",
                "run_status": "succeeded",
                "raw_output": json.dumps(
                    {
                        "leaf_accepted": {
                            "slug": "knowledge-management",
                            "confidence": 0.91,
                        },
                        "capabilities_accepted": [
                            {"slug": "document-chat", "confidence": 0.8},
                            {"slug": "summarization", "confidence": 0.7},
                        ],
                    }
                ),
            }
        )
        self.assertEqual(pred.primary_slug, "knowledge-management")
        self.assertEqual(pred.prompt_version, "shadow-top2-v2-2026-08-08")
        self.assertEqual(pred.source, "classification_run")
        self.assertEqual(pred.capabilities, ["document-chat", "summarization"])

    def test_primary_match_accepts_alternates(self):
        gold = GoldRow(
            tool_id=1,
            canonical_slug="x",
            official_url="",
            entity_kind="independent_product",
            primary_leaf_slug="voice-generation-conversion",
            primary_acceptable_alternates=["speech-text-conversion"],
        )
        self.assertTrue(primary_match(gold, "speech-text-conversion"))
        self.assertFalse(primary_match(gold, "music-generation"))

    def test_auto_accepted_simulation(self):
        pred = Prediction(tool_id=1, primary_slug="a", confidence=0.9, decision_status="provisional")
        self.assertTrue(is_auto_accepted(pred, threshold=0.85))
        self.assertFalse(is_auto_accepted(pred, threshold=0.95))
        pred2 = Prediction(tool_id=1, primary_slug="a", confidence=0.99, decision_status="unresolved")
        self.assertFalse(is_auto_accepted(pred2, threshold=0.5))

    def test_report_metrics(self):
        gold_rows = [
            GoldRow(
                tool_id=43,
                canonical_slug="synthesia",
                official_url="",
                entity_kind="independent_product",
                primary_leaf_slug="video-generation-conversion",
                capabilities_ok=["text-to-video", "avatar-video"],
                primary_must_not=["music-generation"],
                notes="DRAFT",
            ),
            GoldRow(
                tool_id=48,
                canonical_slug="elevenlabs",
                official_url="",
                entity_kind="independent_product",
                primary_leaf_slug="voice-generation-conversion",
                capabilities_ok=["text-to-speech"],
                notes="DRAFT",
            ),
            GoldRow(
                tool_id=99,
                canonical_slug="missing",
                official_url="",
                entity_kind="independent_product",
                primary_leaf_slug="music-generation",
                notes="DRAFT",
            ),
        ]
        shadow = {
            43: Prediction(
                tool_id=43,
                primary_slug="video-generation-conversion",
                confidence=0.9,
                decision_status="provisional",
                capabilities=["text-to-video", "avatar-video", "translation"],
            ),
            48: Prediction(
                tool_id=48,
                primary_slug="music-generation",  # wrong
                confidence=0.5,
                decision_status="provisional",
                capabilities=["text-to-speech"],
            ),
        }
        report = build_evaluation_report(
            EvalBundle(
                gold_rows=gold_rows,
                shadow=shadow,
                auto_accepted_threshold=0.85,
            )
        )
        metrics = report["metrics"]
        # 43 match, 48 mismatch, 99 missing => accuracy on evaluated=1/2=0.5
        self.assertEqual(metrics["overall_exact_accuracy"], 0.5)
        self.assertEqual(report["missing_shadow_n"], 1)
        self.assertEqual(metrics["auto_accepted_n"], 1)  # only 43 conf>=0.85
        self.assertEqual(metrics["auto_accepted_precision"], 1.0)
        self.assertEqual(report["evaluation_summary"]["shadow_match_gold"], 1)
        self.assertEqual(report["evaluation_summary"]["shadow_mismatch_gold"], 1)

        md = render_markdown_report(report)
        self.assertIn("auto_accepted Precision", md)
        self.assertIn("synthesia", md)

        with tempfile.TemporaryDirectory() as tmp:
            paths = write_report_files(report, tmp)
            self.assertTrue(Path(paths["latest_md"]).is_file())
            data = json.loads(Path(paths["latest_json"]).read_text(encoding="utf-8"))
            self.assertEqual(data["gold_n"], 3)


if __name__ == "__main__":
    unittest.main()
