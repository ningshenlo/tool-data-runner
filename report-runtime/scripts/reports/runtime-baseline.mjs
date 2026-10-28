// The server bundle carries website hashes, never unpublished report data.
import fs from 'node:fs/promises';
import path from 'node:path';
import { digest, insist } from './pipeline-core.mjs';

export async function runtimeBaseline(root) {
  const bytes = await fs.readFile(path.join(root, 'runtime-baseline.json')).catch(error => {
    if (error.code === 'ENOENT') return null;
    throw error;
  });
  if (!bytes) return null;
  const { manifestDigest, ...body } = JSON.parse(bytes);
  insist(body.schemaVersion === 1 && body.mode === 'draft-only' && manifestDigest === digest(body), 'Invalid runtime baseline');
  insist(body.files && body.editions, 'Incomplete runtime baseline');
  for (const [file, hash] of Object.entries(body.files)) {
    insist(/^(content\/reports\/|public\/report-assets\/)/.test(file) && !file.includes('\\') && !file.split('/').some(part => !part || part === '.' || part === '..') && /^[a-f0-9]{64}$/.test(hash), 'Invalid website baseline entry');
  }
  for (const [id, status] of Object.entries(body.editions)) {
    insist(/^[a-z-]+-\d{4}-\d{2}$/.test(id) && ['review', 'published'].includes(status) && Object.hasOwn(body.files, `content/reports/${id}.v1.json`), 'Invalid edition baseline');
  }
  return body;
}
