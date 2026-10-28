"""Local liveness check: a busy attempt may take the configured render timeout."""
import json
from pathlib import Path
import time

root = Path('/app/work/reports/automation')
stamps = [p.stat().st_mtime for p in root.glob('*/*/state.json') if json.loads(p.read_text())['status'] == 'running']
heartbeat = root / 'heartbeat.json'
if heartbeat.exists():
    stamps.append(heartbeat.stat().st_mtime)
if not stamps or time.time() - max(stamps) > 2100:
    raise SystemExit(1)
