/** Offline rasterization of the exact SVG template used by the report webpage. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { initWasm, Resvg } from '@resvg/resvg-wasm';
import { reportMediaSvg } from '../../lib/reports/web-media.ts';
import { edition } from '../../content/reports/presentation.ts';
const require=createRequire(import.meta.url);
const input=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const r=input.report;
const source=JSON.parse(await fs.readFile(`content/reports/${r.id}.${r.version}.json`,'utf8'));
if(JSON.stringify(source)!==JSON.stringify(r)||r.publication.status!=='review')throw Error('Only current review editions may be rendered');
try{await fs.access(`content/reports/releases/${r.id}.${r.version}.json`);throw Error('Frozen edition');}catch(e){if(e.code!=='ENOENT')throw e;}
const report=edition({data:r,seoPageId:input.seoPageId,featuredDomain:input.featuredDomain,...Object.fromEntries(['name','scopeName','headline'].map(key=>[key,Object.fromEntries(['en','zh-CN'].map(locale=>[locale,input.locales[locale][key]]))]))});
await initWasm(await fs.readFile(require.resolve('@resvg/resvg-wasm/index_bg.wasm')));
const fontPath=process.argv[3]||process.env.REPORT_FONT||'C:/Windows/Fonts/msyh.ttc';
const font=await fs.readFile(fontPath);
const options={font:{fontBuffers:[font],defaultFontFamily:'Microsoft YaHei',sansSerifFamily:'Microsoft YaHei',loadSystemFonts:false}};
for(const locale of ['en','zh-CN']){
 const folder=`public/report-assets/previews/${r.sector}/${r.month}/${r.version}/${locale==='zh-CN'?'zh-cn':'en'}`;
 await fs.mkdir(folder,{recursive:true});
 for(const kind of ['overview','trend','contribution'])for(const portrait of [false,true]){
  const svg=reportMediaSvg(report,locale,kind,portrait),renderer=new Resvg(svg,options),image=renderer.render();
  await fs.writeFile(path.join(folder,`${kind}-${portrait?'portrait':'landscape'}.png`),image.asPng());image.free();renderer.free();
 }
 const svg=reportMediaSvg(report,locale,'overview').replace('width="1920" height="1080"','width="1200" height="630"');
 const renderer=new Resvg(svg,options),image=renderer.render();await fs.writeFile(path.join(folder,'social-card.png'),image.asPng());image.free();renderer.free();
}
console.log(`${r.id}: rendered shared web SVG assets`);
