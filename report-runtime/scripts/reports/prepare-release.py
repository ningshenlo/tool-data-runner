"""Stage a reviewed edition at a new immutable URL, then install it with rollback on failure."""
import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('report_id')
parser.add_argument('--apply', action='store_true')
parser.add_argument('--reviewed-by')
parser.add_argument('--published-on', default=date.today().isoformat())
args = parser.parse_args()
if not re.fullmatch(r'[a-z-]+-\d{4}-\d{2}', args.report_id): raise ValueError('Invalid report ID')
payload = json.loads((Path('work/reports') / (args.report_id+'.render-input.json')).read_text(encoding='utf-8'))
r, s = payload['report'], payload['summary']
source = Path('content/reports') / f'{r["id"]}.{r["version"]}.json'
lock = Path('content/reports/releases') / f'{r["id"]}.{r["version"]}.json'
if json.loads(source.read_text(encoding='utf-8')) != r or r['publication']['status'] != 'review' or lock.exists():
    raise ValueError('Stale input or already frozen edition')
preview_assets = Path('public/report-assets/previews') / r['sector'] / r['month'] / r['version']
published_assets = Path('public/report-assets/published') / r['sector'] / r['month'] / r['version']
paths = [source] + sorted(p for p in preview_assets.rglob('*') if p.is_file())
hashes = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
verified = json.loads(Path('work/reports/asset-sha256.json').read_text(encoding='utf-8'))
if len(paths) < 2 or any(verified.get(path) != value for path, value in hashes.items()):
    raise ValueError('Run verify-assets.py for these exact files first')
f = next(p for p in s['rows'] if p['domain'] == payload['featuredDomain'])
review = f'''# {r['id']} {r['version']} 发布前复核

页面与素材已准备；以下五项由第二位复核者确认。当前记录不表示已批准或已上线。

- [ ] 可比样本 {s['count']}；上月 {s['previous']:,} 次，本月 {s['current']:,} 次，环比 {s['growth']*100:+.4f}%。
- [ ] 增长 {s['growing']}、下降 {s['declining']}、持平 {s['unchanged']}；增长占比 {s['breadth']*100:.4f}%。
- [ ] 域名等权增速中位数 {s['medianGrowth']*100:+.4f}%。
- [ ] 头部三家份额 {s['top3Share']*100:.4f}%，份额变化 {s['top3ShareChangePp']:+.4f} pp；固定本月前三家访问量变化 {s['currentTop3Growth']*100:+.4f}%。
- [ ] 重点样本 {f['domain']}：{f['previous']:,} → {f['current']:,}，增减 {f['delta']:+,}，环比 {f['growth']*100:+.4f}%，贡献 {f['contributionPp']:+.4f} pp。

公开口径：第三方估计，全球 Web 整站，可比域名，包含综合平台，仅反映样本。复核 coverage.json 的候选范围、排除原因及未覆盖域名。中英文素材均需保留 Sigpik 署名、月份、口径及固定链接。
'''
review_path = Path('work/reports') / (r['id']+'.release-review.md')
review_path.write_text(review, encoding='utf-8')
if not args.apply:
    print(f'Prepared review: {review_path}; {len(paths)} files reconciled. No publication state changed.')
    raise SystemExit(0)
if not args.reviewed_by or not args.reviewed_by.strip(): raise ValueError('Record the human reviewer after explicit approval')
published = date.fromisoformat(args.published_on)
if published < date.fromisoformat(r['preparedOn']) or published > date.today(): raise ValueError('Invalid publication date')
if published_assets.exists(): raise ValueError('Published asset path already exists; never overwrite it')
seo = Path('config/seo.ts')
source_before, seo_before = source.read_bytes(), seo.read_bytes()
r['editorial'] = {locale: {key: payload['locales'][locale][key] for key in ['headline','deck','titles','findings','shareText']} for locale in ['en','zh-CN']}
r['publication'] = {'status': 'published', 'publishedOn': published.isoformat()}
changes = {source: (json.dumps(r, ensure_ascii=False, indent=2)+'\n').encode('utf-8')}
renderer = Path(__file__).resolve().with_name('render-assets.py')
stage_parent = Path('work/reports').resolve()
with tempfile.TemporaryDirectory(prefix='publication-render-', dir=stage_parent) as temporary:
    stage = Path(temporary).resolve()
    assert stage.is_relative_to(stage_parent)
    (stage/source).parent.mkdir(parents=True, exist_ok=True)
    (stage/source).write_bytes(changes[source])
    render_input = stage/'render-input.json'
    render_input.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    for locale in ['en','zh-cn']:
        output = stage/published_assets/locale
        output.mkdir(parents=True, exist_ok=True)
        for name in ['summary.csv','cohort.csv']:
            reader = csv.DictReader(io.StringIO((preview_assets/locale/name).read_text(encoding='utf-8-sig')))
            rows = list(reader)
            for row in rows:
                row['publication_status'] = 'published'
                row['published_on'] = published.isoformat()
            with (output/name).open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=reader.fieldnames, quoting=csv.QUOTE_ALL)
                writer.writeheader()
                writer.writerows(rows)
        coverage = json.loads((preview_assets/locale/'coverage.json').read_text(encoding='utf-8'))
        coverage['publication'] = r['publication']
        (output/'coverage.json').write_text(json.dumps(coverage, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    subprocess.run([sys.executable, str(renderer), '--input', str(render_input), '--unlocked-publication'], cwd=stage, check=True)
    for path in sorted((stage/published_assets).rglob('*')):
        if path.is_file(): changes[path.relative_to(stage)] = path.read_bytes()
seo_text = seo_before.decode('utf-8')
sector_page = {'music-generation': 'musicResearch', 'video-generation': 'videoResearch', 'image-generation': 'imageResearch'}[r['sector']]
for key in [payload['seoPageId'], sector_page, 'reports']:
    pattern = re.compile(r'(^  '+re.escape(key)+r': \{\r?\n)(.*?)(\r?\n  \},)', re.M|re.S)
    def enable(match):
        return match[1]+match[2].replace('index: false','index: true').replace('sitemap: false','sitemap: true').replace('/report-assets/previews/','/report-assets/published/')+match[3]
    seo_text, count = pattern.subn(enable, seo_text, count=1)
    if count != 1: raise ValueError('SEO entry not found: '+key)
changes[seo] = seo_text.encode('utf-8')
release_hashes = {p.as_posix(): hashlib.sha256(body).hexdigest() for p, body in changes.items() if p != seo}
release = {'reportId': r['id'], 'version': r['version'], 'publishedOn': published.isoformat(), 'reviewedBy': args.reviewed_by.strip(), 'sha256': release_hashes}
changes[lock] = (json.dumps(release, ensure_ascii=False, indent=2)+'\n').encode('utf-8')
if source.read_bytes() != source_before or seo.read_bytes() != seo_before or lock.exists() or published_assets.exists():
    raise ValueError('Files changed during publication; retry after review')
if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != value for path, value in hashes.items()):
    raise ValueError('Preview files changed during publication')
originals = {p: p.read_bytes() if p.exists() else None for p in changes}
installed = []
try:
    for path, body in changes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        installed.append(path)
        path.write_bytes(body)
except Exception:
    for path in reversed(installed):
        if originals[path] is None: path.unlink(missing_ok=True)
        else: path.write_bytes(originals[path])
    raise
print(f'Locally frozen {r["id"]} at {published_assets}; preview assets preserved. Deployment is a separate step.')
