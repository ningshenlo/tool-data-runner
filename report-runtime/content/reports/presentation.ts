import { reportAssetRoot, validateReport, type ReportData } from '../../lib/reports/model.ts';
import type { STATIC_SEO_PAGES } from '../../config/seo.ts';
type Localized = { en: string; 'zh-CN': string };
type Definition = { data: ReportData; seoPageId: keyof typeof STATIC_SEO_PAGES; name: Localized; scopeName: Localized; headline: Localized; featuredDomain: string };
export function edition(definition: Definition) {
  const summary = validateReport(definition.data);
  if (!summary.rows.some(p => p.domain === definition.featuredDomain)) throw Error('Featured domain is outside the fixed cohort.');
  const frozen = definition.data.editorial;
  return { ...definition, headline: frozen ? { en: frozen.en.headline, 'zh-CN': frozen['zh-CN'].headline } : definition.headline, summary };
}
export type ReportEdition = ReturnType<typeof edition>;
export function reportPath(report: ReportEdition) { return `/reports/${report.data.sector}/${report.data.month}`; }
export function reportAssetBase(report: ReportEdition, locale: string) { return `${reportAssetRoot(report.data)}/${locale === 'zh-CN' ? 'zh-cn' : 'en'}`; }
export function reportUrl(report: ReportEdition, locale: string) { return `https://sigpik.com${locale === 'zh-CN' ? '/zh-cn' : ''}${reportPath(report)}`; }
export function reportZipName(report: ReportEdition) { const r = report.data; return `sigpik-report-${r.sector}-${r.month}-${r.version}.zip`; }
export function localized(value: Localized, locale: string) { return value[locale === 'zh-CN' ? 'zh-CN' : 'en']; }
export function reportMonth(month: string, locale: string) { const [y, m] = month.split('-').map(Number); return locale === 'zh-CN' ? `${y} 年 ${m} 月` : new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' }).format(new Date(Date.UTC(y!, m! - 1, 1))); }
export function signed(value: number, digits = 2) { return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`; }
export function changePhrase(rate: number, locale: string) { return locale === 'zh-CN' ? rate === 0 ? '持平' : `${rate > 0 ? '增长' : '下降'} ${Math.abs(rate * 100).toFixed(1)}%` : rate === 0 ? 'were unchanged' : `${rate > 0 ? 'rose' : 'fell'} ${Math.abs(rate * 100).toFixed(1)}%`; }
const productNames: Record<string, { en: string; 'zh-CN'?: string }> = {
  'elevenlabs.io': { en: 'ElevenLabs' }, 'speechify.com': { en: 'Speechify' }, 'naturalreaders.com': { en: 'NaturalReader' },
  'gamma.app': { en: 'Gamma' }, 'napkin.ai': { en: 'Napkin AI' }, 'beautiful.ai': { en: 'Beautiful.ai' },
  'suno.com': { en: 'Suno' },
  'midjourney.com': { en: 'Midjourney' },
  'davinci.ai': { en: 'DaVinci' }, 'higgsfield.ai': { en: 'Higgsfield' }, 'dreamina.capcut.com': { en: 'Dreamina' }, 'kling.ai': { en: 'Kling AI', 'zh-CN': '可灵 Kling AI' }, 'veed.io': { en: 'VEED' },
  'magnific.com': { en: 'Magnific' }, 'firefly.adobe.com': { en: 'Adobe Firefly' }, 'picsart.com': { en: 'Picsart' }, 'kittl.com': { en: 'Kittl' }, 'zeemo.ai': { en: 'Zeemo' },
  'flowmusic.app': { en: 'Flow Music' }, 'mureka.ai': { en: 'Mureka' }, 'treblo.com': { en: 'Treblo' }, 'udio.com': { en: 'Udio' },
};
export function reportProductName(domain: string, locale: string) { const p = productNames[domain]; return (locale === 'zh-CN' ? p?.['zh-CN'] : undefined) ?? p?.en ?? domain; }
export function reportScope(report: ReportEdition, locale: string) { const n = report.summary.count; return locale === 'zh-CN' ? `口径：${n} 个可比域名的全球 Web 整站估计访问量，包含综合平台；仅反映样本，不代表行业总规模、用户数或收入。` : `Scope: estimated worldwide whole-site Web visits across ${n} comparable domains, including multipurpose platforms; sample observations, not industry totals, users or revenue.`; }
export function reportDirectionMethod(locale: string) {
  return locale === 'zh-CN' ? '涨跌按两期未四舍五入的估计访问量之差的正负判断，持平指两期完全相等。小幅变动也计入方向，不代表统计显著变化；缺失值不按零处理。' : 'Direction follows the sign of the change in unrounded estimated visits; unchanged means exactly equal in both months. Small changes count toward direction, without implying statistical significance. Missing values are not treated as zero.';
}
export function reportReuseText(report: ReportEdition, locale: string) {
  if (report.data.reuse?.policyVersion !== 'attribution-v1') return undefined;
  return locale === 'zh-CN' ? '本期报告的图表、估计访问量及可下载域名明细可公开引用、转载和再分发。请保留 Sigpik 署名、统计月份、样本口径、版本及本期原始链接；改绘或节选请注明。估计访问量不等于独立用户数或行业总规模。' : 'Charts, estimated visit counts and downloadable domain data in this edition may be quoted, republished and redistributed. Retain Sigpik attribution, reporting period, sample scope, version and the original edition link; identify adaptations or excerpts. Estimated visits are not unique users or industry totals.';
}
export function reportVolumeBalance(report: ReportEdition, locale: string) {
  const s = report.summary, number = (value: number) => value.toLocaleString(locale === 'zh-CN' ? 'zh-CN' : 'en-US');
  return locale === 'zh-CN' ? `${s.growing} 个增长域名合计增加 ${number(s.growingVisitIncrease)} 次，${s.declining} 个下降域名合计减少 ${number(s.decliningVisitDecrease)} 次，相抵后${s.delta >= 0 ? '净增' : '净减'} ${number(Math.abs(s.delta))} 次。` : `${s.growing} growing domains added ${number(s.growingVisitIncrease)} visits; ${s.declining} declining domains lost ${number(s.decliningVisitDecrease)} visits, for a net ${s.delta >= 0 ? 'gain' : 'loss'} of ${number(Math.abs(s.delta))} visits.`;
}
export function reportDeck(report: ReportEdition, locale: string) {
  if (report.data.editorial) return report.data.editorial[locale === 'zh-CN' ? 'zh-CN' : 'en'].deck;
  const s = report.summary, f = s.rows.find(p => p.domain === report.featuredDomain)!;
  if (report.data.templateVersion === 'editorial-v1') return locale === 'zh-CN' ? `${s.count} 个可比样本的估计 Web 访问量环比${changePhrase(s.growth, locale)}。${reportVolumeBalance(report, locale)}` : `Estimated Web visits across ${s.count} comparable samples ${changePhrase(s.growth, locale)} month over month. ${reportVolumeBalance(report, locale)}`;
  const name = reportProductName(f.domain, locale);
  return locale === 'zh-CN' ? `${s.count} 个可比样本的估计 Web 访问量环比${changePhrase(s.growth, locale)}；${name} 的访问量${changePhrase(f.growth, locale)}。` : `Estimated Web visits across ${s.count} comparable samples ${changePhrase(s.growth, locale)} month over month; ${name}'s visits ${changePhrase(f.growth, locale)}.`;
}
export function reportShareBody(report: ReportEdition, locale: string) { return report.data.editorial?.[locale === 'zh-CN' ? 'zh-CN' : 'en'].shareText ?? `${locale === 'zh-CN' ? `【Sigpik ${reportMonth(report.data.month, locale)}${localized(report.name, locale)}报告】` : `[Sigpik ${reportMonth(report.data.month, locale)} ${localized(report.name, locale)} Report] `}${reportDeck(report, locale)}\n${reportScope(report, locale)}\n${reportUrl(report, locale)}`; }
export function reportStatusLine(report: ReportEdition, locale: string) {
  const r = report.data;
  const status = r.publication.status === 'published' ? (locale === 'zh-CN' ? `已发布 ${r.publication.publishedOn}` : `Published ${r.publication.publishedOn}`) : (locale === 'zh-CN' ? '发布前预览 · 待复核' : 'Publication preview · Pending review');
  return `${r.month} / ${r.version} · ${status}`;
}
export function reportShareText(report: ReportEdition, locale: string) { return `${reportShareBody(report, locale)}\n${reportStatusLine(report, locale)}`; }
export function reportCitation(report: ReportEdition, locale: string, text: string) {
  return `${text}\n\n${locale === 'zh-CN' ? '数据整理与分析：Sigpik 市场观测' : 'Data compilation and analysis: Sigpik Market Observations'}\n${reportUrl(report, locale)}\n${reportScope(report, locale)}\n${reportStatusLine(report, locale)}`;
}
export function reportFindingTitles(report: ReportEdition, locale: string) {
  if (report.data.editorial) return report.data.editorial[locale === 'zh-CN' ? 'zh-CN' : 'en'].titles;
  const s = report.summary;
  if (report.data.templateVersion === 'editorial-v1') return locale === 'zh-CN' ? ['增长与下降的访问体量', `${reportProductName(report.featuredDomain, locale)} 的变化贡献`, '最大域名与其余样本对照'] : ['Visit gains and losses', `${reportProductName(report.featuredDomain, locale)}’s contribution`, 'Largest domain and remaining sample'];
  return locale === 'zh-CN' ? [s.declining > s.count / 2 ? '多数样本访问量下降' : s.growing > s.count / 2 ? '多数样本访问量增长' : '样本涨跌分布', `${reportProductName(report.featuredDomain, locale)} 的变化贡献`, '头部份额与访问量变化'] : [s.declining > s.count / 2 ? 'Most samples lost visits' : s.growing > s.count / 2 ? 'Most samples gained visits' : 'Sample growth distribution', `${reportProductName(report.featuredDomain, locale)}'s contribution`, 'Top-three share and visits'];
}
export function reportFindings(report: ReportEdition, locale: string) {
  if (report.data.editorial) return report.data.editorial[locale === 'zh-CN' ? 'zh-CN' : 'en'].findings;
  const s = report.summary, r = report.data, f = s.rows.find(p => p.domain === report.featuredDomain)!;
  const month = reportMonth(r.month, locale), name = reportProductName(f.domain, locale), pp = Math.abs(s.top3ShareChangePp).toFixed(2);
  if (r.templateVersion === 'editorial-v1') {
    const top = s.rows[0]!, remaining = (s.current - top.current) / (s.previous - top.previous) - 1;
    return locale === 'zh-CN' ? [
      `据 Sigpik 市场观测，${month}，${s.count} 个可比域名的估计 Web 访问量环比${changePhrase(s.growth, locale)}。${reportVolumeBalance(report, locale)}`,
      `据 Sigpik 市场观测，${month}，${name} 的估计 Web 访问量环比${changePhrase(f.growth, locale)}，${f.delta >= 0 ? '增加' : '减少'} ${(Math.abs(f.delta) / 10000).toFixed(1)} 万次，对样本整体环比贡献 ${signed(f.contributionPp)} 个百分点。`,
      `据 Sigpik 市场观测，${month}，${reportProductName(top.domain, locale)} 占样本估计 Web 访问量 ${(top.current / s.current * 100).toFixed(1)}%；固定剔除该域名后，其余 ${s.count - 1} 个域名合计环比${changePhrase(remaining, locale)}。全部 ${s.count} 个域名的增速中位数为 ${signed(s.medianGrowth * 100, 1)}%。`,
    ] : [
      `According to Sigpik Market Observations, estimated Web visits across ${s.count} comparable domains ${changePhrase(s.growth, locale)} in ${month}. ${reportVolumeBalance(report, locale)}`,
      `According to Sigpik Market Observations, ${name}'s estimated Web visits ${changePhrase(f.growth, locale)} in ${month}, ${f.delta >= 0 ? 'adding' : 'losing'} ${(Math.abs(f.delta) / 1e6).toFixed(2)} million visits and contributing ${signed(f.contributionPp)} percentage points to sample growth.`,
      `According to Sigpik Market Observations, ${reportProductName(top.domain, locale)} accounted for ${(top.current / s.current * 100).toFixed(1)}% of estimated sample Web visits in ${month}. Excluding that domain in both months, visits across the remaining ${s.count - 1} domains ${changePhrase(remaining, locale)}. Median growth across all ${s.count} domains was ${signed(s.medianGrowth * 100, 1)}%.`,
    ];
  }
  return locale === 'zh-CN' ? [
    `据 Sigpik 市场观测，${month}，${s.count} 个可比${localized(report.scopeName, locale)}样本的估计 Web 访问量环比${changePhrase(s.growth, locale)}，其中 ${s.declining} 个下降、${s.growing} 个增长、${s.unchanged} 个持平。`,
    `据 Sigpik 市场观测，${month}，${name} 的估计 Web 访问量环比${changePhrase(f.growth, locale)}，${f.delta >= 0 ? '增加' : '减少'} ${(Math.abs(f.delta) / 10000).toFixed(1)} 万次，对样本整体环比贡献 ${signed(f.contributionPp)} 个百分点。`,
    `据 Sigpik 市场观测，${month}，头部三家占样本访问量 ${(s.top3Share * 100).toFixed(1)}%，环比${s.top3ShareChangePp >= 0 ? '增加' : '减少'} ${pp} 个百分点；固定这三家比较，其合计访问量${changePhrase(s.currentTop3Growth, locale)}。`,
  ] : [
    `According to Sigpik Market Observations, estimated Web visits across ${s.count} comparable ${localized(report.scopeName, locale).toLowerCase()} ${changePhrase(s.growth, locale)} in ${month} versus ${reportMonth(r.baselineMonth, locale)}; ${s.declining} declined, ${s.growing} grew and ${s.unchanged} were unchanged.`,
    `According to Sigpik Market Observations, ${name}'s estimated Web visits ${changePhrase(f.growth, locale)} in ${month}, ${f.delta >= 0 ? 'adding' : 'losing'} ${(Math.abs(f.delta) / 1e6).toFixed(2)} million visits and contributing ${signed(f.contributionPp)} percentage points to sample growth.`,
    `According to Sigpik Market Observations, the top three accounted for ${(s.top3Share * 100).toFixed(1)}% of sample visits in ${month}, ${s.top3ShareChangePp >= 0 ? 'up' : 'down'} ${pp} percentage points; their combined visits ${changePhrase(s.currentTop3Growth, locale)} with membership held fixed.`,
  ];
}
