import fs from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { localized, reportFindings, reportUrl, reportMonth, reportProductName, reportScope, reportDeck, reportDirectionMethod, reportReuseText, reportFindingTitles, reportShareBody } from '../../content/reports/presentation.ts';
import { reportAssetRoot } from '../../lib/reports/model.ts';

export const serializeCsv = matrix => '\uFEFF' + matrix.map(row => row.map(cell => `"${String(cell ?? '').replaceAll('"', '""')}"`).join(',')).join('\r\n') + '\r\n';

export async function buildMedia(editions, root = process.cwd()) {
  const base = pathToFileURL(path.resolve(root) + path.sep);
  for (const edition of editions) {
  const report = edition.data, s = edition.summary;
  const locked = await fs.access(new URL(`content/reports/releases/${report.id}.${report.version}.json`, base)).then(() => true, error => { if (error.code === 'ENOENT') return false; throw error; });
  if (locked && report.publication.status !== 'published') throw new Error('Release lock exists; the frozen edition cannot be changed back to review.');
  if (report.publication.status !== 'review') { console.log(`Skipping immutable ${report.id}`); continue; }
  if (!report.coverage) throw Error(`Missing coverage for ${report.id}`);
  const rendering = { report, summary: s, seoPageId: edition.seoPageId, featuredDomain: edition.featuredDomain, locales: {} };
  for (const locale of ['en', 'zh-CN']) {
    const dir = new URL(`public${reportAssetRoot(report)}/${locale === 'en' ? 'en' : 'zh-cn'}/`, base);
    await fs.mkdir(dir, { recursive: true });
    rendering.locales[locale] = { headline: localized(edition.headline, locale), deck: reportDeck(edition, locale), titles: reportFindingTitles(edition, locale), shareText: reportShareBody(edition, locale), findings: reportFindings(edition, locale), url: reportUrl(edition, locale), month: reportMonth(report.month, locale), prior: reportMonth(report.baselineMonth, locale), name: localized(edition.name, locale), scopeName: localized(edition.scopeName, locale), scope: reportScope(edition, locale), featuredName: reportProductName(edition.featuredDomain, locale), productNames: Object.fromEntries(s.rows.map(p => [p.domain, reportProductName(p.domain, locale)])) };
    rendering.locales[locale].directionMethod = reportDirectionMethod(locale);
    rendering.locales[locale].reuseText = reportReuseText(edition, locale);
    const metrics = [
      ['sample_count', s.count, 'domains'], ['previous_visits', s.previous, 'estimated_visits'],
      ['current_visits', s.current, 'estimated_visits'], ['visit_change', s.delta, 'estimated_visits'],
      ['growing_visit_increase', s.growingVisitIncrease, 'estimated_visits'],
      ['declining_visit_decrease', s.decliningVisitDecrease, 'estimated_visits'],
      ['growth', s.growth * 100, 'percent'], ['growing', s.growing, 'domains'],
      ['declining', s.declining, 'domains'], ['unchanged', s.unchanged, 'domains'],
      ['breadth', s.breadth * 100, 'percent'], ['median_growth', s.medianGrowth * 100, 'percent'],
      ['top3_share', s.top3Share * 100, 'percent'], ['top3_share_change', s.top3ShareChangePp, 'percentage_points'],
      ['current_top3_growth', s.currentTop3Growth * 100, 'percent'],
      ...[...s.positive, ...s.negative].flatMap(p => [[`${p.domain}:visit_change`, p.delta, 'estimated_visits'], [`${p.domain}:growth`, p.growth * 100, 'percent'], [`${p.domain}:contribution`, p.contributionPp, 'percentage_points']]),
    ];
    const matrix = [['period', 'baseline_period', 'metric', 'value', 'unit', 'sample_domains', 'version', 'source', 'report_url', 'publication_status', 'published_on'], ...metrics.map(([metric, value, unit]) => [report.month, report.baselineMonth, metric, value, unit, s.count, report.version, 'Sigpik Market Observations', reportUrl(edition, locale), report.publication.status, report.publication.publishedOn ?? ''])];
    // This is a typed, formula-free CSV. Independent verify-assets.py recomputes
    // every metric. No desktop-only workbook runtime is needed on the server.
    if (metrics.some(([, value]) => typeof value !== 'number' || !Number.isFinite(value))) throw Error('Invalid summary metric');
    await fs.writeFile(new URL('summary.csv', dir), serializeCsv(matrix));
    const cohort = [['domain', 'period', 'baseline_period', 'previous_visits', 'current_visits', 'visit_change', 'growth_percent', 'contribution_percentage_points', 'version', 'publication_status', 'published_on', 'report_url'], ...s.rows.map(p => [p.domain, report.month, report.baselineMonth, p.previous, p.current, p.delta, p.growth * 100, p.contributionPp, report.version, report.publication.status, report.publication.publishedOn ?? '', reportUrl(edition, locale)])];
    await fs.writeFile(new URL('cohort.csv', dir), serializeCsv(cohort));
    await fs.writeFile(new URL('coverage.json', dir), JSON.stringify({ reportId: report.id, period: report.month, baselinePeriod: report.baselineMonth, version: report.version, publication: report.publication, ...report.coverage, comparableCurrentVisits: s.current, shareOfObservedVisits: s.current / report.coverage.observedCurrentVisits, scope: reportScope(edition, locale), reportUrl: reportUrl(edition, locale) }, null, 2) + '\n');
  }
  await fs.mkdir(new URL('work/reports/', base), { recursive: true });
  await fs.writeFile(new URL(`work/reports/${report.id}.render-input.json`, base), JSON.stringify(rendering, null, 2));
  }
}
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await buildMedia((await import('../../content/reports/registry.ts')).REPORTS);
