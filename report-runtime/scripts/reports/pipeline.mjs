/** File-only report pipeline. No network requests, database writes or deployments. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { parseArgs } from 'node:util';
import { edition, reportDeck, reportMonth } from '../../content/reports/presentation.ts';
import { buildMedia } from './build-media.mjs';
import { reportAssetRoot } from '../../lib/reports/model.ts';
import { acquireDraftLock } from './draft-lock.mjs';
import { CHECKS, SECTORS, assertCoverageReady, digest, insist, json, prepareBatch, sha256, shiftMonth, validateApproval } from './pipeline-core.mjs';

import { runtimeBaseline } from './runtime-baseline.mjs';

const codeRoot = fileURLToPath(new URL('../../', import.meta.url));
const sharedFiles = ['content/reports/registry.ts', 'config/seo.ts', 'public/_headers'];
const read = async file => JSON.parse(await fs.readFile(file, 'utf8'));
const bytesOrNull = file => fs.readFile(file).catch(e => { if (e.code === 'ENOENT') return null; throw e; });
async function put(root, relative, body) {
  const file = within(root, relative); await fs.mkdir(path.dirname(file), { recursive: true }); await fs.writeFile(file, body);
}
export function within(root, relative) {
  insist(typeof relative === 'string' && relative && !relative.includes('\\') && !relative.split('/').includes('..') && !path.isAbsolute(relative), 'Invalid bundle path');
  const result = path.resolve(root, relative);
  insist(result.startsWith(path.resolve(root) + path.sep), 'Path escapes workspace');
  return result;
}
async function filesUnder(root, relative) {
  const dir = within(root, relative);
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(e => { if (e.code === 'ENOENT') return []; throw e; });
  const files = [];
  for (const entry of entries) {
    insist(!entry.isSymbolicLink(), 'Symlinks are not allowed in a report bundle');
    const name = relative + '/' + entry.name;
    if (entry.isDirectory()) files.push(...await filesUnder(root, name)); else if (entry.isFile()) files.push(name);
  }
  const baseline = await runtimeBaseline(root);
  return [...new Set([...files, ...Object.keys(baseline?.files ?? {}).filter(file => file.startsWith(relative + '/'))])].sort();
}
async function fingerprint(root, files) {
  const result = {}, baseline = await runtimeBaseline(root);
  for (const file of files.slice().sort()) { const bytes = await bytesOrNull(within(root, file)); result[file] = bytes === null ? (baseline?.files[file] ?? null) : sha256(bytes); }
  return result;
}
export async function run(command, args, cwd) {
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env: process.env, stdio: 'inherit', shell: false, windowsHide: true });
    child.once('error', reject); child.once('exit', code => code === 0 ? resolve() : reject(Error(`${path.basename(command)} failed (${code})`)));
  });
}
function pythonOptions() {
  return { executable: process.env.REPORT_PYTHON || 'python', args: [...(process.env.REPORT_PYTHON_DEPS ? ['--python-deps', process.env.REPORT_PYTHON_DEPS] : []), ...(process.env.REPORT_FONT ? ['--font', process.env.REPORT_FONT] : [])] };
}
async function renderingRuntime() {
  const fontPath = process.env.REPORT_FONT || 'C:/Windows/Fonts/msyh.ttc';
  const parsed = path.parse(fontPath);
  const boldPath = path.join(parsed.dir, parsed.base.includes('-Regular') ? parsed.base.replace('-Regular', '-Bold') : parsed.name + 'bd' + parsed.ext);
  const regular = await fs.readFile(fontPath), bold = await bytesOrNull(boldPath);
  return { fontSha256: sha256(regular), boldFontSha256: sha256(bold ?? regular), version: process.env.REPORT_RUNTIME_VERSION || 'local' };
}
const script = name => path.join(codeRoot, 'scripts/reports', name);
export function patchRegistration(original, drafts) {
  let registry = original['content/reports/registry.ts'].toString('utf8'), seo = original['config/seo.ts'].toString('utf8'), headers = original['public/_headers'].toString('utf8');
  // Preserve all unrelated source bytes, including the existing newline style.
  const registryNl = registry.includes('\r\n') ? '\r\n' : '\n', seoNl = seo.includes('\r\n') ? '\r\n' : '\n';
  for (const item of drafts) {
    const d = item.definition, r = d.data, name = 'report_' + r.id.replaceAll('-', '_');
    const oldImport = registry.match(new RegExp("import (\\w+) from './" + r.id + "\\.v1\\.json'"));
    let symbol = name;
    if (oldImport) {
      symbol = oldImport[1];
      const oldEntry = registry.split(/\r?\n/).find(line => line.includes(`edition({ data: ${symbol} as ReportData,`));
      insist(oldEntry, 'Existing registry shape changed');
      d.seoPageId = oldEntry.match(/seoPageId: '([^']+)'/)?.[1];
      insist(d.seoPageId, 'Existing SEO ID is missing');
      const { data, ...metadata } = d;
      registry = registry.replace(oldEntry, `  edition({ data: ${symbol} as ReportData, ...(${JSON.stringify(metadata)} as const) }),`);
    } else {
      registry = `import ${symbol} from './${r.id}.v1.json' with { type: 'json' };${registryNl}` + registry;
      const { data, ...metadata } = d;
      const anchor = `];${registryNl}export type ReportEdition`;
      insist(registry.includes(anchor), 'Registry insertion anchor missing');
      registry = registry.replace(anchor, `  edition({ data: ${symbol} as ReportData, ...(${JSON.stringify(metadata)} as const) }),${registryNl}${anchor}`);
    }
    const pagePath = `/reports/${r.sector}/${r.month}`;
    const escapedKey = d.seoPageId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const entryPattern = new RegExp('(^  ' + escapedKey + ': \\{\\r?\\n)([\\s\\S]*?)(\\r?\\n  \\},)', 'm');
    const entry = { path: pagePath, index: false, canonical: true, sitemap: false, reviewedAt: r.preparedOn, imagePath: `${reportAssetRoot(r)}/en/social-card.png`, copy: {} };
    const report = edition(d);
    for (const locale of ['en', 'zh-CN']) entry.copy[locale] = {
      title: locale === 'en' ? `AI ${d.name.en.split(' ')[0]} Traffic Report: ${reportMonth(r.month, locale)}` : `${reportMonth(r.month, locale)} AI ${d.name[locale]}流量报告`,
      description: locale === 'en' ? `${report.summary.count} comparable ${d.scopeName.en.toLowerCase()}: estimated Web visits ${report.summary.growth >= 0 ? 'rose' : 'fell'} ${Math.abs(report.summary.growth * 100).toFixed(1)}%. Explore findings, coverage notes and downloadable charts with sample data.` : `${report.summary.count}个${d.scopeName[locale]}可比样本：估计 Web 访问量环比${report.summary.growth >= 0 ? '上涨' : '下降'}${Math.abs(report.summary.growth * 100).toFixed(1)}%。查看增长贡献、覆盖说明与可引用图表。`,
    };
    const entryBody = JSON.stringify(entry, null, 2).slice(2, -2).replace(/^(\s*)"([a-zA-Z]+)":/gm, '$1$2:');
    const newEntry = `  ${d.seoPageId}: {${seoNl}${entryBody.split('\n').map(line => '  ' + line).join(seoNl)}${seoNl}  },`;
    if (entryPattern.test(seo)) seo = seo.replace(entryPattern, newEntry);
    else {
      insist(!seo.includes(`'${pagePath}'`) && !seo.includes(`"${pagePath}"`), 'Report URL is already registered under another key');
      insist(seo.includes('export const STATIC_SEO_PAGES = {'), 'SEO insertion anchor missing');
      seo = seo.replace('export const STATIC_SEO_PAGES = {', `export const STATIC_SEO_PAGES = {${seoNl}${newEntry}`);
    }
    // One set of download rules covers every month without exhausting the host's rule limit.
    for (const [file, type] of [['sigpik-report-*.zip', 'application/zip'], ['summary.csv', 'text/csv; charset=utf-8'], ['cohort.csv', 'text/csv; charset=utf-8'], ['coverage.json', 'application/json; charset=utf-8']]) {
      const url = `/report-assets/:channel/:sector/:month/:version/:locale/${file}`;
      if (!headers.split(/\r?\n/).includes(url)) headers += `\n${url}\n  Content-Type: ${type}\n  Content-Disposition: attachment; filename="${file.replace('*', ':splat')}"\n`;
    }
  }
  return { 'content/reports/registry.ts': registry, 'config/seo.ts': seo, 'public/_headers': headers };
}
async function ensureUnpublished(root, drafts) {
  const baseline = await runtimeBaseline(root);
  for (const { definition: { data: r } } of drafts) {
    const current = await bytesOrNull(path.join(root, `content/reports/${r.id}.v1.json`));
    const locked = await bytesOrNull(path.join(root, `content/reports/releases/${r.id}.v1.json`));
    insist(baseline?.editions[r.id] !== 'published' && !baseline?.files[`content/reports/releases/${r.id}.v1.json`], `${r.id}: published editions cannot be regenerated`);
    insist(!locked && (!current || JSON.parse(current).publication.status === 'review'), `${r.id}: published editions cannot be regenerated`);
    insist((await filesUnder(root, `public/report-assets/published/${r.sector}/${r.month}/v1`)).length === 0, `${r.id}: published asset address already exists`);
  }
}
async function verifyDraft(folder) {
  const manifest = await read(path.join(folder, 'draft.json')), { draftDigest, ...body } = manifest;
  insist(draftDigest === digest(body), 'Draft manifest was modified');
  insist(digest(await fingerprint(folder, Object.keys(manifest.files))) === digest(manifest.files), 'Draft files changed after preparation; regenerate and review again');
  for (const area of ['content', 'public', 'config']) {
    insist((await filesUnder(folder, area)).every(file => Object.hasOwn(manifest.files, file)), 'Unreviewed file in draft bundle');
  }
  return manifest;
}

export function selectDrafts(drafts, sector) {
  if (!sector) return drafts;
  const selected = drafts.filter(d => d.definition.data.sector === sector);
  insist(selected.length === 1, 'Unknown sector selection');
  return selected;
}
export async function generateDraft({ root = process.cwd(), input, sector }) {
  root = path.resolve(root);
  const raw = await fs.readFile(input), batch = JSON.parse(raw), drafts = selectDrafts(prepareBatch(batch), sector);
  assertCoverageReady(drafts);
  await ensureUnpublished(root, drafts);
  const originals = Object.fromEntries(await Promise.all(sharedFiles.map(async file => [file, await fs.readFile(within(root, file))])));
  const patched = patchRegistration(originals, drafts);
  const sourceFiles = [...sharedFiles];
  for (const { definition: { data: r } } of drafts) sourceFiles.push(`content/reports/${r.id}.v1.json`, ...await filesUnder(root, `public${reportAssetRoot(r)}`));
  const base = await fingerprint(root, sourceFiles);
  const generatorFiles = [...await filesUnder(codeRoot, 'scripts/reports'), 'content/reports/registry.ts', 'content/reports/presentation.ts', 'lib/reports/model.ts', 'lib/market-statistics.ts'].filter(file => /\.(?:mjs|ts|py)$/.test(file));
  generatorFiles.push('public/brand/sigpik-logo.png');
  const generator = await fingerprint(codeRoot, generatorFiles);
  const runtime = await renderingRuntime();
  const key = digest({ input: sha256(raw), base, generator, runtime }).slice(0, 20);
  const parent = path.join(root, 'work/reports/drafts'), prefix = `${batch.month}-${key}-`;
  await fs.mkdir(parent, { recursive: true });
  for (const name of (await fs.readdir(parent)).filter(name => name.startsWith(prefix))) {
    const folder = path.join(parent, name);
    if (await bytesOrNull(path.join(folder, 'FAILED.json'))) continue;
    if (await bytesOrNull(path.join(folder, 'draft.json'))) {
      await verifyDraft(folder); return { status: 'unchanged', folder };
    }
  }
  const guard = path.join(parent, `${batch.month}.running`), handle = await acquireDraftLock(guard);
  let temporary;
  try {
    temporary = await fs.mkdtemp(path.join(parent, prefix));
    for (const [file, bytes] of Object.entries(patched)) await put(temporary, file, bytes);
    for (const { definition: { data } } of drafts) await put(temporary, `content/reports/${data.id}.v1.json`, json(data));
    await buildMedia(drafts.map(d => edition(d.definition)), temporary);
    const py = pythonOptions();
    for (const { definition: { data: r } } of drafts) await run(py.executable, [script('render-assets.py'), '--input', `work/reports/${r.id}.render-input.json`, ...py.args], temporary);
    await run(py.executable, [script('verify-assets.py')], temporary);
    for (const { definition: { data: r } } of drafts) await run(py.executable, [script('prepare-release.py'), r.id], temporary);
    await put(temporary, 'coverage.json', json(drafts.map(d => d.coverage)));
    let review = `# ${batch.month} 报告草稿\n\n状态：待人工复核。未注册到网站、未发布。\n\n数据准备：${batch.preparedBy}\n\n口径：各赛道单独统计全球 Web 整站估计访问量；仅反映可比样本，不能累加为行业规模。\n\n`;
    for (const { definition: d, coverage } of drafts) {
      const r = d.data, payload = await read(path.join(temporary, `work/reports/${r.id}.render-input.json`));
      review += `## ${d.name['zh-CN']}\n\n候选 ${coverage.candidates}，可比 ${coverage.comparable}，排除 ${coverage.excluded.length}。\n\n`;
      for (const locale of ['zh-CN', 'en']) {
        const copy = payload.locales[locale], asset = `public${reportAssetRoot(r)}/${locale === 'en' ? 'en' : 'zh-cn'}`;
        review += `### ${copy.headline}\n\n${copy.findings.map(value => '- ' + value).join('\n')}\n\n![${d.name[locale]}](${asset}/overview-landscape.png)\n\n[横版 PNG](${asset}/overview-landscape.png) · [竖版 PNG](${asset}/overview-portrait.png) · [汇总 CSV](${asset}/summary.csv) · [域名 CSV](${asset}/cohort.csv) · [引用全文](${asset}/citation.txt)\n\n${copy.directionMethod}\n\n${copy.reuseText ?? ''}\n\n`;
      }
      review += `[五项指标核对表](work/reports/${r.id}.release-review.md)\n\n`;
    }
    review += '[SEO 文案与索引配置](config/seo.ts) · [样本覆盖与排除原因](coverage.json)\n\n请复核双语文案与所有图表，在 approval.json 中填写第二位复核者、确认日期，并逐项勾选五项指标。修改输入文案后需重新生成；旧签核不能用于新文件。\n';
    await put(temporary, 'REVIEW.md', review);
    const files = [...await filesUnder(temporary, 'content'), ...await filesUnder(temporary, 'public'), ...await filesUnder(temporary, 'config'), ...(await filesUnder(temporary, 'work/reports')).filter(file => /\.(json|md)$/.test(file) && !file.includes('/.matplotlib/')), 'coverage.json', 'REVIEW.md'];
    const installFiles = files.filter(file => /^(content|public|config)\//.test(file));
    const publishedFiles = installFiles.filter(file => file.startsWith('public/report-assets/previews/')).map(file => file.replace('/previews/', '/published/'));
    const baseline = { ...base, ...await fingerprint(root, [...installFiles, ...publishedFiles]) };
    insist(digest(await fingerprint(root, Object.keys(base))) === digest(base), 'Source workspace changed during generation');
    const manifest = { schemaVersion: 1, status: 'review', month: batch.month, preparedBy: batch.preparedBy.trim(), preparedOn: batch.preparedOn, inputSha256: sha256(raw), reportIds: drafts.map(d => d.definition.data.id), generator, runtime, baseline, installFiles, files: await fingerprint(temporary, files) };
    manifest.draftDigest = digest(manifest);
    await put(temporary, 'approval.json', json({ status: 'pending', draftDigest: manifest.draftDigest, reviewedBy: '', approvedOn: null, reports: Object.fromEntries(manifest.reportIds.map(id => [id, Object.fromEntries(CHECKS.map(key => [key, false]))])) }));
    // The completion manifest is written last. Consumers never accept a partial build.
    await put(temporary, 'draft.json', json(manifest));
    return { status: 'review', folder: temporary, draftDigest: manifest.draftDigest };
  } catch (error) {
    if (temporary) await put(temporary, 'FAILED.json', json({ status: 'failed', reason: error.message }));
    throw error;
  } finally { await handle.close(); await fs.unlink(guard); }
}

/** Resolve and validate a completed export without writing or rendering anything. */
export async function readCompletedExport(directory) {
  directory = path.resolve(directory);
  const completion = await read(path.join(directory, 'complete.json'));
  insist(completion.state === 'complete' && Array.isArray(completion.sectors), 'The upstream export has not completed');
  const sectors = [];
  for (const entry of completion.sectors) {
    const payload = {};
    for (const kind of ['snapshot', 'traffic']) {
      const reference = entry[kind];
      insist(reference && /^[a-f0-9]{64}$/.test(reference.sha256), 'Export reference needs an exact SHA-256');
      const bytes = await fs.readFile(within(directory, reference.file));
      insist(sha256(bytes) === reference.sha256, `Completed export changed: ${reference.file}`);
      payload[kind] = JSON.parse(bytes);
    }
    sectors.push({ sector: entry.sector, expectedDomains: entry.expectedDomains, ...payload,
      ...(Object.hasOwn(entry, 'editorial') ? { editorial: entry.editorial } : {}),
      ...(Object.hasOwn(entry, 'supplementalPeers') ? { supplementalPeers: entry.supplementalPeers } : {}),
      ...(Object.hasOwn(entry, 'templateVersion') ? { templateVersion: entry.templateVersion } : {}),
      ...(Object.hasOwn(entry, 'coverageExceptions') ? { coverageExceptions: entry.coverageExceptions } : {}),
      ...(Object.hasOwn(entry, 'scopeNotes') ? { scopeNotes: entry.scopeNotes } : {}),
      ...(Object.hasOwn(entry, 'reuse') ? { reuse: entry.reuse } : {}),
    });
  }
  const batch = { schemaVersion: completion.schemaVersion, state: completion.state, month: completion.month, preparedOn: completion.preparedOn, preparedBy: completion.preparedBy, ...(Object.hasOwn(completion, 'expectedSectors') ? { expectedSectors: completion.expectedSectors } : {}), sectors };
  prepareBatch(batch);
  return batch;
}

