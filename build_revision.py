"""Fingerprint exactly the source files shipped in the image."""
import hashlib
from pathlib import Path

def revision(root=None):
    root = Path(root or Path(__file__).parent)
    files = [root / name for name in ["runner.py", "d1_costguard.py", "taxonomy_shadow.py", "taxonomy_batch.py", "anti_bot_signatures.py", "classification_anomalies.py", "requirements.txt"]]
    for folder in ["pricing", "sitemap_monitor"]:
        files.extend((root / folder).rglob("*.py"))
    digest = hashlib.sha256()
    for file in sorted(files):
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(file.read_bytes().replace(b"\r\n", b"\n"))
    return "cost-v1-" + digest.hexdigest()[:20]

if __name__ == "__main__":
    print(revision())
