import { createHash } from 'node:crypto';
import { validateReport } from '../../lib/reports/model.ts';

export const SECTORS = {
  'video-generation': { name: { en: 'Video generation', 'zh-CN': '视频生成' }, scopeName: { en: 'Video-related platforms', 'zh-CN': '视频生成相关平台' } },
  'image-generation': { name: { en: 'Image generation', 'zh-CN': '图像生成' }, scopeName: { en: 'Image-related platforms', 'zh-CN': '图像生成相关平台' } },
  'music-generation': { name: { en: 'Music generation', 'zh-CN': '音乐生成' }, scopeName: { en: 'Music-related platforms', 'zh-CN': '音乐生成相关平台' } },
};
export const CHECKS = ['totals', 'breadth', 'median', 'concentration', 'contributors'];
// Explicit coverage checks, not an exhaustive list of products in each market.
const COVERAGE_DOMAINS = { 'video-generation': ['runwayml.com'], 'image-generation': ['midjourney.com'], 'music-generation': ['suno.com', 'flowmusic.app', 'mureka.ai', 'udio.com'] };
// Preserve v1 input defaults; newly scoped exports use the accepted shared design.
const LEGACY_DEFAULT_TEMPLATES = { 'video-generation': 'classic-v1', 'image-generation': 'classic-v1', 'music-generation': 'editorial-v1' };
const LEGACY_SECTORS = Object.keys(LEGACY_DEFAULT_TEMPLATES);
export const sha256 = value => createHash('sha256').update(value).digest('hex');
export const json = value => JSON.stringify(value, null, 2) + '\n';
export function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}
export const digest = value => sha256(JSON.stringify(canonical(value)));
export function insist(condition, message) { if (!condition) throw Error(message); }
export function validDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
}
export function shiftMonth(month, offset) {
  insist(/^\d{4}-(0[1-9]|1[0-2])$/.test(month), 'Invalid month');
  const [y, m] = month.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1 + offset, 1)).toISOString().slice(0, 7);
}
function publicText(value) {
  insist(typeof value === 'string' && value.trim().length > 0 && value.length < 250 && !/[<>\r\n]/.test(value) && !/similarweb/i.test(value), 'Invalid public editorial text');
  return value;
}
function headline(s) {
  if (s.growth > 0 && s.declining > s.count / 2) return { en: 'Visits grew. More than half of samples declined.', 'zh-CN': '总访问量上涨，过半样本仍在下降' };
  if (s.growth < 0 && s.growing > s.count / 2) return { en: 'Visits fell. More than half of samples grew.', 'zh-CN': '总访问量下降，过半样本仍在增长' };
  return { en: s.growth > 0 ? 'Sample visits increased this month.' : s.growth < 0 ? 'Sample visits declined this month.' : 'Sample visits were unchanged this month.', 'zh-CN': s.growth > 0 ? '本月样本访问量增长' : s.growth < 0 ? '本月样本访问量回落' : '本月样本访问量持平' };
}

