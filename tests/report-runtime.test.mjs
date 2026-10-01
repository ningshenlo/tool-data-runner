import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { runtimeBaseline } from '../report-runtime/scripts/reports/runtime-baseline.mjs';
import { digest } from '../report-runtime/scripts/reports/pipeline-core.mjs';
import { releaseDraft } from '../report-runtime/scripts/reports/pipeline.mjs';

test('data-free baseline detects tampering and refuses path escapes', async t => {
  const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'report-baseline-'));
  t.after(() => fs.rm(folder, { recursive: true, force: true }));
  const id = 'music-generation-2025-08', file = `content/reports/${id}.v1.json`;
  const body = { schemaVersion: 1, mode: 'draft-only', files: { [file]: 'a'.repeat(64) }, editions: { [id]: 'published' } };
  const write = value => fs.writeFile(path.join(folder, 'runtime-baseline.json'), JSON.stringify({ ...value, manifestDigest: digest(value) }));
  assert.equal(await runtimeBaseline(folder), null);
  await write(body);
  assert.equal((await runtimeBaseline(folder)).editions[id], 'published');
  await assert.rejects(releaseDraft({ root: folder, folder }), /only prepares drafts/);
  await fs.writeFile(path.join(folder, 'runtime-baseline.json'), JSON.stringify({ ...body, manifestDigest: 'b'.repeat(64) }));
  await assert.rejects(runtimeBaseline(folder), /Invalid runtime baseline/);
  await write({ ...body, files: { ...body.files, 'content/reports/../../secret': 'a'.repeat(64) } });
  await assert.rejects(runtimeBaseline(folder), /Invalid website baseline/);
  await write({ ...body, files: {} });
  await assert.rejects(runtimeBaseline(folder), /Invalid edition baseline/);
});
