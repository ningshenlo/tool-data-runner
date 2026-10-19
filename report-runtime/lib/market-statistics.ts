/** Formula primitives only. Callers choose the cohort, period and publication rules. */
export type CurrentDomainSample = { domain: string; current: number };
export type ComparableDomainSample = CurrentDomainSample & { previous: number };

export function assertUniqueDomains(samples: readonly { domain: string }[]) {
  const domains = new Set<string>();
  for (const sample of samples) {
    if (!sample.domain.trim() || domains.has(sample.domain)) throw Error('Invalid or duplicate statistical domain.');
    domains.add(sample.domain);
  }
}

export function isValidVisitCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

export function summarizeCurrentCohort<T extends CurrentDomainSample>(samples: readonly T[]) {
  assertUniqueDomains(samples);
  if (samples.some(sample => !isValidVisitCount(sample.current))) throw Error('Invalid current observation.');
  const current = samples.reduce((sum, sample) => sum + sample.current, 0);
  const rows = [...samples].sort((a, b) => b.current - a.current || a.domain.localeCompare(b.domain));
  const top3Current = rows.slice(0, 3).reduce((sum, sample) => sum + sample.current, 0);
  // Display eligibility belongs to the caller; historical reports retain zero here.
  return { count: rows.length, current, top3Current, top3Share: current ? top3Current / current : 0, rows };
}

export function summarizeComparableCohort<T extends ComparableDomainSample>(samples: readonly T[]) {
  if (!samples.length) throw Error('A comparison needs comparable domains.');
  if (samples.some(sample => !isValidVisitCount(sample.previous) || sample.previous <= 0)) {
    throw Error('Invalid comparable baseline.');
  }
  const previous = samples.reduce((sum, sample) => sum + sample.previous, 0);
  const contributions = samples.map(sample => ({
    ...sample,
    delta: sample.current - sample.previous,
    growth: sample.current / sample.previous - 1,
    contributionPp: 100 * (sample.current - sample.previous) / previous,
  }));
  const snapshot = summarizeCurrentCohort(contributions);
  return {
    ...snapshot,
    previous,
    delta: snapshot.current - previous,
    growth: snapshot.current / previous - 1,
    positive: contributions.filter(sample => sample.delta > 0)
      .sort((a, b) => b.delta - a.delta || a.domain.localeCompare(b.domain)).slice(0, 3),
    negative: contributions.filter(sample => sample.delta < 0)
      .sort((a, b) => a.delta - b.delta || a.domain.localeCompare(b.domain)).slice(0, 3),
  };
}
