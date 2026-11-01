import unittest
from pathlib import Path


class DockerImageContractTests(unittest.TestCase):
    def test_runner_local_modules_are_copied_into_image(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
            encoding="utf-8"
        )
        for module in (
            "runner.py",
            "report_exports.py",
            "market_snapshot_refresh.py",
            "search_demand_refresh.py",
            "search_demand_brand.py",
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
        self.assertIn("COPY shared ./shared", dockerfile)
        self.assertIn("COPY workloads ./workloads", dockerfile)
        self.assertIn("COPY contracts ./contracts", dockerfile)
        self.assertIn("COPY config ./config", dockerfile)
        self.assertIn("COPY libs ./libs", dockerfile)
        self.assertIn("COPY ops ./ops", dockerfile)
        self.assertIn("COPY postgres_client.py postgres_legacy.py ./", dockerfile)
        self.assertIn("COPY report-markets.json .", dockerfile)
        self.assertIn("COPY scripts/check-market-publication.py ./scripts/", dockerfile)
        self.assertIn("COPY scripts/check-search-demand-publication.py ./scripts/", dockerfile)


if __name__ == "__main__":
    unittest.main()