/** Consume an explicitly completed export, never infer readiness from a few available rows. */
export function prepareBatch(batch, today = new Date().toISOString().slice(0, 10)) {
  insist([1, 2].includes(batch?.schemaVersion), 'Unsupported ready-export schema');
  insist(batch.state === 'complete', 'Monthly data is not marked complete');
  const baselineMonth = shiftMonth(batch.month, -1);
  insist(validDate(batch.preparedOn) && batch.preparedOn <= today && batch.month < batch.preparedOn.slice(0, 7), 'Only a completed calendar month can be prepared');
  insist(typeof batch.preparedBy === 'string' && batch.preparedBy.trim(), 'Identify the preparer or upstream export job');
  const expected = batch.schemaVersion === 1 ? LEGACY_SECTORS : batch.expectedSectors;
  insist(Array.isArray(expected) && expected.length > 0 && new Set(expected).size === expected.length && expected.every(sector => typeof sector === 'string' && Object.hasOwn(SECTORS, sector)), 'Declare a non-empty unique expectedSectors list of configured markets');
  insist(Array.isArray(batch.sectors) && batch.sectors.length === expected.length && new Set(batch.sectors.map(s => s?.sector)).size === batch.sectors.length && batch.sectors.every(s => expected.includes(s?.sector)), batch.schemaVersion === 1 ? 'The legacy monthly batch requires video, image and music exactly once' : 'Completed market scope differs from expectedSectors; partial exports are not complete');
  return batch.sectors.map(input => {
    const { sector, snapshot, traffic } = input;
    insist(snapshot?.latestMonth === batch.month + '-01' && snapshot.baselineMonth === baselineMonth + '-01', `${sector}: snapshot period mismatch`);
    insist(Array.isArray(snapshot.peers) && input.expectedDomains === snapshot.peers.length && input.expectedDomains >= 3, `${sector}: incomplete cohort export`);
    // Keep editorial additions separate from the original category export. They
    // undergo the same traffic reconciliation and are included in draft review.
    const additions = input.supplementalPeers ?? [];
    insist(Array.isArray(additions), `${sector}: invalid supplemental candidates`);
    const supplements = additions.map(item => {
      insist(['parent_category_assignment', 'category_snapshot_omission'].includes(item.reason) && validDate(item.checkedOn) && item.checkedOn <= batch.preparedOn, `${sector}: invalid supplement evidence`);
      insist(item.evidenceUrl === `https://${item.peer?.normalizedDomain}/`, `${sector}: supplement requires its official domain URL`);
      return { domain: item.peer.normalizedDomain, reason: item.reason, evidenceUrl: item.evidenceUrl, checkedOn: item.checkedOn };
    });
    const candidates = [...snapshot.peers, ...additions.map(item => item.peer)];
    insist(Array.isArray(traffic?.rows) && traffic.rows.length > 0, `${sector}: missing traffic rows`);
    const byDomain = new Map();
    for (const row of traffic.rows) {
      insist(typeof row.normalized_domain === 'string' && /^\d{4}-(0[1-9]|1[0-2])-01$/.test(row.traffic_month), 'Invalid traffic key');
      const months = byDomain.get(row.normalized_domain) ?? new Map();
      insist(!months.has(row.traffic_month), `${sector}: duplicate domain/month evidence`);
      insist(row.visits == null || (Number.isSafeInteger(row.visits) && row.visits >= 0), `${sector}: invalid visits`);
      months.set(row.traffic_month, row.visits); byDomain.set(row.normalized_domain, months);
    }
    const seen = new Set(), products = [], excluded = [];
    for (const peer of candidates) {
      const domain = peer.normalizedDomain;
      insist(typeof domain === 'string' && /^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/.test(domain) && !seen.has(domain), `${sector}: invalid or duplicate domain`);
      seen.add(domain);
      const months = byDomain.get(domain);
      const current = months?.get(batch.month + '-01'), previous = months?.get(baselineMonth + '-01');
      // A missing source row must not silently shrink a snapshot that contains a number.
      insist((current ?? null) === (peer.visits ?? null) && (previous ?? null) === (peer.previousVisits ?? null), `${sector}: snapshot/source disagreement for ${domain}`);
      if (!Number.isSafeInteger(current) || !Number.isSafeInteger(previous) || previous <= 0) {
        excluded.push({ domain, reason: !Number.isSafeInteger(current) ? 'missing_current' : !Number.isSafeInteger(previous) ? 'missing_baseline' : 'zero_baseline' });
        continue;
      }
      const slug = typeof peer.canonicalSlug === 'string' && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(peer.canonicalSlug) ? peer.canonicalSlug : undefined;
      products.push({ domain, current, previous, ...(slug ? { slug } : {}) });
    }
    products.sort((a, b) => a.domain.localeCompare(b.domain));
    const history = [];
    // Use only the contiguous suffix of three months for this exact comparable cohort.
    for (let offset = 0; offset >= -2; offset--) {
      const month = shiftMonth(batch.month, offset), values = products.map(p => byDomain.get(p.domain)?.get(month + '-01'));
      if (!values.every(v => Number.isSafeInteger(v) && v >= 0)) break;
      history.unshift({ month, visits: values.reduce((a, b) => a + b, 0), count: products.length });
    }
    const data = { id: `${sector}-${batch.month}`, sector, month: batch.month, baselineMonth, version: 'v1', templateVersion: input.templateVersion === undefined ? (batch.schemaVersion === 1 ? LEGACY_DEFAULT_TEMPLATES[sector] : 'editorial-v1') : input.templateVersion, preparedOn: batch.preparedOn, publication: { status: 'review', publishedOn: null }, products, history };
    data.coverage = {
      candidates: seen.size, comparable: products.length,
      observedCurrentVisits: candidates.reduce((total, p) => total + (Number.isSafeInteger(p.visits) ? p.visits : 0), 0),
      exclusionCounts: Object.fromEntries(['missing_current', 'missing_baseline', 'zero_baseline'].map(reason => [reason, excluded.filter(p => p.reason === reason).length])),
      unrepresentedDomains: COVERAGE_DOMAINS[sector].filter(domain => !products.some(p => p.domain === domain)).map(domain => ({domain, reason: excluded.find(p => p.domain === domain)?.reason ?? 'outside_candidates'})),
      categorySnapshot: typeof snapshot.marketSlug === 'string' && /^[a-z0-9-]+$/.test(snapshot.marketSlug) ? snapshot.marketSlug : sector,
      methodVersion: supplements.length ? 'comparable-domains-v2' : 'comparable-domains-v1',
      ...(supplements.length ? { categoryCandidates: snapshot.peers.length, supplements } : {}),
    };
    const exceptions = input.coverageExceptions ?? [];
    insist(Array.isArray(exceptions) && new Set(exceptions.map(p => p.domain)).size === exceptions.length, `${sector}: invalid coverage exceptions`);
    for (const item of exceptions) {
      const gap = data.coverage.unrepresentedDomains.find(p => p.domain === item.domain);
      insist(gap && COVERAGE_DOMAINS[sector].includes(item.domain) && gap.reason === item.reason, `${sector}: coverage exception does not match a missing required domain`);
      insist(typeof item.reviewedBy === 'string' && item.reviewedBy.trim() && validDate(item.checkedOn) && item.checkedOn <= batch.preparedOn && item.evidenceUrl === `https://${item.domain}/`, `${sector}: coverage exception requires reviewer, date and official evidence`);
      publicText(item.explanation?.en); publicText(item.explanation?.['zh-CN']);
    }
    data.coverage.criticalChecks = { policyVersion: 'critical-domains-v1', domains: COVERAGE_DOMAINS[sector].map(domain => {
      if (products.some(p => p.domain === domain)) return { domain, status: 'included' };
      const reason = data.coverage.unrepresentedDomains.find(p => p.domain === domain).reason;
      const reviewed = exceptions.find(p => p.domain === domain);
      return reviewed ? { domain, status: 'reviewed_exclusion', reason, explanation: reviewed.explanation, evidenceUrl: reviewed.evidenceUrl, checkedOn: reviewed.checkedOn } : { domain, status: 'unresolved', reason };
    }) };
    if (input.scopeNotes !== undefined) {
      insist(Array.isArray(input.scopeNotes) && new Set(input.scopeNotes.map(note => note.domain)).size === input.scopeNotes.length, `${sector}: invalid scope notes`);
      data.coverage.scopeNotes = input.scopeNotes.map(note => {
        insist(products.some(p => p.domain === note.domain) && note.evidenceUrl === `https://${note.domain}/` && validDate(note.checkedOn) && note.checkedOn <= batch.preparedOn, `${sector}: scope note needs a comparable domain and dated official evidence`);
        return { domain: note.domain, description: { en: publicText(note.description?.en), 'zh-CN': publicText(note.description?.['zh-CN']) }, checkedOn: note.checkedOn, evidenceUrl: note.evidenceUrl };
      });
    }
    if (input.reuse !== undefined) {
      insist(input.reuse?.policyVersion === 'attribution-v1' && validDate(input.reuse.confirmedOn) && input.reuse.confirmedOn <= batch.preparedOn, `${sector}: invalid reuse confirmation`);
      data.reuse = { policyVersion: 'attribution-v1', confirmedOn: input.reuse.confirmedOn };
    }
    const summary = validateReport(data);
    insist(Number.isSafeInteger(summary.current) && Number.isSafeInteger(summary.previous), 'Visit total exceeds safe integer precision');
    const featuredDomain = input.editorial?.featuredDomain ?? [...summary.rows].sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || a.domain.localeCompare(b.domain))[0].domain;
    insist(products.some(p => p.domain === featuredDomain), 'Featured domain is outside the cohort');
    const copy = input.editorial?.headline ?? (data.templateVersion === 'editorial-v1'
      ? { en: 'Visit gains, losses and the net change', 'zh-CN': '样本访问量的增量、减量与净变化' }
      : headline(summary));
    const definition = { data, seoPageId: 'report' + sector.split('-').map(s => s[0].toUpperCase() + s.slice(1)).join('') + batch.month.replace('-', ''), ...SECTORS[sector], headline: { en: publicText(copy.en), 'zh-CN': publicText(copy['zh-CN']) }, featuredDomain };
    insist(!/similarweb/i.test(json(definition)), 'Public field allowlist contains a supplier name');
    return { definition, summary, coverage: { sector, candidates: seen.size, comparable: products.length, excluded, historyMonths: history.map(p => p.month) } };
  });
}

