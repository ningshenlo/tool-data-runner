import unittest
from pathlib import Path


class DockerImageContractTests(unittest.TestCase):
    def test_runner_local_modules_are_copied_into_image(self):
        dockerfile = (Path(__file__).resolve().parent / "Dockerfile").read_text(
            encoding="utf-8"
        )
        for module in (
            "runner.py",
            "report_exports.py",
            "market_snapshot_refresh.py",
            "search_demand_refresh.py",
            "search_demand_brand.py",
            "d1_costguard.py",
            "taxonomy_shadow.py",
            "taxonomy_precision.py",
            "taxonomy_schema.py",
            "taxonomy_batch.py",
            "jev.py",
            "jev_capabilities.py",
            "jev_taxonomy.py",
            "anti_bot_signatures.py",
            "classification_anomalies.py",
        ):
            self.assertIn(f"COPY {module} .", dockerfile)
        self.assertIn("COPY report-markets.json .", dockerfile)
        self.assertIn("COPY scripts/check-market-publication.py ./scripts/", dockerfile)
        self.assertIn("COPY scripts/check-search-demand-publication.py ./scripts/", dockerfile)


if __name__ == "__main__":
    unittest.main()
