import { localized, reportUrl, type ReportEdition } from '../../content/reports/presentation.ts';
export type MediaChart = 'overview' | 'trend' | 'contribution';
const escape = (value: string | number) => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!));
/** Pure SVG shared by inline previews and browser PNG exports. No external assets. */
export function reportMediaSvg(edition: ReportEdition, locale: string, kind: MediaChart, portrait = false) {
 const zh=locale==='zh-CN', s=edition.summary, r=edition.data;
 const w=portrait?1080:1920,h=portrait?1350:1080,p=portrait?64:100,inner=w-2*p;
 const ink='#192027',muted='#64748b',orange='#cf510b',green='#287b69',rose='#b64a60',line='#e7ebef';
 const text=(x:number,y:number,value:string|number,size=26,color=ink,weight=400,anchor='start')=>`<text x="${x}" y="${y}" font-size="${size}" fill="${color}" font-weight="${weight}" text-anchor="${anchor}">${escape(value)}</text>`;
 const rule=(y:number)=>`<path d="M${p} ${y}H${w-p}" stroke="${line}"/>`;
 const num=(n:number)=>zh?(n>=1e8?`${(n/1e8).toFixed(2)}亿`:`${(n/1e4).toFixed(1)}万`):`${(n/1e6).toFixed(2)}M`;
 const pct=(n:number)=>`${n>0?'+':''}${(n*100).toFixed(1)}%`;
 const titles=zh?{overview:'看清本月市场变化',trend:'访问趋势与样本涨跌',contribution:'哪些产品带来访问增减？'}:{overview:'The month in perspective',trend:'Traffic trend & sample breadth',contribution:'Who drove the change?'};
 let body=text(p,80,'SIGPIK / '+(zh?'市场研究':'MARKET RESEARCH'),24,orange,650)+text(w-p,80,`${r.month} · ${r.version}`,24,muted,400,'end')+text(p,155,`${localized(edition.name,locale)} · ${zh?'月度报告':'Monthly report'}`,28,muted)+text(p,225,titles[kind],portrait?44:54,ink,700)+rule(267);
 if(kind==='overview'){
  const metrics=[{value:num(s.current),label:zh?'样本月访问量':'Monthly sample visits',detail:`${s.count} ${zh?'个可比域名':'comparable domains'}`,color:ink},{value:pct(s.growth),label:zh?'样本月环比':'Month-over-month change',detail:`${r.baselineMonth} → ${r.month}`,color:s.growth<0?rose:green},{value:`${(s.top3Share*100).toFixed(1)}%`,label:zh?'头部三家访问份额':'Top-three visit share',detail:zh?'按本月访问量排序':'Ranked by current visits',color:orange}];
  metrics.forEach((m,i)=>{const x=portrait?p:p+i*inner/3,y=portrait?355+i*220:440;body+=text(x,y,m.value,portrait?70:86,m.color,650)+text(x,y+62,m.label,30,ink,500)+text(x,y+106,m.detail,23,muted);});
  body+=text(p,portrait?1060:760,zh?`增长 ${s.growing} / 下降 ${s.declining} / 持平 ${s.unchanged}`:`Growing ${s.growing} / Declining ${s.declining} / Flat ${s.unchanged}`,28,muted);
 }else if(kind==='trend'){
  const vals=r.history.map(v=>v.visits),lo=Math.min(...vals),hi=Math.max(...vals),span=Math.max(hi-lo,hi*.08,1),top=370,bottom=portrait?820:650;
  body+=text(p,320,zh?'估计 Web 访问量 · 纵轴非零起点':'Estimated Web visits · nonzero baseline',22,muted);
  [0,.5,1].forEach(t=>{const y=bottom-t*(bottom-top);body+=`<path d="M${p} ${y}H${w-p}" stroke="${line}"/>`;});
  const points=vals.map((v,i)=>({x:p+60+i*(inner-120)/Math.max(1,vals.length-1),y:bottom-30-(v-lo)/span*(bottom-top-80)}));
  body+=`<path d="${points.map((q,i)=>`${i?'L':'M'}${q.x} ${q.y}`).join(' ')}" fill="none" stroke="${orange}" stroke-width="5" stroke-linejoin="round"/>`;
  points.forEach((q,i)=>{body+=`<circle cx="${q.x}" cy="${q.y}" r="7" fill="${orange}"/>`+text(q.x,q.y-24,num(vals[i]!),25,ink,600,'middle')+text(q.x,bottom+46,r.history[i]!.month,23,muted,400,'middle');});
  const by=portrait?990:800;
  body+=text(p,by-30,zh?`${s.growing} 增长 · ${s.declining} 下降 · ${s.unchanged} 持平`:`${s.growing} growing · ${s.declining} declining · ${s.unchanged} flat`,26,ink,500);
  let x=p;[[s.growing,green],[s.declining,rose],[s.unchanged,'#cbd5e1']].forEach(([count,color])=>{const width=Number(count)/s.count*inner;body+=`<rect x="${x}" y="${by}" width="${width}" height="10" fill="${color}"/>`;x+=width;});
 }else{
  const rows=[...s.positive.slice(0,3),...s.negative.slice(0,3)],max=Math.max(...rows.map(v=>Math.abs(v.contributionPp)),.01),zero=p+inner*.63,half=inner*.23;
  body+=text(p,320,zh?'对样本整体环比的百分点贡献（pp）':'Contribution to sample growth (percentage points)',22,muted);
  const step=portrait?110:76,start=400;
  body+=`<path d="M${zero} 353V${start+rows.length*step-35}" stroke="${line}"/>`;
  rows.forEach((row,i)=>{const y=start+i*step,v=row.contributionPp,bw=Math.abs(v)/max*half,c=v<0?rose:green;body+=text(p,y+8,row.domain,portrait?23:27,ink,500)+`<rect x="${v<0?zero-bw:zero}" y="${y-20}" width="${bw}" height="32" rx="3" fill="${c}"/>`+text(v<0?zero-bw-12:zero+bw+12,y+6,`${v>0?'+':''}${v.toFixed(2)}`,22,c,600,v<0?'end':'start');});
  body+=text(p,portrait?1130:910,zh?'正、负贡献各取前三；不代表全部样本。':'Top three positive and negative contributors; not the full cohort.',21,muted);
 }
 body+=rule(h-140)+text(p,h-103,zh?`Sigpik 市场观测 · ${r.month} · ${s.count} 个可比域名 · ${r.publication.status==='review'?'预览版':'已发布'}`:`Sigpik Market Observations · ${r.month} · ${s.count} comparable domains · ${r.publication.status}`,21,muted)+text(p,h-65,zh?'第三方整站访问估计；样本范围不等于行业规模、用户数或收入。':'Third-party whole-site estimates; not industry totals, users or revenue.',portrait?19:21,muted)+text(p,h-28,reportUrl(edition,locale),portrait?17:20,muted);
 return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escape(titles[kind])}" font-family="Arial, Microsoft YaHei, sans-serif"><title>${escape(titles[kind])}</title><rect width="${w}" height="${h}" fill="white"/>${body}</svg>`;
}

