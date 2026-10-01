"""Versioned deterministic charts with measured pixel typography."""
import calendar
import json
import math
import re
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

BG, INK, MUTED, GRID = '#f9fafb', '#18211e', '#566170', '#d3dce0'
GREEN, PALE, RED = '#14734b', '#bdebcf', '#d9534f'
DPI = 100
# These are pixels, not Matplotlib points. Never size text to fill a box.
TYPE = dict(brand=28, header=26, title=40, body=30, value=32, axis=26,
            insight=32, note=26, footer=21, name=26, domain=20)
BRAND_MARK = Path(__file__).resolve().parents[2] / 'public/brand/sigpik-logo.png'


def uses_editorial_template(report):
    version = report.get('templateVersion', 'classic-v1')
    if version not in ('classic-v1', 'editorial-v1'):
        raise ValueError(f'Unsupported report template version: {version!r}')
    return version == 'editorial-v1'


def nice_limit(value):
    """Round up to a legible linear scale, leaving room above direct labels."""
    target = max(value * 1.14, .001)
    magnitude = 10 ** math.floor(math.log10(target))
    return next(step * magnitude for step in (1, 1.5, 2, 2.5, 5, 10) if step * magnitude >= target)


class Canvas:
    def __init__(self, width, height, font_path, scale=1):
        self.width, self.height, self.scale = width, height, scale
        self.fig = plt.figure(figsize=(width / DPI, height / DPI), dpi=DPI, facecolor=BG)
        self.font = FontProperties(fname=str(font_path))
        candidate = font_path.with_name(font_path.name.replace('-Regular', '-Bold') if '-Regular' in font_path.name else font_path.stem + 'bd' + font_path.suffix)
        self.bold_font = FontProperties(fname=str(candidate)) if candidate.exists() else self.font
        self.texts, self.geometry = [], []

    def font_for(self, pixels, bold=False):
        font = (self.bold_font if bold else self.font).copy()
        font.set_size(pixels * 72 / DPI)
        return font

    def measure(self, value, role, bold=False):
        font = self.font_for(TYPE[role] * self.scale, bold)
        return self.fig.canvas.get_renderer().get_text_width_height_descent(value, font, False)[0]

    def text(self, x, y, value, role='body', color=INK, bold=False, align='left', region=None, backdrop=False):
        pixels = TYPE[role] * self.scale
        if pixels > 54:
            raise ValueError('Typography exceeds the 54px ceiling')
        item = self.fig.text(x / self.width, 1 - y / self.height, str(value),
                             fontproperties=self.font_for(pixels, bold), color=color,
                             ha=align, va='top', linespacing=1.25)
        if backdrop:
            item.set_bbox(dict(facecolor=BG, edgecolor='none', pad=2))
        self.texts.append((item, role, pixels, region or (48, 32, self.width - 48, self.height - 32)))
        return item

    def runs(self, x, y, parts, role='body', region=None):
        for value, color, bold in parts:
            self.text(x, y, value, role, color, bold, region=region)
            x += self.measure(value, role, bold)

    def wrapped(self, x, y, value, width, role='name', max_lines=2, region=None):
        # Break at words; CJK labels may break at characters. Never silently shrink or clip.
        tokens = list(value) if re.search(r'[\u3400-\u9fff]', value) else re.findall(r'\S+\s*', value)
        lines, current = [], ''
        for token in tokens:
            if self.measure(token.rstrip(), role) > width:
                raise ValueError(f'Unbreakable label exceeds its column: {value}')
            proposed = current + token
            if current and self.measure(proposed.rstrip(), role) > width:
                lines.append(current.rstrip())
                current = token.lstrip()
            else:
                current = proposed
        if current:
            lines.append(current.rstrip())
        if len(lines) > max_lines:
            raise ValueError(f'Label exceeds {max_lines} lines: {value}')
        for index, line in enumerate(lines):
            self.text(x, y + index * TYPE[role] * self.scale * 1.25, line, role, region=region)
        return len(lines) * TYPE[role] * self.scale * 1.25

    def line(self, x1, y1, x2, y2, color=GRID, width=1, dashed=False):
        self.fig.add_artist(Line2D([x1 / self.width, x2 / self.width],
                                  [1 - y1 / self.height, 1 - y2 / self.height],
                                  transform=self.fig.transFigure, color=color,
                                  linewidth=width * 72 / DPI, linestyle=(0, (4, 5)) if dashed else '-', zorder=1))

    def rect(self, x, y, width, height, color):
        self.fig.add_artist(Rectangle((x / self.width, 1 - (y + height) / self.height),
                                     width / self.width, height / self.height,
                                     transform=self.fig.transFigure, facecolor=color, edgecolor='none', zorder=2))

    def brand(self, x, y, zh, compact=False):
        mark_size = 58 if compact else 70 * self.scale
        ax = self.fig.add_axes([(x - 12) / self.width, 1 - (y + mark_size - 9) / self.height,
                                mark_size / self.width, mark_size / self.height])
        ax.imshow(plt.imread(BRAND_MARK)); ax.axis('off')
        self.text(x + mark_size - 12, y + 5, 'sigpik', 'brand', bold=True)
        if not compact:
            self.text(x + mark_size + 94 * self.scale, y + 7,
                      '/  市场观测' if zh else '/  Market observations', 'header')

    def validate(self):
        self.fig.canvas.draw()
        renderer = self.fig.canvas.get_renderer()
        boxes = []
        for item, role, pixels, region in self.texts:
            box = item.get_window_extent(renderer)
            bounds = (box.x0, self.height - box.y1, box.x1, self.height - box.y0)
            left, top, right, bottom = region
            if bounds[0] < left - .5 or bounds[1] < top - .5 or bounds[2] > right + .5 or bounds[3] > bottom + .5:
                raise ValueError(f'Text outside its region: {item.get_text()!r}; {bounds} not in {region}')
            for previous in boxes:
                a = previous['bounds']
                if min(a[2], bounds[2]) - max(a[0], bounds[0]) > 1 and min(a[3], bounds[3]) - max(a[1], bounds[1]) > 1:
                    raise ValueError(f'Text collision: {previous["text"]!r} and {item.get_text()!r}')
            boxes.append(dict(text=item.get_text(), role=role, pixels=pixels, bounds=list(bounds)))
        return boxes

    def save(self, path, audit_root):
        try:
            boxes = self.validate()
            self.fig.savefig(path, dpi=DPI, facecolor=BG, metadata={'Software': 'Sigpik Market Observations'})
            audit_root.mkdir(parents=True, exist_ok=True)
            (audit_root / (path.parent.name + '-' + path.stem + '.json')).write_text(json.dumps({
                'width': self.width, 'height': self.height, 'dpi': DPI,
                'text': boxes, 'geometry': self.geometry, 'validation': 'passed',
            }, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            plt.close(self.fig)


class EditorialCharts:
    def __init__(self, payload, font_path, audit_root):
        self.report, self.summary = payload['report'], payload['summary']
        self.payload, self.font_path, self.audit_root = payload, font_path, audit_root

    def heading(self, zh, trend=False):
        s, r = self.summary, self.report
        if trend:
            months = len(r['history'])
            if months == 3:
                return '同一组样本，三个月的变化' if zh else 'One cohort, three months of change'
            return f'同一组样本，{months}个月的变化' if zh else f'One cohort, {months} months of change'
        return '样本访问量的增量、减量与净变化' if zh else 'Visit gains, losses and the net change'

    def base(self, portrait, zh, title, subtitle):
        w, h = (1080, 1350) if portrait else (1920, 1080)
        c = Canvas(w, h, self.font_path, 1 if portrait else 1.15)
        margin = 72 if portrait else 110
        r, copy = self.report, self.payload['locales']['zh-CN' if zh else 'en']
        c.brand(margin, 55, zh)
        c.text(w - margin, 62, copy['name'] + ' · ' + r['month'].replace('-', '.'), 'header', align='right')
        status = (('已发布 ' if zh else 'Published ') + r['publication']['publishedOn']) if r['publication']['status'] == 'published' else ('发布前预览' if zh else 'Publication preview')
        c.text(w - margin, 102, r['version'] + ' · ' + status, 'footer', MUTED, align='right')
        c.text(margin, 185 if portrait else 155, title, 'title', region=(margin, 150, w - margin, 246))
        if isinstance(subtitle, list):
            c.runs(margin, 255 if portrait else 230, subtitle, 'body', region=(margin, 220, w - margin, 310))
        else:
            c.text(margin, 255 if portrait else 230, subtitle, 'body', MUTED, region=(margin, 220, w - margin, 310))
        footer_y = 1200 if portrait else 942
        c.line(margin, footer_y, w - margin, footer_y)
        year, month = map(int, r['month'].split('-'))
        prior_month = int(r['baselineMonth'][-2:])
        period = f'{year}年{month}月 vs {prior_month}月' if zh else f'{calendar.month_abbr[month]} {year} vs {calendar.month_abbr[prior_month]}'
        footer = [period + (f' · {self.summary["count"]} 个样本 · 全球 Web 整站估计' if zh else f' · {self.summary["count"]} domains · Estimated whole-site Web visits'),
                  '包含综合平台；仅反映可比样本。数据整理与分析：Sigpik' if zh else 'Includes multipurpose platforms; comparable sample only. Analysis: Sigpik',
                  copy['url'].removeprefix('https://')]
        for i, line in enumerate(footer):
            c.text(margin, footer_y + 18 + i * 30, line, 'footer', MUTED)
        return c

    def volume_rows(self, zh):
        s = self.summary
        gain, loss = s['growingVisitIncrease'], s['decliningVisitDecrease']
        return [
            ('gains', f'{s["growing"]} 个增长域名 · 合计增量' if zh else f'{s["growing"]} growing domains · Visits gained', gain, GREEN if gain else MUTED),
            ('losses', f'{s["declining"]} 个下降域名 · 合计减量' if zh else f'{s["declining"]} declining domains · Visits lost', -loss, RED if loss else MUTED),
            ('net', '增量与减量相抵 · 净变化' if zh else 'Gains minus losses · Net change', s['delta'], GREEN if s['delta'] > 0 else RED if s['delta'] < 0 else MUTED),
        ]

    def volume_label(self, value, zh):
        sign = '+' if value > 0 else '−' if value < 0 else ''
        return f'{sign}{abs(value) / 1e4:,.1f}万' if zh else f'{sign}{abs(value) / 1e6:.2f}M'

    def overview(self, path, portrait, zh):
        s = self.summary
        subtitle = (f'{s["count"]} 个可比域名 · 样本访问量环比 {s["growth"] * 100:+.1f}%' if zh else f'{s["count"]} comparable domains · Visits {s["growth"] * 100:+.1f}% month over month')
        c = self.base(portrait, zh, self.heading(zh), subtitle)
        margin = 72 if portrait else 110
        rows = self.volume_rows(zh)
        maximum = max(1, *(abs(row[2]) for row in rows))
        width = c.width - margin * 2 - (190 if portrait else 260)
        first, step = (385, 210) if portrait else (355, 155)
        for index, (kind, label, value, tone) in enumerate(rows):
            yy = first + index * step
            c.text(margin, yy, label, 'body', INK)
            c.text(c.width - margin, yy, self.volume_label(value, zh), 'value', tone, True, align='right')
            length = abs(value) / maximum * width
            c.rect(margin, yy + 67, length, 26, tone)
            c.line(margin, yy + 60, margin, yy + 100, MUTED)
            c.geometry.append(dict(kind='change-volume-bar', metric=kind, value=value, width=length, pixelsPerVisit=width/maximum))
        divider = 1050 if portrait else 830
        c.line(margin, divider, c.width - margin, divider)
        c.text(margin, divider + 26, (f'减量占上月样本总访问量 {s["decliningVisitDecrease"] / s["previous"] * 100:.2f}%。' if zh else f'Losses equal {s["decliningVisitDecrease"] / s["previous"] * 100:.2f}% of prior-month sample visits.'), 'note')
        c.text(margin, divider + 76, '柱长按访问体量绘制；涨跌方向另用正负号标明。' if zh else 'Bar lengths show magnitudes; signs indicate direction.', 'note', MUTED)
        c.save(path, self.audit_root)

    def bars(self, c, portrait, zh, baseline, top, left, right, value_role='value', axis_role='axis', annotate=True):
        history = self.report['history']
        divisor = 1e4 if zh else 1e6
        values = [point['visits'] / divisor for point in history]
        limit = nice_limit(max(values))
        mantissa = limit / 10 ** math.floor(math.log10(limit))
        ticks = [0, limit / 2.5, limit * 2 / 2.5, limit] if mantissa == 2.5 else [0, limit / 2, limit]
        # A true zero baseline and a single linear map are shared by every bar and grid rule.
        y = lambda value: baseline - value / limit * (baseline - top)
        c.line(left, top, left, baseline, MUTED)
        for tick in ticks:
            yy = y(tick)
            if tick != limit:
                c.line(left, yy, right, yy, MUTED if tick == 0 else GRID, dashed=tick != 0)
            c.text(left - 18, yy - 15 * c.scale, f'{tick:,.0f}' if zh else f'{tick:g}', axis_role, MUTED, align='right')
        step = (right - left) / len(values)
        centers = [left + step * (i + .5) for i in range(len(values))]
        for index, (point, value, center) in enumerate(zip(history, values, centers)):
            bar_top = y(value)
            c.rect(center - step * .29, bar_top, step * .58, baseline - bar_top, GREEN if index == len(values) - 1 else PALE)
            label = f'{value:,.1f}万' if zh else f'{value:,.2f}M'
            c.text(center, bar_top - 45 * c.scale, label, value_role, align='center', backdrop=True)
            month = int(point['month'][-2:])
            c.text(center, baseline + 20 * c.scale, f'{month}月' if zh else calendar.month_abbr[month], axis_role, MUTED, align='center')
            c.geometry.append(dict(kind='visit-bar', month=point['month'], value=point['visits'], zero=baseline,
                                   top=bar_top, height=baseline - bar_top, pixelsPerVisit=(baseline - top) / (limit * divisor)))
        if annotate and len(values) >= 2:
            x1, x2 = centers[-2:]
            yy = min(y(values[-2]), y(values[-1])) - 80 * c.scale
            tone = GREEN if self.summary['growth'] >= 0 else RED
            c.line(x1, yy, x2, yy, tone, 1.5)
            c.line(x1, yy, x1, y(values[-2]) - 65 * c.scale, tone, 1.5)
            c.line(x2, yy, x2, y(values[-1]) - 58 * c.scale, tone, 1.5)
            c.text((x1 + x2) / 2, yy - 44 * c.scale, f'{self.summary["growth"] * 100:+.1f}%', value_role, tone, True, 'center')

    def render_trend(self, path, portrait, zh, detailed=False):
        s, r = self.summary, self.report
        month = int(r['month'][-2:])
        if zh:
            prefix = f'{month}月样本访问量环比' + ('增长 ' if s['growth'] >= 0 else '下降 ')
            value = f'{abs(s["growth"]) * 100:.1f}%'
        else:
            prefix = f'{calendar.month_name[month]} sample visits · '
            value = f'{s["growth"] * 100:+.1f}% month over month'
        subtitle = [(prefix, MUTED, False), (value, GREEN if s['growth'] >= 0 else RED, False)]
        if detailed:
            subtitle = f'固定 {s["count"]} 个可比域名 · 各月均使用相同样本' if zh else f'The same {s["count"]} comparable domains in every month'
        c = self.base(portrait, zh, self.heading(zh, detailed), subtitle)
        margin = 72 if portrait else 110
        c.text(margin, 350 if portrait else 305, '估计访问量 / 万次' if zh else 'Estimated visits / millions', 'axis', MUTED)
        baseline = (905 if detailed else 950) if portrait else (690 if detailed else 720)
        self.bars(c, portrait, zh, baseline, 425 if portrait else 370, 160 if portrait else 250, c.width - margin)
        divider = (1000 if detailed else 1050) if portrait else (785 if detailed else 800)
        c.line(margin, divider, c.width - margin, divider)
        if detailed:
            distribution = f'增长 {s["growing"]}  /  下降 {s["declining"]}  /  持平 {s["unchanged"]}' if zh else f'{s["growing"]} grew  /  {s["declining"]} declined  /  {s["unchanged"]} unchanged'
            c.text(margin, divider + 30, distribution, 'insight')
            xx = margin
            for key, tone in [('growing', GREEN), ('declining', RED), ('unchanged', MUTED)]:
                width = (c.width - margin * 2) * s[key] / s['count']
                c.rect(xx, divider + 88, width, 14, tone)
                c.geometry.append(dict(kind='breadth-segment', key=key, count=s[key], width=width))
                xx += width
            days = '/'.join(str(calendar.monthrange(*map(int, p['month'].split('-')))[1]) for p in r['history'])
            c.text(margin, divider + 133 if portrait else divider + 118,
                   f'各月天数：{days}；未按日均归一。' if zh else f'Days per month: {days}. Monthly totals, not daily averages.', 'note', MUTED)
        else:
            c.runs(margin, divider + 36, [(f'{s["declining"]} / {s["count"]} ', RED, True),
                                         ('个样本访问量下降' if zh else 'domains recorded fewer visits', INK, False)], 'insight')
            decline_share = s['declining'] / s['count'] * 100
            text = (f'总量上升，{decline_share:.0f}% 的可比域名仍在回落。' if s['growth'] > 0 else f'{decline_share:.0f}% 的可比域名访问量下降。') if zh else f'{decline_share:.0f}% of comparable domains declined.'
            c.text(margin, divider + 91, text, 'note', MUTED)
        c.save(path, self.audit_root)

    def contribution(self, path, portrait, zh):
        s = self.summary
        c = self.base(portrait, zh, '哪些样本带来访问增减' if zh else 'Where visits were gained and lost',
                      '各取正、负贡献前三 · 对整体环比的百分点贡献（pp）' if zh else 'Top three in each direction · Contribution to sample growth (pp)')
        rows = s['positive'] + s['negative']
        margin = 72 if portrait else 110
        left, right = (442, 883) if portrait else (620, 1630)
        low = min([0] + [p['contributionPp'] for p in rows])
        high = max([0] + [p['contributionPp'] for p in rows])
        span = max(high - low, .01)
        low -= span * .11; high += span * .05
        x = lambda value: left + (value - low) / (high - low) * (right - left)
        first, step = (408, 88) if portrait else (363, 70)
        c.line(x(0), first - 44, x(0), first + step * (len(rows) - 1) + 37, MUTED)
        copy = self.payload['locales']['zh-CN' if zh else 'en']
        for i, p in enumerate(rows):
            yy, value = first + i * step, p['contributionPp']
            name = copy['productNames'][p['domain']]
            region = (margin, yy - 36, left - 65, yy + 49)
            height = c.wrapped(margin, yy - 28, name, left - margin - 65, 'name', region=region)
            if name != p['domain']:
                c.text(margin, yy - 24 + height, p['domain'], 'domain', MUTED, region=region)
            c.rect(min(x(0), x(value)), yy - 12, abs(x(value) - x(0)), 24, GREEN if value > 0 else RED)
            c.text(x(value) + (14 if value >= 0 else -14), yy - 19, f'{value:+.2f}', 'value',
                   GREEN if value > 0 else RED, align='left' if value >= 0 else 'right')
            c.geometry.append(dict(kind='contribution-bar', domain=p['domain'], value=value, zero=x(0), end=x(value), pixelsPerPp=(right - left) / (high - low)))
        c.text(x(0), first + step * (len(rows) - 1) + 53, '0', 'axis', MUTED, align='center')
        divider = 1050 if portrait else 830
        c.line(margin, divider, c.width - margin, divider)
        c.text(margin, divider + 28, '按访问增减量排序，非百分比增速榜。' if zh else 'Ranked by visit change, not percentage growth.', 'note')
        c.text(margin, divider + 78, '柱长表示对样本整体环比的贡献。' if zh else 'Bar length shows contribution to total sample growth.', 'note', MUTED)
        c.save(path, self.audit_root)

    def social(self, path, zh):
        c = Canvas(1200, 630, self.font_path, .78)
        r, s = self.report, self.summary
        copy = self.payload['locales']['zh-CN' if zh else 'en']
        c.brand(215, 72, zh, compact=True)
        c.text(985, 82, copy['name'] + ' · ' + r['month'].replace('-', '.'), 'header', align='right')
        status = ('已发布' if zh else 'Published') if r['publication']['status'] == 'published' else ('发布前预览' if zh else 'Publication preview')
        c.text(985, 115, r['version'] + ' · ' + status, 'footer', MUTED, align='right')
        c.text(215, 161, '样本访问量增减对照' if zh else 'Visit gains, losses and net change', 'title')
        c.text(215, 216, (f'{s["count"]} 个可比域名 · 环比 {s["growth"] * 100:+.1f}%' if zh else f'{s["count"]} comparable domains · {s["growth"] * 100:+.1f}% month over month'), 'body', MUTED)
        rows = self.volume_rows(zh)
        maximum = max(1, *(abs(row[2]) for row in rows))
        for index, (kind, label, value, tone) in enumerate(rows):
            yy = 282 + index * 66
            c.text(215, yy, label, 'axis')
            c.text(985, yy, self.volume_label(value, zh), 'value', tone, True, align='right')
            width = abs(value) / maximum * 600
            c.rect(215, yy + 33, width, 10, tone)
            c.geometry.append(dict(kind='change-volume-bar', metric=kind, value=value, width=width, pixelsPerVisit=600/maximum))
        c.line(215, 495, 985, 495)
        c.text(215, 514, '全球 Web 整站估计访问量；含综合平台，仅反映样本。' if zh else 'Whole-site Web estimates; includes multipurpose platforms; sample only.', 'footer', MUTED)
        c.text(215, 543, copy['url'].removeprefix('https://'), 'footer', MUTED)
        # Keep essential social content inside the established center-crop safe rectangle.
        c.texts = [(item, role, size, (200, 65, 1000, 565)) for item, role, size, _ in c.texts]
        c.save(path, self.audit_root)

    def render(self, chart, path, portrait, zh):
        if chart == 'overview':
            self.overview(path, portrait, zh)
        elif chart == 'trend':
            self.render_trend(path, portrait, zh, detailed=True)
        else:
            self.contribution(path, portrait, zh)