/** Upstream writes complete.json last; verified exports immediately produce review assets. */
export async function generateFromExport({ root = process.cwd(), directory, sector }) {
  const batch = await readCompletedExport(directory);
  const relative = `work/reports/inputs/${batch.month}-${digest(batch).slice(0, 20)}.json`;
  await put(root, relative, json(batch));
  return generateDraft({ root, input: within(root, relative), sector });
}

/** One completed export per market; a missing or invalid market never stops its peers. */
export async function generateExportQueue({ root = process.cwd(), directory, month, sectors = Object.keys(SECTORS), generate = generateFromExport }) {
  root = path.resolve(root); directory = path.resolve(directory);
  shiftMonth(month, 0);
  insist(Array.isArray(sectors) && sectors.length > 0 && new Set(sectors).size === sectors.length && sectors.every(sector => typeof sector === 'string' && Object.hasOwn(SECTORS, sector)), 'Queue requires unique configured sectors');
  const results = [];
  for (const sector of sectors) {
    const exportDirectory = within(directory, sector);
    try {
      const bytes = await bytesOrNull(path.join(exportDirectory, 'complete.json'));
      if (!bytes) { results.push({ sector, status: 'waiting', reason: 'No completion manifest yet' }); continue; }
      const completion = JSON.parse(bytes.toString('utf8'));
      if (completion.state !== 'complete') { results.push({ sector, status: 'waiting', reason: 'Upstream export is not complete' }); continue; }
      insist(completion.month === month, `${sector}: queue/export month mismatch`);
      insist(completion.schemaVersion === 2 && completion.expectedSectors?.length === 1 && completion.expectedSectors[0] === sector && completion.sectors?.length === 1 && completion.sectors[0]?.sector === sector, `${sector}: queue expects a schemaVersion 2 export for this market only`);
      const batch = await readCompletedExport(exportDirectory);
      assertCoverageReady(prepareBatch(batch));
      const existing = await bytesOrNull(within(root, `content/reports/${sector}-${month}.v1.json`));
      const baseline = await runtimeBaseline(root);
      if (baseline?.editions[`${sector}-${month}`] === 'published' || (existing && JSON.parse(existing.toString('utf8')).publication?.status === 'published')) {
        if (!baseline) await run(process.execPath, [script('check-release-locks.mjs')], root);
        results.push({ sector, status: 'published', reason: 'Existing published edition retained' }); continue;
      }
      results.push({ sector, ...await generate({ root, directory: exportDirectory, sector }) });
    } catch (error) {
      results.push({ sector, status: 'blocked', reason: error.message });
    }
  }
  const completed = results.filter(r => ['review', 'unchanged', 'published'].includes(r.status)).length;
  const status = completed === results.length ? 'complete' : completed ? 'partial' : results.some(r => r.status === 'blocked') ? 'blocked' : 'waiting';
  const receipt = { schemaVersion: 1, month, status, deployed: false, results };
  // Each invocation keeps its own receipt, so one queue cannot erase another's outcome.
  const receipts = within(root, 'work/reports/queue-runs');
  await fs.mkdir(receipts, { recursive: true });
  const folder = await fs.mkdtemp(path.join(receipts, `${month}-`));
  await fs.writeFile(path.join(folder, 'receipt.json'), json(receipt));
  return { ...receipt, receiptPath: path.join(folder, 'receipt.json') };
}

