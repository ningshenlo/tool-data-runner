import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const input = resolve(process.argv[2] ?? '../reports/market-report-pilot-20260910');
const outputPath = 'content/reports/video-generation-2026-08.v1.json';
const existing = await readFile(outputPath, 'utf8').then(JSON.parse).catch(error => {
  if (error.code === 'ENOENT') return null;
  throw error;
});
if (existing?.publication.status === 'published') throw new Error('Published editions cannot be replaced by an import.');
const source = JSON.parse(await readFile(resolve(input, 'report-data.json'), 'utf8'));
const snapshot = JSON.parse(await readFile(resolve(input, 'video-market-snapshot.json'), 'utf8'));
const peers = new Map(snapshot.peers.map(p => [p.normalizedDomain, p]));
const data = {
  id: 'video-generation-2026-08', sector: 'video-generation', month: '2026-08', baselineMonth: '2026-07', version: 'v1',
  preparedOn: source.preparedOn, publication: { status: 'review', publishedOn: null },
  products: source.products.map(({ domain, current, previous }) => {
    const peer = peers.get(domain);
    return { domain, current, previous, ...(peer?.canonicalSlug ? { slug: peer.canonicalSlug } : {}) };
  }),
  history: source.history.map(({ month, visits, count }) => ({ month: month.slice(0, 7), visits, count })),
};
await mkdir('content/reports', { recursive: true });
await writeFile(outputPath, JSON.stringify(data, null, 2) + '\n');
console.log(`Imported ${data.products.length} comparable domains into the public field allowlist.`);
