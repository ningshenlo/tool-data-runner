import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { REPORTS, reportPath, reportAssetBase, reportZipName, reportUrl } from '../../content/reports/registry.ts';
const origin = process.argv[2] ?? 'http://127.0.0.1:3000';
const devAssets = process.argv.includes('--dev-assets');
const results = [];
for (const locale of ['en','zh-CN']) {
  const prefix = locale === 'zh-CN' ? '/zh-cn' : '';
  const overview = await fetch(origin + prefix + '/reports', {redirect:'manual'});
  assert.equal(overview.status,200);
  const body = await overview.text();
  for (const r of REPORTS) assert.ok(body.includes(prefix+reportPath(r)));
  assert.equal((body.match(/<h1\b/g)??[]).length,1);
  results.push({path:prefix+'/reports',status:overview.status});
  for (const report of REPORTS) {
    const path = prefix + reportPath(report);
    const response = await fetch(origin+path); const html = await response.text();
    assert.equal(response.status,200,path);
    assert.equal((html.match(/<h1\b/g)??[]).length,1,path);
    assert.ok(!html.toLowerCase().includes('similarweb'),path);
    assert.ok(html.includes(reportUrl(report,locale)));
    assert.ok(html.includes(`https://sigpik.com${reportAssetBase(report,locale)}/social-card.png`));
    const metas = html.match(/<meta\b[^>]*>/g)??[];
    assert.equal(metas.filter(m=>m.includes('property="og:image"')).length,1);
    assert.equal(metas.filter(m=>m.includes('name="twitter:image"')).length,1);
    const links=html.match(/<link\b[^>]*>/g)??[];
    assert.equal(links.filter(l=>l.includes('rel="canonical"')).length,1);
    assert.ok(metas.some(m=>m.includes('name="robots"')&&m.includes(report.data.publication.status==='published'?'index,follow':'noindex,follow')));
    assert.ok(html.includes('id="coverage"'));
    for (const filename of [reportZipName(report),'summary.csv','cohort.csv','coverage.json','citation.txt','social-card.png']) {
      const asset = reportAssetBase(report,locale)+'/'+filename;
      const download = await fetch(origin+asset);
      assert.equal(download.status,200,asset);
      if (!devAssets) assert.match(download.headers.get('cache-control')??'',report.data.publication.status==='review'?/no-store/:/immutable/);
      if (filename.endsWith('.zip')) {
        assert.match(download.headers.get('content-type')??'',/application\/zip/);
        if (!devAssets) assert.ok(download.headers.get('content-disposition')?.includes(`filename="${filename}"`));
      }
      if (!devAssets && filename.endsWith('.csv')) assert.match(download.headers.get('content-disposition')??'',/attachment/);
      const bytes = Buffer.from(await download.arrayBuffer());
      const local = await fs.readFile(new URL('../../public'+asset,import.meta.url));
      assert.equal(createHash('sha256').update(bytes).digest('hex'),createHash('sha256').update(local).digest('hex'));
    }
    results.push({path,status:response.status,count:report.summary.count,assets:'verified'});
  }
}
for (const path of ['/reports/unknown/2026-08','/reports/music-generation/2026-09']) assert.equal((await fetch(origin+path)).status,404,path);
await fs.writeFile(new URL('../../work/reports/preview-verification.json',import.meta.url),JSON.stringify({checkedAt:new Date().toISOString(),origin,hostingHeadersVerified:!devAssets,results},null,2));
console.log(JSON.stringify(results,null,2));
