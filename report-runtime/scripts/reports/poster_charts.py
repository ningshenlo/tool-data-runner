"""Shareable editorial posters, rendered from the frozen report's exact values.

Layout revision is independent of the data edition. Published files are protected
by render-assets.py; changing this generator invalidates prepared release reviews.
"""
import calendar

from editorial_charts import Canvas, EditorialCharts

INK, MUTED, GRID = '#18181b', '#66666e', '#dedee2'
GREEN, RED = '#12734b', '#c43d47'
TYPE = dict(brand=34, header=28, title=64, body=30, value=44, axis=26,
            insight=38, note=26, footer=24, name=30, domain=23,
            hero=168, stat=64, kicker=25, leader=52)
PORTRAIT_TYPE = {**TYPE, 'title': 56, 'hero': 136, 'stat': 58,
                 'value': 40, 'footer': 20, 'name': 28, 'leader': 44}
SOCIAL_TYPE = {**TYPE, 'brand': 28, 'header': 23, 'title': 40, 'hero': 104,
               'body': 26, 'value': 34, 'note': 22, 'footer': 18}


def tone(value):
    return GREEN if value > 0 else RED if value < 0 else MUTED


class PosterCanvas(Canvas):
    def __init__(self, width, height, font_path, typography):
        super().__init__(width, height, font_path, typography=typography, max_pixels=168)

    def text(self, x, y, value, role='body', color=INK, bold=False,
             align='left', region=None, backdrop=False):
        return super().text(x, y, value, role, color, bold, align, region, backdrop)


