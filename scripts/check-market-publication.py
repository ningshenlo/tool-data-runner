"""Read-only preflight for an explicit, closed calendar month."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner
from market_snapshot_refresh import month_readiness
from report_exports import shift_month


async def check(month):
    shift_month(month, 0)
    if month >= datetime.now(timezone.utc).strftime('%Y-%m'):
        raise ValueError('Only closed calendar months can be checked')
    async with runner.D1Client(runner.load_config(require_brightdata=False)) as d1:
        versions=await d1.query('SELECT id,status,traffic_month,baseline_month FROM market_snapshot_versions ORDER BY id DESC LIMIT 8')
        release=await d1.query('SELECT status FROM traffic_month_release_checks WHERE source=? AND traffic_month=?',[runner.TRAFFIC_SOURCE,month+'-01'])
        readiness=await month_readiness(d1,month+'-01',source=runner.TRAFFIC_SOURCE,visible_tools_ctes=runner.MARKET_VISIBLE_TOOLS_CTES)
        result={'month':month,'versions':versions,'release':release,'readiness':readiness}
        print(json.dumps(result))
        return readiness['status']=='ready' and bool(release) and release[0]['status']=='available'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--month',required=True,help='YYYY-MM; SELECTs only')
    args=parser.parse_args()
    try:
        raise SystemExit(0 if asyncio.run(check(args.month)) else 2)
    except Exception as error:
        print(json.dumps({'status':'blocked','error_type':type(error).__name__,'reason':str(error)[:1200]}))
        raise SystemExit(1)