/** Only selected reports gate generation; another sector may remain in review. */
export function assertCoverageReady(drafts) {
  for (const { definition: { data } } of drafts) {
    const checks = data.coverage?.criticalChecks;
    insist(checks && checks.domains.length === COVERAGE_DOMAINS[data.sector].length && COVERAGE_DOMAINS[data.sector].every(domain => checks.domains.some(p => p.domain === domain)), `${data.id}: required-domain checks are missing`);
    const unresolved = checks.domains.filter(p => p.status === 'unresolved');
    insist(!unresolved.length, `${data.id}: unresolved coverage: ${unresolved.map(p => `${p.domain} (${p.reason})`).join(', ')}. Supply matching reviewed coverageExceptions or complete the evidence; do not fill missing values with zero.`);
  }
}

export function validateApproval(approval, manifest, today = new Date().toISOString().slice(0, 10)) {
  insist(approval?.status === 'approved' && approval.draftDigest === manifest.draftDigest, 'Approval is pending or belongs to different draft bytes');
  insist(typeof approval.reviewedBy === 'string' && approval.reviewedBy.trim() && approval.reviewedBy.trim().toLowerCase() !== manifest.preparedBy.trim().toLowerCase(), 'A second reviewer is required');
  insist(validDate(approval.approvedOn) && approval.approvedOn >= manifest.preparedOn && approval.approvedOn <= today, 'Invalid approval date');
  insist(Object.keys(approval.reports ?? {}).sort().join() === manifest.reportIds.slice().sort().join(), 'Approval must cover every report in this draft');
  for (const id of manifest.reportIds) insist(CHECKS.every(key => approval.reports[id]?.[key] === true), `${id}: five-point review is incomplete`);
}
