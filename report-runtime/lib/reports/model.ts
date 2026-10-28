import { summarizeComparableCohort } from '../market-statistics.ts';

export type ReportProduct = { domain: string; current: number; previous: number; name?: string; slug?: string };
export const GROWTH_BASELINE_MINIMUM = 100_000;
export const REPORT_TEMPLATE_VERSIONS = ['classic-v1', 'editorial-v1'] as const;
export type ReportTemplateVersion = (typeof REPORT_TEMPLATE_VERSIONS)[number];
export type ReportCoverage = {
  candidates: number; comparable: number; observedCurrentVisits: number;
  exclusionCounts: { missing_current: number; missing_baseline: number; zero_baseline: number };
  unrepresentedDomains: { domain: string; reason: 'outside_candidates' | 'missing_current' | 'missing_baseline' | 'zero_baseline' }[];
  categorySnapshot: string; methodVersion: string;
  categoryCandidates?: number;
  supplements?: { domain: string; reason: 'parent_category_assignment' | 'category_snapshot_omission'; evidenceUrl: string; checkedOn: string }[];
  criticalChecks?: { policyVersion: 'critical-domains-v1'; domains: {
    domain: string; status: 'included' | 'unresolved' | 'reviewed_exclusion'; reason?: string;
    explanation?: Record<'en' | 'zh-CN', string>; evidenceUrl?: string; checkedOn?: string;
  }[] };
  scopeNotes?: { domain: string; description: Record<'en' | 'zh-CN', string>; checkedOn: string; evidenceUrl: string }[];
};
export type ReportData = {
  id: string; sector: string; month: string; baselineMonth: string; version: string; preparedOn: string;
  templateVersion?: ReportTemplateVersion;
  reuse?: { policyVersion: 'attribution-v1'; confirmedOn: string };
  publication: { status: 'review' | 'published'; publishedOn: string | null };
  products: ReportProduct[]; history: { month: string; visits: number; count: number }[];
  coverage?: ReportCoverage;
  editorial?: Record<'en' | 'zh-CN', { headline: string; deck: string; titles: string[]; findings: string[]; shareText: string }>;
};
export function validateReport(report: ReportData) {
  if (report.templateVersion !== undefined && !REPORT_TEMPLATE_VERSIONS.includes(report.templateVersion)) throw Error('Unsupported report template version.');
  if (report.reuse && (report.reuse.policyVersion !== 'attribution-v1' || !/^\d{4}-\d{2}-\d{2}$/.test(report.reuse.confirmedOn) || report.reuse.confirmedOn > report.preparedOn)) throw Error('Invalid report reuse confirmation.');
  const pattern = /^\d{4}-(0[1-9]|1[0-2])$/;
  if (!pattern.test(report.month) || !pattern.test(report.baselineMonth) || !/^v[1-9]\d*$/.test(report.version)) throw Error('Invalid edition period or version.');
  const [year, month] = report.month.split('-').map(Number);
  const previousMonth = new Date(Date.UTC(year!, month! - 2, 1)).toISOString().slice(0, 7);
  if (report.baselineMonth !== previousMonth || report.id !== `${report.sector}-${report.month}`) throw Error('Edition identity or baseline mismatch.');
  if (!['review', 'published'].includes(report.publication.status) || (report.publication.status === 'published') !== !!report.publication.publishedOn) throw Error('Publication status and date disagree.');
  if (report.publication.status === 'published' && !report.editorial) throw Error('Published editions need frozen editorial copy.');
  const summary = summarizeReport(report.products);
  if (report.coverage) {
    const c = report.coverage;
    if (![c.candidates, c.comparable, c.observedCurrentVisits, ...Object.values(c.exclusionCounts)].every(n => Number.isSafeInteger(n) && n >= 0)
      || c.comparable !== summary.count || c.candidates !== c.comparable + Object.values(c.exclusionCounts).reduce((a,b) => a+b, 0)
      || c.observedCurrentVisits < summary.current) throw Error('Coverage does not reconcile with the comparable cohort.');
    if (c.supplements && (c.categoryCandidates === undefined || c.categoryCandidates + c.supplements.length !== c.candidates
      || new Set(c.supplements.map(p => p.domain)).size !== c.supplements.length
      || c.supplements.some(p => p.evidenceUrl !== `https://${p.domain}/` || !['parent_category_assignment', 'category_snapshot_omission'].includes(p.reason)))) throw Error('Supplemental coverage does not reconcile.');
    if (c.criticalChecks && (c.criticalChecks.policyVersion !== 'critical-domains-v1'
      || new Set(c.criticalChecks.domains.map(p => p.domain)).size !== c.criticalChecks.domains.length
      || c.criticalChecks.domains.some(p => !['included', 'unresolved', 'reviewed_exclusion'].includes(p.status)
        || (p.status === 'included') !== report.products.some(row => row.domain === p.domain)
        || (p.status === 'reviewed_exclusion' && (!p.reason || !p.explanation?.en || !p.explanation['zh-CN'] || p.evidenceUrl !== `https://${p.domain}/` || !p.checkedOn))))) throw Error('Critical coverage checks do not reconcile.');
  }
  if (summary.count < 3 || summary.current <= 0) throw Error('Reports require at least three comparable domains and a positive current total.');
  const history = report.history;
  if (history.length < 2 || history.some((p, i) => !pattern.test(p.month) || p.count !== summary.count || !Number.isSafeInteger(p.visits) || p.visits < 0 || (i > 0 && p.month <= history[i - 1]!.month))) throw Error('Historical series must use one ordered fixed cohort.');
  if (history.at(-1)?.month !== report.month || history.at(-1)?.visits !== summary.current || history.at(-2)?.month !== report.baselineMonth || history.at(-2)?.visits !== summary.previous) throw Error('Historical comparison does not reconcile.');
  return summary;
}
export function reportAssetRoot(report: Pick<ReportData, 'sector' | 'month' | 'version' | 'publication'>) {
  return `/report-assets/${report.publication.status === 'published' ? 'published' : 'previews'}/${report.sector}/${report.month}/${report.version}`;
}
export function trendScale(history: ReportData['history']) {
  const values = history.map(p => p.visits / 1e6);
  const minimum = Math.min(...values), maximum = Math.max(...values);
  const span = Math.max(maximum - minimum, maximum * .06, .001);
  const low = Math.max(0, minimum - span * .35), high = maximum + span * .5;
  return { low, high, ticks: Array.from({ length: 4 }, (_, i) => low + (high - low) * i / 3) };
}

