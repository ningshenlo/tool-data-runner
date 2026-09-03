import unittest
from pathlib import Path


class DockerImageContractTests(unittest.TestCase):
    def test_runner_local_modules_are_copied_into_image(self):
        dockerfile = (Path(__file__).resolve().parent / "Dockerfile").read_text(
            encoding="utf-8"
        )
        for module in (
            "runner.py",
            "taxonomy_shadow.py",
            "taxonomy_batch.py",
            "anti_bot_signatures.py",
            "classification_anomalies.py",
        ):
            self.assertIn(f"COPY {module} .", dockerfile)


if __name__ == "__main__":
    unittest.main()
