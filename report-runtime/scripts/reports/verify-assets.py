"""Independently reconcile public files, units, ZIPs and immutable release manifests."""
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import statistics
import struct
import zipfile

manifest = {}
headers = Path('public/_headers').read_text(encoding='utf-8')
def matching_headers(url):
    result = {}
    for block in re.split(r'\n\s*\n', headers.strip()):
        pattern, *lines = block.splitlines()
        pattern = re.sub(r':([A-Za-z]\w*)', r'(?P<\1>[^/]+)', pattern.replace('.', r'\.').replace('*', '(?P<splat>.*)'))
        match = re.fullmatch(pattern, url)
        if not match: continue
        for line in lines:
            key, value = line.strip().split(':', 1)
            for name, capture in match.groupdict().items(): value = value.replace(':'+name, capture)
            result[key.lower()] = result.get(key.lower(), []) + [value.strip()]
    return {key: ', '.join(values) for key, values in result.items()}
for source in sorted(Path('content/reports').glob('*.v*.json')):
    r = json.loads(source.read_text(encoding='utf-8'))
    if 'products' not in r: continue
    products = r['products']; count = len(products)
    previous = sum(p['previous'] for p in products); current = sum(p['current'] for p in products)
    growing = sum(p['current'] > p['previous'] for p in products)
    declining = sum(p['current'] < p['previous'] for p in products)
    head = sorted(products, key=lambda p: (-p['current'],p['domain']))[:3]
    prior_head = sorted(products,key=lambda p: (-p['previous'],p['domain']))[:3]
    expected_values = {
        'sample_count': count, 'previous_visits': previous, 'current_visits': current, 'visit_change': current-previous,
        'growth': (current/previous-1)*100, 'growing': growing, 'declining': declining, 'unchanged': count-growing-declining,
        'breadth': growing/count*100, 'median_growth': statistics.median(p['current']/p['previous']-1 for p in products)*100,
        'top3_share': sum(p['current'] for p in head)/current*100,
        'top3_share_change': (sum(p['current'] for p in head)/current - sum(p['previous'] for p in prior_head)/previous)*100,
        'current_top3_growth': (sum(p['current'] for p in head)/sum(p['previous'] for p in head)-1)*100,
    }
    channel = 'published' if r['publication']['status'] == 'published' else 'previews'
    root = Path('public/report-assets')/channel/r['sector']/r['month']/r['version']
    edition_manifest = {source.as_posix(): hashlib.sha256(source.read_bytes()).hexdigest()}
    for locale in ['en','zh-cn']:
        folder = root/locale
        members = {f'{chart}-{shape}.png' for chart in ['overview','trend','contribution'] for shape in ['landscape','portrait']} | {'summary.csv','cohort.csv','coverage.json','citation.txt','README.txt'}
        archive = folder/f'sigpik-report-{r["sector"]}-{r["month"]}-{r["version"]}.zip'
        with zipfile.ZipFile(archive) as z:
            assert set(z.namelist()) == members and z.testzip() is None
            assert all(z.read(name) == (folder/name).read_bytes() for name in members)
        for path in folder.glob('*.png'):
            raw = path.read_bytes(); assert raw[:8] == b'\x89PNG\r\n\x1a\n'
            dims = struct.unpack('>II',raw[16:24])
            assert dims == ((1200,630) if path.name == 'social-card.png' else (1080,1350) if 'portrait' in path.name else (1920,1080)), path
        rows = list(csv.DictReader(io.StringIO((folder/'summary.csv').read_text(encoding='utf-8-sig'))))
        values = {row['metric']:float(row['value']) for row in rows}
        assert len(values) == len(rows), 'Duplicate CSV metric'
        for key,value in expected_values.items(): assert abs(values[key]-value) < 1e-8, (r['id'],key)
        # Historical assets may omit this additive pair. When present, both
        # magnitudes must reconcile independently to every comparable domain.
        change_volumes = {
            'growing_visit_increase': sum(max(p['current']-p['previous'], 0) for p in products),
            'declining_visit_decrease': sum(max(p['previous']-p['current'], 0) for p in products),
        }
        if any(key in values for key in change_volumes):
            for key,value in change_volumes.items():
                assert key in values and values[key] == value, (r['id'],key)
            assert values['growing_visit_increase'] - values['declining_visit_decrease'] == values['visit_change']
        by_domain = {p['domain']:p for p in products}
        for row in rows:
            metric = row['metric']
            if ':' in metric:
                domain, kind = metric.rsplit(':',1); p = by_domain[domain]; delta = p['current']-p['previous']
                value = {'visit_change':delta,'growth':(p['current']/p['previous']-1)*100,'contribution':delta/previous*100}[kind]
                assert abs(float(row['value'])-value) < 1e-8
            expected_unit = 'percentage_points' if metric == 'top3_share_change' or metric.endswith(':contribution') else 'percent' if metric in ['growth','breadth','median_growth','top3_share','current_top3_growth'] or metric.endswith(':growth') else 'domains' if metric in ['sample_count','growing','declining','unchanged'] else 'estimated_visits'
            assert row['unit'] == expected_unit
            assert row['period'] == r['month'] and row['baseline_period'] == r['baselineMonth'] and row['version'] == r['version'] and int(row['sample_domains']) == count
            assert row['publication_status'] == r['publication']['status'] and row['published_on'] == (r['publication']['publishedOn'] or '')
            assert row['report_url'] == f'https://sigpik.com'+('/zh-cn' if locale=='zh-cn' else '')+f'/reports/{r["sector"]}/{r["month"]}'
        cohort = list(csv.DictReader(io.StringIO((folder/'cohort.csv').read_text(encoding='utf-8-sig'))))
        assert len(cohort) == count and {row['domain'] for row in cohort} == set(by_domain), 'Full cohort does not match the report'
        for row in cohort:
            product = by_domain[row['domain']]
            assert int(row['previous_visits']) == product['previous'] and int(row['current_visits']) == product['current']
            delta = product['current'] - product['previous']
            assert int(row['visit_change']) == delta
            assert abs(float(row['growth_percent']) - delta/product['previous']*100) < 1e-8
            assert abs(float(row['contribution_percentage_points']) - delta/previous*100) < 1e-8
            for key in ['period','baseline_period','version','report_url','publication_status','published_on']:
                assert row[key] == rows[0][key]
        coverage = json.loads((folder/'coverage.json').read_text(encoding='utf-8'))
        assert all(coverage[key] == value for key, value in r['coverage'].items())
        assert coverage['candidates'] == count + sum(coverage['exclusionCounts'].values())
        assert coverage['comparable'] == count and coverage['comparableCurrentVisits'] == current
        assert coverage['observedCurrentVisits'] >= current
        assert abs(coverage['shareOfObservedVisits'] - current/coverage['observedCurrentVisits']) < 1e-12
        assert coverage['publication'] == r['publication'] and coverage['reportId'] == r['id']
        assert coverage['period'] == r['month'] and coverage['baselinePeriod'] == r['baselineMonth']
        assert coverage['version'] == r['version'] and coverage['reportUrl'] == rows[0]['report_url']
        status_label = ('已发布' if locale == 'zh-cn' else 'Published') if channel == 'published' else ('发布前预览' if locale == 'zh-cn' else 'Publication preview')
        assert status_label in (folder/'citation.txt').read_text(encoding='utf-8')
        if channel == 'published':
            assert 'publication previews' not in (folder/'README.txt').read_text(encoding='utf-8').lower()
        for name in [archive.name,'summary.csv','cohort.csv','coverage.json','social-card.png']:
            applied = matching_headers('/'+(folder/name).as_posix().removeprefix('public/'))
            assert applied['cache-control'] == ('no-store' if channel == 'previews' else 'public, max-age=31536000, immutable')
            if not name.endswith('.png'): assert applied['content-disposition'] == f'attachment; filename="{name}"'
        for path in folder.iterdir():
            if path.suffix in {'.txt','.csv','.json'}:
                body = path.read_text(encoding='utf-8-sig')
                assert 'similarweb' not in body.lower() and 'https://sigpik.com/' in body
            edition_manifest[path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f'{r["id"]} {locale}: metrics, full cohort, coverage, status, PNG sizes, ZIP and attribution verified')
    lock = Path('content/reports/releases')/f'{r["id"]}.{r["version"]}.json'
    if r['publication']['status'] == 'published':
        assert lock.exists(), 'Published edition needs a release manifest'
        assert json.loads(lock.read_text(encoding='utf-8'))['sha256'] == edition_manifest, 'Published edition was modified'
    manifest.update(edition_manifest)
Path('work/reports').mkdir(parents=True,exist_ok=True)
Path('work/reports/asset-sha256.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