export function summarizeReport(products: readonly ReportProduct[]) {
  if (!products.length) throw new Error('A report needs comparable products.');
  // Reports intentionally keep their frozen comparable cohort for concentration too.
  const cohort = summarizeComparableCohort(products);
  const { current, previous, rows: byCurrent } = cohort;
  const rows = byCurrent;
  const byPrevious = [...rows].sort((a, b) => b.previous - a.previous || a.domain.localeCompare(b.domain));
  const rates = rows.map(p => p.growth).sort((a, b) => a - b);
  const middle = Math.floor(rates.length / 2);
  const growing = rows.filter(p => p.delta > 0).length;
  const declining = rows.filter(p => p.delta < 0).length;
  // Positive magnitudes over the full cohort, not just the top contributors.
  const growingVisitIncrease = rows.reduce((total, p) => total + Math.max(p.delta, 0), 0);
  const decliningVisitDecrease = rows.reduce((total, p) => total + Math.max(-p.delta, 0), 0);
  const headCurrent = cohort.top3Current;
  const headPrevious = byCurrent.slice(0, 3).reduce((s, p) => s + p.previous, 0);
  const priorTop3 = byPrevious.slice(0, 3).reduce((s, p) => s + p.previous, 0);
  return {
    count: rows.length, current, previous, delta: current - previous, growth: current / previous - 1,
    growing, declining, unchanged: rows.length - growing - declining, breadth: growing / rows.length,
    growingVisitIncrease, decliningVisitDecrease,
    medianGrowth: rates.length % 2 ? rates[middle]! : (rates[middle - 1]! + rates[middle]!) / 2,
    top3Share: current ? headCurrent / current : 0,
    top3ShareChangePp: current ? 100 * (headCurrent / current - priorTop3 / previous) : 0,
    currentTop3Growth: headCurrent / headPrevious - 1,
    positive: cohort.positive,
    negative: cohort.negative,
    fastest: rows.filter(p => p.previous >= GROWTH_BASELINE_MINIMUM && p.delta > 0).sort((a, b) => b.growth - a.growth || a.domain.localeCompare(b.domain)).slice(0, 3),
    rows: byCurrent,
  };
}
