import unittest
from pathlib import Path


class DockerImageContractTests(unittest.TestCase):
    def test_runner_local_modules_are_copied_into_image(self):
        dockerfile = (Path(__file__).resolve().parent / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("COPY scripts/check-market-publication.py ./scripts/", dockerfile)
        self.assertIn("COPY scripts/check-search-demand-publication.py ./scripts/", dockerfile)
        for module in (
            "runner.py",
            "market_snapshot_refresh.py",
            "search_demand_refresh.py",
            "search_demand_brand.py",
            "report_exports.py",
            "report-markets.json",
            "taxonomy_shadow.py",
            "taxonomy_batch.py",
            "anti_bot_signatures.py",
            "classification_anomalies.py",
        ):
            self.assertIn(f"COPY {module} .", dockerfile)


if __name__ == "__main__":
    unittest.main()
