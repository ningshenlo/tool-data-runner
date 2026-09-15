"""Render a fixed edition and its eleven-member media ZIP; never rewrite published files."""
import argparse
import calendar
import json
import os
from pathlib import Path
import sys
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--python-deps', type=Path, default=os.environ.get('REPORT_PYTHON_DEPS'))
parser.add_argument('--font', type=Path, default=Path(os.environ.get('REPORT_FONT', 'C:/Windows/Fonts/msyh.ttc')))
parser.add_argument('--unlocked-publication', action='store_true')
args = parser.parse_args()
if args.python_deps:
    sys.path.insert(0, str(args.python_deps.resolve()))
os.environ.setdefault('MPLCONFIGDIR', str(args.input.resolve().parent / '.matplotlib'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator
from editorial_charts import EditorialCharts, uses_editorial_template

payload = json.loads(args.input.read_text(encoding='utf-8'))
r, s = payload['report'], payload['summary']
source_path = Path('content/reports') / f'{r["id"]}.{r["version"]}.json'
source = json.loads(source_path.read_text(encoding='utf-8'))
if source != r or (r['publication']['status'] != 'review' and not args.unlocked_publication) or (Path('content/reports/releases') / f'{r["id"]}.{r["version"]}.json').exists():
    raise ValueError('Stale render input or published edition. Create a new correction version.')
featured = next(p for p in s['rows'] if p['domain'] == payload['featuredDomain'])
channel = 'published' if r['publication']['status'] == 'published' else 'previews'
root = Path('public/report-assets') / channel / r['sector'] / r['month'] / r['version']
editorial = EditorialCharts(payload, args.font, args.input.resolve().parent / 'layout-checks' / r['id']) if uses_editorial_template(r) else None
font = FontProperties(fname=str(args.font))
BG, INK, MUTED, GRID, GREEN, RED = '#f9fafb', '#192027', '#566170', '#dce1e6', '#14734b', '#b43c42'
plt.rcParams.update({'axes.unicode_minus': False, 'savefig.facecolor': BG})

def color(value):
    return GREEN if value > 0 else RED if value < 0 else MUTED

def text(fig, x, y, value, size=22, foreground=INK, weight='normal', ha='left', max_width=None):
    item = fig.text(x, y, value, fontproperties=font, fontsize=size, color=foreground, weight=weight, ha=ha, va='top', linespacing=1.5)
    if max_width:
        fig.canvas.draw()
        width = item.get_window_extent(fig.canvas.get_renderer()).width
        if width > max_width:
            item.set_fontsize(size * max_width / width)
    return item

def base(w, h, title, zh):
    fig = plt.figure(figsize=(w/100, h/100), dpi=100, facecolor=BG)
    factor = 1 if w == 1080 else 1.4
    text(fig, .065, .952, 'SIGPIK / ' + ('市场观测' if zh else 'MARKET OBSERVATIONS'), 17*factor)
    text(fig, .935, .951, f'{r["month"]} / {r["version"]}', 15*factor, MUTED, ha='right')
    text(fig, .065, .917, publication_label(zh), 13*factor, MUTED)
    text(fig, .065, .871, title, 34*factor, weight='bold', max_width=w*.87)
    text(fig, .065, .803, f'{copy["scopeName"]} · ' + ('全球 Web' if zh else 'Worldwide Web'), 15*factor, MUTED)
    fig.add_artist(Line2D([.065, .935], [.124, .124], color=GRID, linewidth=1))
    footer = (f'数据整理与分析：Sigpik 市场观测\n{r["month"]} 对比 {r["baselineMonth"]} · {s["count"]} 个可比域名 · 第三方整站访问估计\n包含综合平台；仅反映样本，非行业总规模、用户数或收入。' if zh else f'Data compilation and analysis: Sigpik Market Observations\n{r["month"]} vs {r["baselineMonth"]} · {s["count"]} comparable domains · Third-party whole-site estimates\nIncludes multipurpose platforms; sample observations, not industry totals, users or revenue.')
    text(fig, .065, .106, footer+'\n'+copy['url'], 12 if w == 1080 else 14, MUTED)
    return fig

def save(fig, path):
    fig.savefig(path, dpi=100, metadata={'Software': 'Sigpik Market Observations'})
    plt.close(fig)

def overview(path, portrait, zh):
    w, h = (1080,1350) if portrait else (1920,1080)
    fig = base(w, h, '三项数字，观察样本变化' if zh else 'Three measures of sample change', zh)
    values = [
        (f'{s["growth"]*100:+.1f}%', '样本总访问量环比' if zh else 'Total sample visits, MoM', f'{s["declining"]} 个下降 / {s["growing"]} 个增长' if zh else f'{s["declining"]} declined / {s["growing"]} grew', color(s['growth'])),
        (f'{featured["growth"]*100:+.1f}%', f'{copy["featuredName"]} ' + ('访问量环比' if zh else 'visits, MoM'), f'访问增减量 {featured["delta"]/10000:+.1f} 万次' if zh else f'{featured["delta"]/1e6:+.2f} million visits', color(featured['growth'])),
        (f'{s["top3Share"]*100:.1f}%', '头部三家样本内份额' if zh else 'Top-three sample share', f'份额变化 {s["top3ShareChangePp"]:+.2f} pp' if zh else f'Share change {s["top3ShareChangePp"]:+.2f} pp', INK),
    ]
    for i, (value, label, detail, foreground) in enumerate(values):
        xx, yy = (.065, [.737,.536,.335][i]) if portrait else ([.065,.37,.675][i],.64)
        text(fig, xx, yy, value, 59 if portrait else 76, foreground, 'bold')
        text(fig, xx, yy-(.077 if portrait else .165), label, 23 if portrait else 26, max_width=w*(.87 if portrait else .27))
        text(fig, xx, yy-(.12 if portrait else .229), detail, 17 if portrait else 20, MUTED)
    if not portrait:
        text(fig, .065, .255, f'固定本月前三家比较：合计访问量环比 {s["currentTop3Growth"]*100:+.1f}%。' if zh else f'With current top-three membership held fixed, combined visits changed {s["currentTop3Growth"]*100:+.1f}%.', 23)
    save(fig,path)

def trend(path, portrait, zh):
    w, h = (1080,1350) if portrait else (1920,1080)
    fig = base(w, h, '访问趋势与样本涨跌分布' if zh else 'Visit trends and sample breadth', zh)
    ax = fig.add_axes([.12,.40 if portrait else .415,.76,.33 if portrait else .285],facecolor=BG)
    vals = [p['visits']/1e6 for p in r['history']]
    n = len(vals); span = max(max(vals)-min(vals),max(vals)*.06,.001)
    ax.plot(range(n),vals,color=INK,marker='o',lw=3,markersize=9)
    ax.set_ylim(max(0,min(vals)-span*.35),max(vals)+span*.5); ax.set_xlim(-.15,n-1+.15)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
    ax.set_xticks(range(n),[p['month'] for p in r['history']])
    ax.tick_params(axis='both',labelsize=17 if portrait else 20,colors=MUTED,length=0,pad=12)
    ax.grid(axis='y',color=GRID); ax.set_axisbelow(True)
    for spine in ax.spines.values(): spine.set_visible(False)
    for i,value in enumerate(vals): ax.annotate(f'{value:.2f}M',(i,value),xytext=(0,15),textcoords='offset points',ha='center',fontsize=19 if portrait else 24,color=INK)
    text(fig,.12,.757,'单位：百万次访问 · 纵轴非零起点' if zh else 'Million visits · Nonzero vertical baseline',15 if portrait else 19,MUTED)
    bar = fig.add_axes([.12,.255,.76,.045],facecolor=BG)
    left = 0
    for key, foreground in [('growing',GREEN),('declining',RED),('unchanged',MUTED)]:
        bar.barh([0],[s[key]],left=left,color=foreground,height=.8); left += s[key]
    bar.set_xlim(0,s['count']); bar.axis('off')
    text(fig,.12,.347,f'增长 {s["growing"]} / 下降 {s["declining"]} / 持平 {s["unchanged"]}' if zh else f'{s["growing"]} grew / {s["declining"]} declined / {s["unchanged"]} unchanged',22 if portrait else 26)
    days = [calendar.monthrange(*map(int,p['month'].split('-')))[1] for p in r['history']]
    note = f'同一组 {s["count"]} 个域名；各月天数依次为 '+ '/'.join(map(str,days))+'。' if zh else f'Same {s["count"]} domains; days per month: '+ '/'.join(map(str,days))+'.'
    text(fig,.12,.216,note,14 if portrait else 19,MUTED)
    save(fig,path)

def contribution(path, portrait, zh):
    w,h = (1080,1350) if portrait else (1920,1080)
    fig = base(w,h,'谁带来最多访问增减量' if zh else 'Where visits were gained and lost',zh)
    rows = s['positive'] + s['negative']; count = len(rows)
    ax = fig.add_axes([.38 if portrait else .29,.257,.46 if portrait else .56,.46],facecolor=BG)
    vals = [p['contributionPp'] for p in rows]; span = max(max(vals,default=0)-min(vals,default=0),.01)
    ax.barh(range(count),vals,height=.48,color=[color(p['delta']) for p in rows])
    ax.set_ylim(count-.35,-.65); ax.set_xlim(min(0,min(vals,default=0))-span*.18,max(0,max(vals,default=0))+span*.2)
    labels = [copy['productNames'][p['domain']]+'\n'+p['domain'] if copy['productNames'][p['domain']] != p['domain'] else p['domain'] for p in rows]
    ax.set_yticks(range(count),labels,fontproperties=font)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
    ax.tick_params(axis='both',labelsize=14 if portrait else 20,colors=MUTED,length=0,pad=12)
    ax.axvline(0,color=GRID,lw=1); ax.grid(axis='x',color=GRID); ax.set_axisbelow(True)
    for spine in ax.spines.values(): spine.set_visible(False)
    for i,p in enumerate(rows):
        inside = abs(p['contributionPp']) > span*.32
        right = (p['delta'] > 0) != inside
        ax.annotate(f'{p["contributionPp"]:+.2f}',(p['contributionPp'],i),xytext=(8 if right else -8,0),textcoords='offset points',ha='left' if right else 'right',va='center',fontsize=17 if portrait else 23,color='white' if inside else INK)
    text(fig,.065,.758,'各取正、负贡献前三 · 对整体环比的百分点贡献（pp）' if zh else 'Top three in each direction · Contribution to sample growth (pp)',15 if portrait else 22,MUTED)
    note = '按访问增减量排序，非百分比增速榜。' if zh else 'Ranked by absolute visit change, not percentage growth.'
    if any(p['domain']=='seedance.ai' for p in rows): note += '\n'+('seedance.ai 为域名样本，不据此认定官方模型归属。' if zh else 'Inclusion of seedance.ai does not establish official model affiliation.')
    text(fig,.065,.211,note,14 if portrait else 17,MUTED)
    save(fig,path)

def social(path,zh):
    fig = plt.figure(figsize=(12,6.3),dpi=100,facecolor=BG)
    text(fig,.19,.86,'SIGPIK / 市场观测' if zh else 'SIGPIK / MARKET OBSERVATIONS',17)
    text(fig,.19,.815,publication_label(zh),11,MUTED,max_width=755)
    text(fig,.19,.77,f'{copy["month"]} · {copy["name"]}',24,max_width=755)
    text(fig,.19,.65,f'{s["growth"]*100:+.1f}%',78,color(s['growth']),'bold')
    text(fig,.19,.405,f'{s["count"]} 个可比样本 · 估计 Web 访问量环比' if zh else f'{s["count"]} comparable domains · Estimated Web visits, MoM',17,max_width=755)
    text(fig,.19,.305,f'{copy["featuredName"]} {featured["growth"]*100:+.1f}%',27,color(featured['growth']),max_width=755)
    text(fig,.19,.18,'整站估计访问量；含综合平台，仅反映样本。' if zh else 'Whole-site estimates, including multipurpose platforms; sample only.',12,MUTED,max_width=755)
    fig.canvas.draw()
    for item in fig.texts:
        box = item.get_window_extent(fig.canvas.get_renderer())
        if not (200 <= box.x0 and box.x1 <= 1000 and 65 <= box.y0 and box.y1 <= 565): raise ValueError('Social-card safe rectangle exceeded: '+item.get_text())
    save(fig,path)

def publication_label(zh):
    if r['publication']['status'] == 'published':
        return ('已发布 ' if zh else 'Published ') + r['publication']['publishedOn']
    return '发布前预览 · 待复核' if zh else 'Publication preview · Pending review'

for locale in ['en','zh-CN']:
    zh = locale == 'zh-CN'; copy = payload['locales'][locale]
    directory = root / ('zh-cn' if zh else 'en'); directory.mkdir(parents=True,exist_ok=True)
    for name,render in [('overview',overview),('trend',trend),('contribution',contribution)]:
        for shape in ['landscape','portrait']:
            if editorial: editorial.render(name, directory/f'{name}-{shape}.png', shape=='portrait', zh)
            else: render(directory/f'{name}-{shape}.png',shape=='portrait',zh)
    if editorial: editorial.social(directory/'social-card.png', zh)
    else: social(directory/'social-card.png',zh)
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
