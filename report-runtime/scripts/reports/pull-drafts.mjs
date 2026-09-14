import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { digest, insist, sha256, json } from './pipeline-core.mjs';

export const KEY = /^[a-f0-9]{64}$/;
const MAX_FILE = 16 * 1024 * 1024;
const ROOT = fileURLToPath(new URL('../../', import.meta.url));

export function safeFile(root, relative) {
  insist(typeof relative === 'string' && !relative.includes(':') && !relative.includes('\\') && !relative.split('/').some(p => !p || p === '.' || p === '..') && !path.isAbsolute(relative), 'invalid_draft_path');
  const file = path.resolve(root, relative);
  insist(file.startsWith(path.resolve(root) + path.sep), 'invalid_draft_path');
  return file;
}
export function verifyManifest(manifest, key) {
  const { draftDigest, ...body } = manifest;
  insist(KEY.test(key) && draftDigest === key && digest(body) === key && body.schemaVersion === 1 && body.status === 'review', 'invalid_draft_manifest');
  insist(body.reportIds?.length === 1 && /^[a-z]+(?:-[a-z]+)*-\d{4}-(0[1-9]|1[0-2])$/.test(body.reportIds[0]), 'invalid_draft_identity');
  insist(body.files && Object.keys(body.files).length > 0 && Object.keys(body.files).length <= 500, 'invalid_draft_files');
  for (const [name, hash] of Object.entries(body.files)) {
    safeFile(ROOT, name);
    insist(KEY.test(hash), 'invalid_draft_hash');
  }
  return manifest;
}
async function atomic(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temporary = file + '.' + process.pid + '.tmp';
  await fs.writeFile(temporary, json(value));
  await fs.rename(temporary, file);
}
async function limitedBody(response) {
  insist(Number(response.headers.get('content-length') ?? 0) <= MAX_FILE, 'draft_file_too_large');
  const chunks = []; let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    insist(bytes <= MAX_FILE, 'draft_file_too_large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
export async function readCatalog(root = ROOT) {
  return JSON.parse(await fs.readFile(path.join(root, 'work/reports/remote/catalog.json'), 'utf8'));
}
export async function readDraftFile(root, key, relative) {
  insist(KEY.test(key), 'invalid_draft_key');
  const directory = path.join(root, 'work/reports/remote', key);
  const manifest = verifyManifest(JSON.parse(await fs.readFile(path.join(directory, 'draft.json'), 'utf8')), key);
  insist(Object.hasOwn(manifest.files, relative), 'file_not_in_draft');
  const file = safeFile(directory, relative);
  insist(!(await fs.lstat(file)).isSymbolicLink(), 'invalid_draft_path');
  const data = await fs.readFile(file);
  insist(data.length <= MAX_FILE && sha256(data) === manifest.files[relative], 'draft_file_changed');
  return data;
}

export async function pullDrafts({ root = ROOT, fetchImpl = fetch } = {}) {
  // Kept outside Vite's public assets and module graph. Never send to the browser.
  const config = JSON.parse(await fs.readFile(path.join(root, 'work/reports/remote-config.json'), 'utf8'));
  const endpoint = new URL(config.endpoint);
  insist(endpoint.protocol === 'https:' && !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash && endpoint.pathname === '/__sigpik_report_review/v1', 'invalid_review_endpoint');
  insist(typeof config.token === 'string' && config.token.length >= 40, 'invalid_review_credentials');
  const request = async relative => {
    const response = await fetchImpl(endpoint.href + relative, { headers: { Authorization: 'Bearer ' + config.token }, redirect: 'error', signal: AbortSignal.timeout(30_000) });
    insist(response.ok, `review_service_${response.status}`);
    return limitedBody(response);
  };
  const catalog = JSON.parse((await request('/catalog')).toString('utf8'));
  insist(catalog.schemaVersion === 1 && Array.isArray(catalog.reports) && catalog.reports.length <= 300, 'invalid_review_catalog');
  const parent = path.join(root, 'work/reports/remote');
  await fs.mkdir(parent, { recursive: true });
  const reports = [];
  for (const source of catalog.reports) {
    const row = { ...source, available: false };
    if (['review', 'unchanged'].includes(row.status)) {
      let temporary;
      try {
        const key = row.draftDigest;
        insist(KEY.test(key), 'invalid_draft_key');
        const folder = path.join(parent, key);
        const manifest = verifyManifest(JSON.parse((await request(`/drafts/${key}/manifest`)).toString('utf8')), key);
        insist(manifest.reportIds[0] === `${row.sector}-${row.month}`, 'draft_identity_mismatch');
        const exists = await fs.access(path.join(folder, 'draft.json')).then(() => true, () => false);
        if (!exists) {
          temporary = await fs.mkdtemp(path.join(parent, '.pull-'));
          const entries = Object.entries(manifest.files); let total = 0;
          // Four requests at a time keep memory and server concurrency bounded.
          for (let offset = 0; offset < entries.length; offset += 4) {
            const results = await Promise.allSettled(entries.slice(offset, offset + 4).map(async ([name, hash]) => {
              const bytes = await request(`/drafts/${key}/files/${name.split('/').map(encodeURIComponent).join('/')}`);
              insist(sha256(bytes) === hash, 'download_hash_mismatch');
              total += bytes.length; insist(total <= 128 * 1024 * 1024, 'draft_bundle_too_large');
              const file = safeFile(temporary, name);
              await fs.mkdir(path.dirname(file), { recursive: true });
              await fs.writeFile(file, bytes);
            }));
            for (const result of results) if (result.status === 'rejected') throw result.reason;
          }
          await fs.writeFile(path.join(temporary, 'draft.json'), json(manifest));
          await fs.rename(temporary, folder); temporary = undefined;
        }
        // Cache hits still validate every downloaded file; never bless edited data.
        for (const name of Object.keys(manifest.files)) await readDraftFile(root, key, name);
        row.available = true;
      } catch (error) {
        row.syncError = error?.message?.match(/^[a-z_0-9]+$/)?.[0] ?? 'draft_sync_failed';
        if (temporary) await fs.writeFile(path.join(temporary, 'FAILED.json'), json({ error: row.syncError }));
      }
    }
    reports.push(row);
  }
  const result = { schemaVersion: 1, configured: true, checkedAt: new Date().toISOString(), reports,
    marketInventory: catalog.marketInventory ?? [], readiness: catalog.readiness ?? [], deployed: false };
  await atomic(path.join(parent, 'catalog.json'), result);
  return result;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  try {
    const result = await pullDrafts();
    console.log(json({ checkedAt: result.checkedAt, reports: result.reports.map(({ month, sector, status, available, syncError }) => ({ month, sector, status, available, syncError })), deployed: false }));
    if (result.reports.some(row => row.syncError)) process.exitCode = 2;
  } catch (error) { console.error('Draft sync unavailable: ' + (error?.message?.match(/^[a-z_0-9]+$/)?.[0] ?? 'check_private_remote_config')); process.exitCode = 1; }
}
