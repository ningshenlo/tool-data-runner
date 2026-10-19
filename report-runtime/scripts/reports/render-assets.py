"""Package shared web-SVG artwork and data, without modifying published editions."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import zipfile
parser = argparse.ArgumentParser()
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--python-deps') # Accepted for compatibility with older runners.
parser.add_argument('--font', type=Path, default=Path(os.environ.get('REPORT_FONT', 'C:/Windows/Fonts/msyh.ttc')))
parser.add_argument('--unlocked-publication', action='store_true')
args = parser.parse_args()
payload = json.loads(args.input.read_text(encoding='utf-8'))
r, s = payload['report'], payload['summary']
source = json.loads((Path('content/reports') / f'{r["id"]}.{r["version"]}.json').read_text(encoding='utf-8'))
if source != r or r['publication']['status'] != 'review' or (Path('content/reports/releases') / f'{r["id"]}.{r["version"]}.json').exists():
    raise ValueError('Stale input or frozen edition; create a correction version.')
channel = 'previews'
root = Path('public/report-assets') / channel / r['sector'] / r['month'] / r['version']
subprocess.run([os.environ.get('REPORT_NODE', 'node'), str(Path(__file__).with_name('render-web-assets.mjs')), str(args.input.resolve()), str(args.font)], check=True)

def publication_label(zh):
    if r['publication']['status'] == 'published':
        return ('已发布 ' if zh else 'Published ') + r['publication']['publishedOn']
    return '发布前预览 · 待复核' if zh else 'Publication preview · Pending review'

for locale in ['en','zh-CN']:
    zh = locale == 'zh-CN'; copy = payload['locales'][locale]
    directory = root / ('zh-cn' if zh else 'en'); directory.mkdir(parents=True,exist_ok=True)
    attribution = '数据整理与分析：Sigpik 市场观测' if zh else 'Data compilation and analysis: Sigpik Market Observations'
    citation = '\n\n'.join(copy['findings'])+'\n\n'+attribution+'\n'+copy['scope']+'\n'+copy['url']+f'\n{r["month"]} vs {r["baselineMonth"]} · {r["version"]} · '+publication_label(zh)+'\n'
    (directory/'citation.txt').write_text(citation,encoding='utf-8')
    readme = (f'Sigpik {copy["name"]}月度观察\n\n素材：3 类图表，每类 16:9 横版（1920×1080）及 4:5 竖版（1080×1350）。\nsummary.csv 为 UTF-8 BOM，保留未四舍五入数值。percent 为百分数，percentage_points 为百分点。\n引用请保留署名、统计月份、样本范围及报告链接。\n当前素材与页面同为发布前预览。\n' if zh else f'Sigpik {copy["name"]} monthly observations\n\nAssets: 3 charts in 16:9 landscape (1920×1080) and 4:5 portrait (1080×1350).\nsummary.csv uses UTF-8 BOM and unrounded values; percent and percentage_points are distinct units.\nRetain attribution, reporting period, sample scope and report URL when citing.\nThese assets and the page are publication previews.\n')
    readme += '\n' + ('cohort.csv 包含全部可比域名的两期访问量、增减、增长率与贡献；coverage.json 包含样本覆盖和排除原因。\n' if zh else 'cohort.csv contains every comparable domain, both visit counts, change, growth and contribution; coverage.json describes sample coverage and exclusions.\n')
    if copy.get('directionMethod'):
        readme += '\n' + copy['directionMethod'] + '\n'
        readme += ('growing_visit_increase 与 declining_visit_decrease 均以非负数记录完整样本的增量和减量；净变化 = 增量 − 减量，单位为估计访问次数。\n' if zh else 'growing_visit_increase and declining_visit_decrease are nonnegative magnitudes across the full cohort; net change = gains minus losses, in estimated visits.\n')
    if copy.get('reuseText'):
        readme += '\n' + copy['reuseText'] + '\n'
    if r['publication']['status'] == 'published':
        readme = readme.replace('当前素材与页面同为发布前预览。', publication_label(zh)+'。本期已固定；更正需另建版本。').replace('These assets and the page are publication previews.',publication_label(zh)+'. This edition is fixed; corrections require a new version.')
    (directory/'README.txt').write_text(readme+'\n'+attribution+'\n'+copy['scope']+'\n'+copy['url']+f'\n{r["month"]} · {r["version"]}\n',encoding='utf-8')
    members = [f'{chart}-{shape}.png' for chart in ['overview','trend','contribution'] for shape in ['landscape','portrait']]+['summary.csv','cohort.csv','coverage.json','citation.txt','README.txt']
    with zipfile.ZipFile(directory/f'sigpik-report-{r["sector"]}-{r["month"]}-{r["version"]}.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        date = tuple(map(int,r['preparedOn'].split('-')))+(0,0,0)
        for name in members:
            info = zipfile.ZipInfo(name,date); info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,(directory/name).read_bytes())
    print(f'{r["id"]} {locale}: 7 PNG, 5 text/data files, 11-member ZIP ({channel})')