/** Verify and install an approved bundle locally. Production deployment remains separate. */
export async function releaseDraft({ root = process.cwd(), folder, approvalPath, build = true }) {
  root = path.resolve(root); folder = path.resolve(folder);
  insist(!await runtimeBaseline(root) && process.env.REPORT_DRAFT_ONLY !== '1', 'This runtime only prepares drafts; release in the reviewed website workspace');
  const manifest = await verifyDraft(folder), approval = await read(approvalPath ?? path.join(folder, 'approval.json'));
  validateApproval(approval, manifest);
  if (manifest.runtime) insist(digest(manifest.runtime) === digest(await renderingRuntime()), 'Rendering runtime differs from the reviewed draft');
  insist(digest(await fingerprint(codeRoot, Object.keys(manifest.generator))) === digest(manifest.generator), 'Generator changed since review; regenerate the draft');
  insist(digest(await fingerprint(root, Object.keys(manifest.baseline))) === digest(manifest.baseline), 'Workspace changed since preparation; regenerate the draft');
  const drafts = await Promise.all(manifest.reportIds.map(async id => ({ definition: { data: await read(within(folder, `content/reports/${id}.v1.json`)) } })));
  await ensureUnpublished(root, drafts);
  const allowed = file => sharedFiles.includes(file) || drafts.some(({ definition: { data: r } }) => file === `content/reports/${r.id}.v1.json` || file.startsWith(`public${reportAssetRoot(r)}/`));
  insist(manifest.installFiles.every(file => allowed(file) && Object.hasOwn(manifest.files, file) && Object.hasOwn(manifest.baseline, file)), 'Unexpected installation target');
  await run(process.execPath, [script('check-release-locks.mjs')], root);
  const guard = path.join(root, 'work/reports/release.running');
  await fs.mkdir(path.dirname(guard), { recursive: true });
  const handle = await fs.open(guard, 'wx');
  const original = new Map(), installed = [];
  try {
    await handle.writeFile(json({ pid: process.pid, draftDigest: manifest.draftDigest }));
    const release = await fs.mkdtemp(path.join(root, 'work/reports/releasing-'));
    for (const file of Object.keys(manifest.files)) await put(release, file, await fs.readFile(within(folder, file)));
    const py = pythonOptions();
    await run(py.executable, [script('verify-assets.py')], release);
    for (const id of manifest.reportIds) await run(py.executable, [script('prepare-release.py'), id, '--apply', '--reviewed-by', approval.reviewedBy.trim(), '--published-on', approval.approvedOn], release);
    await run(py.executable, [script('verify-assets.py')], release);
    const publishedFiles = manifest.installFiles.filter(file => file.startsWith('public/report-assets/previews/')).map(file => file.replace('/previews/', '/published/'));
    insist(publishedFiles.every(file => manifest.baseline[file] === null), 'Published assets cannot overwrite existing addresses');
    const installFiles = [...manifest.installFiles, ...publishedFiles, ...manifest.reportIds.map(id => `content/reports/releases/${id}.v1.json`)];
    insist(digest(await fingerprint(root, Object.keys(manifest.baseline))) === digest(manifest.baseline), 'Workspace changed while release was being prepared');
    await ensureUnpublished(root, drafts);
    for (const file of installFiles) {
      original.set(file, await bytesOrNull(within(root, file)));
      installed.push(file);
      await put(root, file, await fs.readFile(within(release, file)));
    }
    await run(process.execPath, [script('check-release-locks.mjs')], root);
    if (build) {
      await run(process.execPath, ['node_modules/typescript/bin/tsc', '--noEmit', '--incremental', 'false'], root);
      await run(process.execPath, ['scripts/generate-route-css.mjs', '--check'], root);
      await run(process.execPath, ['node_modules/vite/bin/vite.js', 'build'], root);
    }
    await put(root, `work/reports/${manifest.month}.release-receipt.json`, json({ draftDigest: manifest.draftDigest, approval, status: 'locally-released', deployed: false, buildVerified: build, files: await fingerprint(root, installFiles) }));
    return { status: 'locally-released', deployed: false, reportIds: manifest.reportIds };
  } catch (error) {
    for (const file of installed.reverse()) {
      const before = original.get(file);
      if (before === null) await fs.unlink(within(root, file)).catch(e => { if (e.code !== 'ENOENT') throw e; }); else await put(root, file, before);
    }
    throw error;
  } finally { await handle.close(); await fs.unlink(guard); }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: { input: { type: 'string' }, sector: { type: 'string' }, sectors: { type: 'string' }, month: { type: 'string' }, directory: { type: 'string' }, draft: { type: 'string' }, approval: { type: 'string' }, receipt: { type: 'string' }, help: { type: 'boolean' } } });
  try {
    if (values.help) console.log('from-export --directory monthly/<month>/<sector> [--sector music-generation] | from-exports --directory monthly/<month> --month YYYY-MM [--sectors video-generation,image-generation,music-generation] | generate --input ready.json [--sector music-generation] | release --draft work/reports/drafts/<id> [--approval approval.json]\nSchema 2 declares expectedSectors and supports independently completed markets. Draft generation does not publish or deploy.');
    else if (positionals[0] === 'from-exports' && values.directory && values.month) {
      const result = await generateExportQueue({ directory: values.directory, month: values.month, sectors: values.sectors?.split(',').map(s => s.trim()) });
      // Explicit machine output keeps logs separate from the scheduler contract.
      if (values.receipt) await fs.writeFile(path.resolve(values.receipt), json(result), { flag: 'wx' });
      console.log(json(result));
      if (result.results.some(r => r.status === 'blocked')) process.exitCode = 2;
    }
    else if (positionals[0] === 'from-export' && values.directory) console.log(json(await generateFromExport({ directory: values.directory, sector: values.sector })));
    else if (positionals[0] === 'generate' && values.input) console.log(json(await generateDraft({ input: path.resolve(values.input), sector: values.sector })));
    else if (positionals[0] === 'release' && values.draft) console.log(json(await releaseDraft({ folder: values.draft, approvalPath: values.approval })));
    else throw Error('Use --help for the report pipeline commands');
  } catch (error) { console.error(json({ status: 'failed', reason: error.message })); process.exitCode = 1; }
}
