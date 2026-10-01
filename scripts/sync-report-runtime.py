"""Vendor the report generator, with hashes instead of unpublished site data.

Run with --source /path/to/sigpik after changing report code or publishing an
edition. --verify needs only this repository and is used by the Docker build.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATIC = ['content/reports/registry.ts', 'content/reports/presentation.ts',
          'lib/reports/model.ts', 'lib/market-statistics.ts', 'config/seo.ts',
          'public/_headers', 'public/brand/sigpik-logo.png', 'scripts/reports/requirements.txt']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def write_json(file, value):
    file.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def allowed(file):
    return file in STATIC or (file.startswith('scripts/reports/') and len(file.split('/')) == 3 and Path(file).suffix in {'.py', '.mjs'})


def verify(target):
    receipt = json.loads((target / 'bundle.json').read_text('utf-8'))
    body = {k: v for k, v in receipt.items() if k != 'bundleDigest'}
    assert receipt['bundleDigest'] == digest(body), 'Bundle receipt changed'
    assert body['schemaVersion'] == 1
    actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    assert not any(p.is_symlink() for p in target.rglob('*')), 'Symlinks are not allowed'
    assert actual == set(body['files']) | {'bundle.json'}, 'Unexpected or missing runtime file'
    for file, expected in body['files'].items():
        assert file == 'runtime-baseline.json' or allowed(file), 'Non-code report content in public bundle'
        assert sha((target / file).read_bytes()) == expected, f'Runtime changed: {file}'
    baseline = json.loads((target / 'runtime-baseline.json').read_text('utf-8'))
    assert baseline['manifestDigest'] == digest({k: v for k, v in baseline.items() if k != 'manifestDigest'})
    assert baseline['mode'] == 'draft-only'
    return receipt


def sync(source, target):
    assert source.is_dir() and source != target
    files = STATIC + sorted(p.relative_to(source).as_posix() for p in (source / 'scripts/reports').iterdir() if p.suffix in {'.py', '.mjs'})
    contents = {}
    for file in files:
        assert allowed(file) and not (source / file).is_symlink()
        contents[file] = (source / file).read_bytes()
    virtual = {}
    editions = {}
    for prefix in ['content/reports', 'public/report-assets']:
        for file in sorted((source / prefix).rglob('*')):
            assert not file.is_symlink()
            if not file.is_file() or (prefix == 'content/reports' and file.suffix != '.json'):
                continue
            relative = file.relative_to(source).as_posix()
            virtual[relative] = sha(file.read_bytes())
            if file.parent == source / 'content/reports' and file.name.endswith('.v1.json'):
                data = json.loads(file.read_text('utf-8'))
                assert data['id'] + '.v1.json' == file.name
                assert data['publication']['status'] in ['review', 'published']
                editions[data['id']] = data['publication']['status']
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    baseline = {'schemaVersion': 1, 'mode': 'draft-only', 'sourceCommit': revision, 'files': virtual, 'editions': editions}
    baseline['manifestDigest'] = digest(baseline)
    contents['runtime-baseline.json'] = (json.dumps(baseline, ensure_ascii=False, indent=2) + '\n').encode()
    target.mkdir(parents=True, exist_ok=True)
    # Do not silently keep obsolete files or delete arbitrary directory contents.
    previous = json.loads((target / 'bundle.json').read_text('utf-8'))['files'] if (target / 'bundle.json').exists() else {}
    for file in set(previous) - set(contents):
        assert allowed(file) and (target / file).resolve().is_relative_to(target.resolve())
        (target / file).unlink()
    for file, data in contents.items():
        destination = target / file
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    receipt = {'schemaVersion': 1, 'sourceCommit': revision, 'files': {file: sha(data) for file, data in sorted(contents.items())}}
    receipt['bundleDigest'] = digest(receipt)
    write_json(target / 'bundle.json', receipt)
    return verify(target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--target', type=Path, default=ROOT / 'report-runtime')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify == bool(args.source):
        parser.error('Choose --source or --verify')
    receipt = verify(args.target.resolve()) if args.verify else sync(args.source.resolve(), args.target.resolve())
    print(json.dumps({'bundleDigest': receipt['bundleDigest'], 'files': len(receipt['files']), 'containsReportData': False}))
