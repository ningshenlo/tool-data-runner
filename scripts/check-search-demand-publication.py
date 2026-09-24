"""Read-only preflight for an already published, closed traffic month."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import runner
from report_exports import shift_month
from search_demand_refresh import publish_month


async def check(month):
    shift_month(month,0)
    async with runner.D1Client(runner.load_config(require_brightdata=False)) as d1:
        snapshots=await d1.query('''SELECT id,traffic_month FROM market_snapshot_versions
          WHERE traffic_source=? AND traffic_month=? AND status IN ('active','retired')
          AND activated_at IS NOT NULL ORDER BY id DESC LIMIT 1''',[runner.TRAFFIC_SOURCE,month+'-01'])
        if not snapshots:
            raise RuntimeError('No published traffic snapshot for this month')
        print(json.dumps(await publish_month(d1,snapshots[0],runner.TRAFFIC_SOURCE,dry_run=True)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--month',required=True,help='YYYY-MM; SELECTs only')
    args=parser.parse_args()
    asyncio.run(check(args.month))