class EditorialPosters(EditorialCharts):
    """Each shape has its own composition; no stretched or cropped chart exports."""

    def insight(self, zh):
        s = self.summary
        if s['growth'] > 0 and s['declining'] > s['count'] / 2:
            return ('总量增长，\n多数样本仍在下降' if zh else
                    'Visits grew.\nMost domains did not.')
        if s['growth'] < 0 and s['growing'] > s['count'] / 2:
            return ('总量下降，\n多数样本仍在增长' if zh else
                    'Visits fell.\nMost domains still grew.')
        return ('访问量变化，\n看总量，也看分布' if zh else
                'The total changed.\nSo did the mix.')

    def base(self, portrait, zh, number, category):
        w, h = (1080, 1350) if portrait else (1920, 1080)
        c = PosterCanvas(w, h, self.font_path, PORTRAIT_TYPE if portrait else TYPE)
        m = 64 if portrait else 96
        r, s = self.report, self.summary
        copy = self.payload['locales']['zh-CN' if zh else 'en']
        c.rect(0, 0, w, 8, INK)
        c.brand(m, 44, zh, compact=True)
        c.text(w - m, 53, copy['name'] + ' · ' + r['month'].replace('-', '.'), 'header', align='right')
        c.line(m, 123, w - m, 123, GRID)
        c.text(m, 152, ('市场观测 / ' if zh else 'MARKET OBSERVATIONS / ') + category, 'kicker', MUTED)
        c.text(w - m, 149, f'{number:02d}', 'kicker', MUTED, align='right')
        footer_y = 1163 if portrait else 929
        c.line(m, footer_y, w - m, footer_y, INK, 1.5)
        status = (('已发布 ' if zh else 'Published ') + r['publication']['publishedOn']) if r['publication']['status'] == 'published' else ('发布前预览' if zh else 'Publication preview')
        period = r['month'] + ' vs ' + r['baselineMonth']
        c.text(m, footer_y + 22, period + (f' · {s["count"]} 个可比域名' if zh else f' · {s["count"]} comparable domains'), 'footer', MUTED)
        c.text(w - m, footer_y + 22, r['version'] + ' · ' + status, 'footer', MUTED, align='right')
        c.text(m, footer_y + 57,
               '全球 Web 整站估计访问量；含综合平台，仅反映样本。' if zh else
               'Estimated whole-site Web visits; includes multipurpose platforms; sample only.', 'footer', MUTED)
        c.text(m, footer_y + 92, copy['url'].removeprefix('https://'), 'footer', MUTED)
        c.text(w - m, footer_y + 92, '数据整理与分析：Sigpik' if zh else 'Analysis: Sigpik', 'footer', INK, align='right')
        return c, m

    def metric(self, c, x, y, value, label, role='hero'):
        c.text(x, y, value, role, tone(self.summary['growth']), bold=True)
        c.text(x, y + c.typography[role] * 1.2 + 6, label, 'body', MUTED)

    def overview(self, path, portrait, zh):
        s = self.summary
        c, m = self.base(portrait, zh, 1, '增长的两面' if zh else 'THE GROWTH PICTURE')
        c.text(m, 213, self.insight(zh), 'title', bold=True)
        metric_y = 379 if portrait else 409
        self.metric(c, m, metric_y, f'{s["growth"] * 100:+.1f}%',
                    '样本总访问量 · 环比' if zh else 'Total sample visits · month over month')
        if portrait:
            c.runs(m, 591, [(f'{s["declining"]} / {s["count"]} ', tone(-s['declining']), True),
                           ('个域名访问量下降' if zh else 'domains lost visits', INK, False)], 'body')
            left, right, first, step = m, c.width - m, 698, 143
            c.line(m, 655, right, 655, GRID)
        else:
            c.line(881, 280, 881, 849, GRID)
            c.text(m, 704, f'{s["declining"]} / {s["count"]}', 'stat', tone(-s['declining']), True)
            c.text(m, 793, '个域名访问量下降' if zh else 'domains recorded fewer visits', 'body', MUTED)
            left, right, first, step = 966, c.width - m, 337, 163
            c.text(left, 263, '访问增量如何抵消减量' if zh else 'How gains offset losses', 'insight', bold=True)
        rows = self.volume_rows(zh)
        maximum = max(1, *(abs(row[2]) for row in rows))
        for i, (kind, _, value, _) in enumerate(rows):
            y = first + i * step
            label = ({'gains': f'增长域名 · {s["growing"]} 个', 'losses': f'下降域名 · {s["declining"]} 个', 'net': '相抵后的净变化'} if zh else
                     {'gains': f'Gains · {s["growing"]} domains', 'losses': f'Losses · {s["declining"]} domains', 'net': 'Net change'})[kind]
            c.text(left, y, label, 'body')
            c.text(right, y - 8, self.volume_label(value, zh), 'value', tone(value), True, align='right')
            width = abs(value) / maximum * (right - left)
            c.line(left, y + 71, right, y + 71, GRID, 2)
            c.rect(left, y + 59, width, 24, tone(value))
            c.geometry.append(dict(kind='change-volume-bar', metric=kind, value=value, width=width, pixelsPerVisit=(right-left)/maximum))
        c.text(left, 1120 if portrait else 851,
               '柱长按访问体量绘制 · 正负号表示增减方向' if zh else 'One scale for visit volumes · signs show direction', 'note', MUTED)
        c.save(path, self.audit_root)

    def visit_bars(self, c, zh, left, right, top, baseline):
        history = self.report['history']
        maximum = max(1, *(p['visits'] for p in history)) * 1.25
        unit = 1e4 if zh else 1e6
        scale = (baseline - top) / maximum
        for fraction in (0, .5, 1):
            y = baseline - (baseline - top) * fraction
            c.line(left, y, right, y, GRID, 1.4, dashed=fraction > 0)
        step = (right - left) / len(history)
        for i, point in enumerate(history):
            center = left + step * (i + .5)
            height = point['visits'] * scale
            yy = baseline - height
            c.rect(center - step * .28, yy, step * .56, height, INK if i == len(history)-1 else '#c2c2c9')
            label = f'{point["visits"]/unit:,.1f}万' if zh else f'{point["visits"]/unit:,.2f}M'
            c.text(center, yy - 53, label, 'body', INK, i == len(history)-1, 'center', backdrop=True)
            month = int(point['month'][-2:])
            c.text(center, baseline + 22, f'{month}月' if zh else calendar.month_abbr[month], 'axis', MUTED, align='center')
            c.geometry.append(dict(kind='visit-bar', month=point['month'], value=point['visits'], zero=baseline,
                                   top=yy, height=height, pixelsPerVisit=scale))
        c.text(left - 17, baseline - 15, '0', 'axis', MUTED, align='right')

    def breadth(self, c, zh, x, y, width, portrait):
        s = self.summary
        values = [('growing', GREEN, '增长' if zh else 'grew'),
                  ('declining', RED, '下降' if zh else 'declined'),
                  ('unchanged', MUTED, '持平' if zh else 'unchanged')]
        left = x
        for key, color, _ in values:
            size = width * s[key] / s['count']
            c.rect(left, y, size, 12, color)
            c.geometry.append(dict(kind='breadth-segment', key=key, count=s[key], width=size))
            left += size
        for index, (key, color, label) in enumerate(values):
            c.runs(x + index * width / 3, y + 32,
                   [(f'{s[key]} ', color, True), (label, INK, False)], 'body')

    def render_trend(self, path, portrait, zh, detailed=True):
        s, r = self.summary, self.report
        c, m = self.base(portrait, zh, 2, '同一组样本' if zh else 'THE SAME COHORT')
        c.text(m, 213, ('总量的趋势，\n不等于每个产品的处境' if zh else
                       'One total.\nDifferent trajectories.'), 'title', bold=True)
        if portrait:
            c.text(m, 382, f'{s["growth"]*100:+.1f}%', 'stat', tone(s['growth']), True)
            c.text(m + 330, 398, '样本总访问量 · 环比' if zh else 'Sample visits · MoM', 'body', MUTED)
            left, right, top, baseline = 104, c.width-m, 537, 866
        else:
            self.metric(c, m, 425, f'{s["growth"]*100:+.1f}%',
                        '样本总访问量 · 环比' if zh else 'Total sample visits · MoM')
            c.text(m, 715, f'{len(r["history"])} ' + ('个月' if zh else 'months'), 'stat', bold=True)
            c.text(m, 800, f'固定 {s["count"]} 个可比域名' if zh else f'The same {s["count"]} comparable domains', 'body', MUTED)
            left, right, top, baseline = 970, c.width-m, 366, 694
            c.text(left, 277, '样本估计访问量' if zh else 'Estimated sample visits', 'insight', bold=True)
        self.visit_bars(c, zh, left, right, top, baseline)
        self.breadth(c, zh, m if portrait else left, 991 if portrait else 792,
                     c.width-m*2 if portrait else right-left, portrait)
        days = '/'.join(str(calendar.monthrange(*map(int, p['month'].split('-')))[1]) for p in r['history'])
        c.text(m if portrait else left, 1102 if portrait else 875,
               f'各月 {days} 天 · 月总量，未按日均归一' if zh else
               f'{days} days · monthly totals, not daily averages', 'note', MUTED)
        c.save(path, self.audit_root)

    def contribution(self, path, portrait, zh):
        s = self.summary
        c, m = self.base(portrait, zh, 3, '增长贡献' if zh else 'THE CONTRIBUTORS')
        c.text(m, 213, ('谁在拉动样本访问量？' if zh else 'Who moved the total?'), 'title', bold=True)
        c.text(m, 308, '各取正、负贡献前三 · 对整体环比的百分点贡献（pp）' if zh else
               'Top three in each direction · contribution to sample growth (pp)', 'note', MUTED)
        rows = s['positive'] + s['negative']
        if not rows:
            c.text(m, 490, '本期样本均持平' if zh else 'All sample domains were unchanged.', 'insight')
            c.save(path, self.audit_root)
            return
        left, right = (464, 838) if portrait else (654, 1638)
        first, step = (425, 113) if portrait else (419, 74)
        low = min(0, *(p['contributionPp'] for p in rows))
        high = max(0, *(p['contributionPp'] for p in rows))
        span = max(high-low, .01)
        low -= span * .05
        high += span * .03
        x = lambda value: left + (value-low) / (high-low) * (right-left)
        bottom = first + step * (len(rows)-1)
        c.line(x(0), first-27, x(0), bottom+45, '#aaaab3', 1.5)
        copy = self.payload['locales']['zh-CN' if zh else 'en']
        for i, p in enumerate(rows):
            y, value = first+i*step, p['contributionPp']
            name = copy['productNames'][p['domain']]
            region = (m, y-26, left-40, y+step-25)
            height = c.wrapped(m, y-22, name, left-m-40, 'name', max_lines=2, region=region)
            if name != p['domain']:
                c.text(m, y-19+height, p['domain'], 'domain', MUTED, region=region)
            c.rect(min(x(0), x(value)), y-7, abs(x(value)-x(0)), 27, tone(value))
            c.text(c.width-m, y-21, f'{value:+.2f}', 'value', tone(value), True, align='right')
            c.geometry.append(dict(kind='contribution-bar', domain=p['domain'], value=value,
                                   zero=x(0), end=x(value), pixelsPerPp=(right-left)/(high-low)))
        c.text(x(0), bottom+49, '0', 'axis', MUTED, align='center')
        note_y = 1120 if portrait else 867
        c.text(m, note_y, '按访问增减量排序，非百分比增速榜 · 柱长共用同一比例尺' if zh else
               'Ranked by visit change, not growth rate · one shared linear scale', 'note', MUTED)
        c.save(path, self.audit_root)

    def social(self, path, zh):
        c = PosterCanvas(1200, 630, self.font_path, SOCIAL_TYPE)
        r, s = self.report, self.summary
        copy = self.payload['locales']['zh-CN' if zh else 'en']
        c.rect(0, 0, 1200, 8, INK)
        # Essential copy stays inside the existing 800 x 500 center-crop region.
        c.brand(215, 71, zh, compact=True)
        c.text(985, 84, copy['name']+' · '+r['month'].replace('-', '.'), 'header', align='right')
        c.line(215, 136, 985, 136, GRID)
        c.text(215, 167, self.insight(zh), 'title', bold=True)
        c.text(215, 281, f'{s["growth"]*100:+.1f}%', 'hero', tone(s['growth']), True)
        c.text(215, 414, '样本总访问量 · 环比' if zh else 'Total sample visits · MoM', 'body', MUTED)
        c.line(659, 286, 659, 454, GRID)
        c.text(707, 313, f'{s["declining"]} / {s["count"]}', 'value', tone(-s['declining']), True)
        c.text(707, 370, '个域名' if zh else 'domains', 'body', MUTED)
        c.text(707, 410, '访问量下降' if zh else 'lost visits', 'body', MUTED)
        c.line(215, 480, 985, 480, INK)
        status = ('已发布' if zh else 'Published') if r['publication']['status'] == 'published' else ('发布前预览' if zh else 'Publication preview')
        c.text(985, 453, r['version']+' · '+status, 'footer', MUTED, align='right')
        c.text(215, 498, '全球 Web 整站估计访问量 · 含综合平台，仅反映样本' if zh else
               'Whole-site Web estimates · includes multipurpose platforms; sample only', 'footer', MUTED)
        c.text(215, 532, copy['url'].removeprefix('https://'), 'footer', MUTED)
        c.texts = [(item, role, size, (200, 65, 1000, 565)) for item, role, size, _ in c.texts]
        c.save(path, self.audit_root)
