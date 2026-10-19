"""Offline Docker smoke test. All visit counts are synthetic test fixtures.

Prepare exports separately, then check with the exports volume mounted read-only.
No credentials, website report data or artifacts leave these temporary volumes.
"""
import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import report_exports as producer

MONTH = '2025-08'
NOW = 1757840400
REQUIRED = {
    'music-generation': ['suno.com', 'flowmusic.app', 'mureka.ai', 'udio.com'],
    'image-generation': ['midjourney.com'],
    # Intentionally missing Runway: one blocked market must not stop the others.
    'video-generation': [],
}


def observations(sector):
    domains = REQUIRED[sector] + [f'fixture-{i}.example.com' for i in range(12)]
    return [dict(domain=domain, slug=f'fixture-{i}', category_member=1,
                 current_visits=100000 + i * 3000, previous_visits=105000 + i * 2000,
                 older_visits=102000 + i * 1000, current_schema=2, previous_schema=2,
                 older_schema=2, current_task='done', previous_task='done') for i, domain in enumerate(domains)]


class FixtureD1:
    async def query(self, sql, params):
        assert sql.lstrip().startswith(('SELECT', 'WITH'))
        if 'traffic_month_release_checks' in sql:
            return [{'status': 'available'}]
        return copy.deepcopy(observations(params[0]))


async def prepare(exports):
    markets = [dict(sector=sector, categorySlug=sector, supplements=[]) for sector in REQUIRED]
    result = await producer.export_month(FixtureD1(), exports, MONTH, markets, NOW)
    assert all(item['status'] == 'complete' for item in result['results']), result
    assert len(list(exports.rglob('complete.json'))) == 3
    print('Synthetic completion events prepared; no live data or network used.')


def hash_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def check(root, exports):
    state = root / 'work/reports/automation'
    before = hash_tree(exports)
    immutable = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in json.loads((root / 'bundle.json').read_text(encoding='utf-8'))['files']}
    command = [sys.executable, str(root / 'scripts/reports/draft-worker.py'), '--root', str(root), '--exports', str(exports), '--once']
    first = subprocess.run(command, cwd=root)
    assert first.returncode == 2, first.returncode
    states = {p.parent.name: json.loads(p.read_text(encoding='utf-8')) for p in state.glob(f'{MONTH}/*/state.json')}
    assert {key: value['status'] for key, value in states.items()} == {'image-generation': 'review', 'music-generation': 'review', 'video-generation': 'blocked'}, states
    assert 'runwayml.com' in states['video-generation']['reason'], states['video-generation']
    for sector in ['music-generation', 'image-generation']:
        folder = Path(states[sector]['folder'])
        manifest = json.loads((folder / 'draft.json').read_text(encoding='utf-8'))
        for file, expected in manifest['files'].items():
            assert hashlib.sha256((folder / file).read_bytes()).hexdigest() == expected, file
        for file, expected in manifest['generator'].items():
            assert hashlib.sha256((root / file).read_bytes()).hexdigest() == expected, file
        assert json.loads((folder / 'approval.json').read_text(encoding='utf-8'))['status'] == 'pending'
        assert len(list((folder / 'public').rglob('*.png'))) == 14
        assert all(states[sector].get('deployed') is False for sector in states)
    receipts = list(state.rglob('receipt.json'))
    second = subprocess.run(command, cwd=root)
    assert second.returncode == 2
    assert len(list(state.rglob('receipt.json'))) == len(receipts), 'Restart duplicated completed drafts or ignored retry backoff'
    assert hash_tree(exports) == before, 'Consumer modified upstream data'
    assert all(hashlib.sha256((root / p).read_bytes()).hexdigest() == value for p, value in immutable.items())
    denied = subprocess.run([os.environ.get('REPORT_NODE', 'node'), str(root / 'scripts/reports/pipeline.mjs'), 'release', '--draft', str(folder)], cwd=root, capture_output=True, text=True)
    assert denied.returncode == 1 and 'only prepares drafts' in denied.stderr
    print('PASS: 2 drafts / 28 charts verified; missing-coverage isolation, restart deduplication, immutable exports and release restriction verified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--root', type=Path, default=Path('/app'))
    parser.add_argument('--exports', type=Path, default=Path('/exports'))
    args = parser.parse_args()
    if args.prepare:
        asyncio.run(prepare(args.exports.resolve()))
    else:
        check(args.root.resolve(), args.exports.resolve())
