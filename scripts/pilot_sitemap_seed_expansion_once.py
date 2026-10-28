"""One-off 100-seed US/en pilot; immutable plan, single-seed receipts, no paid retries."""
import argparse, collections, hashlib, json, math, os, random, sqlite3, time, urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'reports/sitemap-keywords-once-20261012'
OUT = SOURCE / 'seed-pilot-100-us-en'
ENDPOINT = 'https://api.dataforseo.com/v3/keywords_data/google_ads/keywords_for_keywords/live'
UNIT = Decimal('0.09')
BUDGET = Decimal('9.00')
VERSION = 'sitemap-seed-pilot-100-us-en-v1'
# Experimental allocation only; not a production quota or natural demand taxonomy.
FAMILIES = [
('image_generate', [
 ('image generation','baseline',''),('ai 360 skybox generator','deliverable','360 skybox'),('ai action figure generator','visual_target','action figure'),('2d anime ai generator','style','2d anime'),('ai watercolor generator','style','watercolor')]),
('background_remove', [
 ('background remover','baseline',''),('car background remover','object','car'),('bulk background remover','workflow','bulk'),('background remover api','interface','api'),('free background remover','constraint','free')]),
('object_remove', [
 ('remove object from photo','baseline',''),('remove fence from photo','object','fence'),('remove power lines from photo','object','power lines'),('remove glasses from photo','object','glasses'),('remove object from image','synonym_control','')]),
('image_upscale', [
 ('image upscaler','baseline',''),('anime image upscaler','style','anime'),('batch upscale images','workflow','batch'),('printing image upscaling api','purpose_interface','printing;api'),('free image upscaler','constraint','free')]),
('image_style', [
 ('image style transfer','baseline',''),('photo to watercolor','style','watercolor'),('photo to charcoal sketch converter','style','charcoal sketch'),('photo to chinese ink painting','style','chinese ink painting'),('convert to impressionism','style','impressionism')]),
('headshot', [
 ('ai headshot generator','baseline',''),('actor headshot generator','subject_context','actor'),('linkedin headshot generator','platform_mention','linkedin'),('lawyer headshot generator','subject_context','lawyer'),('executive headshot generator','subject_context','executive')]),
('image_colorize', [
 ('colorize images','baseline',''),('colorize old photos','input_condition','old photos'),('colorize manga','object','manga'),('comic strip colorizer','object','comic strip'),('image colorization api','interface','api')]),
('face_swap_image', [
 ('face swap','baseline',''),('batch face swap','workflow','batch'),('group photo face swap','input_condition','group photo'),('cartoon face swap','style','cartoon'),('headshot face swap','input_condition','headshot')]),
('clothes_try', [
 ('clothing try on','baseline',''),('virtual dress try on','object','dress'),('virtual wedding dress try on','object','wedding dress'),('virtual suit try on','object','suit'),('virtual tshirt try on','object','tshirt')]),
('image_outpaint', [
 ('outpainting','baseline',''),('ai uncrop image','synonym_control',''),('ai outpainting free','constraint','free'),('ai background expander','target_variant','background'),('expand image','synonym_control','')]),
('image_video', [
 ('image to video','baseline',''),('product photo to video','input_condition','product photo'),('interior render to video','input_condition','interior render'),('animate old photos','input_condition','old photos'),('image to video api','interface','api')]),
('text_video', [
 ('text to video','baseline',''),('book chapter to video','input_condition','book chapter'),('text to music video','deliverable','music video'),('text to cartoon video','style','cartoon'),('text to video api','interface','api')]),
('video_generate', [
 ('ai video generator','baseline',''),('ai ugc ad video generator','purpose','ugc ad'),('ai claymation generator','style','claymation'),('15 second ai video generator','duration','15 second'),('free ai video generator no watermark','constraint','free;no watermark')]),
('video_translate', [
 ('video translation','baseline',''),('instagram video translator','platform_mention','instagram'),('real time video translation','capability','real time'),('translate arabic video to english','language_direction','arabic;english'),('translate spanish video to english','language_direction','spanish;english')]),
('audio_transcribe', [
 ('transcribe audio to text','baseline',''),('medical speech to text','subject_context','medical'),('phone call transcription','input_condition','phone call'),('podcast transcription','input_condition','podcast'),('zoom speech to text','platform_mention','zoom')]),
('text_speech', [
 ('text to speech','baseline',''),('text to speech api','interface','api'),('text to speech with emotion','capability','emotion'),('tiktok text to speech','platform_mention','tiktok'),('xhosa text to speech','language','xhosa')]),
('lip_sync', [
 ('lip sync','baseline',''),('lip sync api','interface','api'),('singing lip sync ai','capability','singing'),('multi character lip sync ai','capability','multi character'),('pet lip sync','object','pet')]),
('voice_clone', [
 ('voice cloning','baseline',''),('arabic voice cloning','language','arabic'),('japanese voice cloning','language','japanese'),('spanish voice cloning','language','spanish'),('ai voice cloning with emotion','capability_review','emotion')]),
]
EXPLORATION = [
 ('ai consistent character generator','image_generate','consistent character'),
 ('ai handwritten font generator','image_generate','handwritten font'),
 ('ai pbr map generator','image_generate','pbr map'),
 ('ai motion control video generator','video_generate','motion control'),
 ('ai video inpainting','video_generate','inpainting'),
 ('ai product staging generator','image_generate','product staging'),
 ('ai product video generator for spatial product ads','video_generate','spatial product ads'),
 ('ai camera movement effect','video_generate','camera movement'),
 ('ai shadow remover','object_remove','shadow'),
 ('ai character design generator','image_generate','character design'),
]

def now(): return datetime.now(timezone.utc).isoformat()
def sha(data): return hashlib.sha256(data).hexdigest()
def load(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)
def normal(word): return ' '.join(word.lower().split())
def log(event, **data): print(json.dumps({'at':now(),'event':event,**data},ensure_ascii=False),flush=True)

def prepare():
    clusters = load(SOURCE/'seed-clusters/clusters.json')
    member_group = {m['keyword']:g['id'] for g in clusters for m in g['members']}
    reviews = {x['keyword']:x for x in load(SOURCE/'seed-clusters/needs-review.json')}
    source = {}
    with (SOURCE/'ai-relevance/judgments.jsonl').open(encoding='utf-8-sig') as handle:
        for line in handle:
            item=json.loads(line)
            if item['status']=='strong': source[item['keyword']]=item
    titles={g['id']:g['title'] for g in clusters}
    anchors={family:rows[0][0] for family,rows in FAMILIES}
    seeds=[]
    def add(keyword,family,role,modifiers,cohort):
        assert keyword in source, f'not_existing_strong_word:{keyword}'
        assert len(keyword)<=80 and keyword.isascii()
        assert all(value in keyword for value in modifiers.split(';') if value)
        item=source[keyword]
        assert item['candidate_sources']
        seeds.append({'id':f's{len(seeds)+1:03d}','keyword':keyword,'family':family,'family_title':titles[family],
            'role':role,'literal_modifiers':[x for x in modifiers.split(';') if x],'cohort':cohort,
            'baseline_keyword':anchors[family],'comparison_scope':'same_task_family' if cohort=='paired' else 'related_broad_anchor_only_not_equivalence',
            'previous_group':member_group.get(keyword),'previous_review':reviews.get(keyword),
            'candidate_sources':item['candidate_sources'],
            'selection_reason':'通用表达对照' if role=='baseline' else ('单独观察小组或待复核候选；不预先认定新生需求' if cohort=='exploration' else '保留词面差异或同义对照，实测相对通用查询的补充')})
    for family,rows in FAMILIES:
        assert len(rows)==5
        for keyword,role,modifiers in rows: add(keyword,family,role,modifiers,'paired')
    for keyword,family,modifiers in EXPLORATION: add(keyword,family,'exploration',modifiers,'exploration')
    assert len(seeds)==len({s['keyword'] for s in seeds})==100
    ids=[s['id'] for s in seeds]
    random.Random(20261012).shuffle(ids)
    for row in seeds:
        row['payload']=[{'keywords':[row['keyword']],'location_code':2840,'language_code':'en','search_partners':False,'include_adult_keywords':False,'sort_by':'relevance'}]
    plan={'version':VERSION,'market':{'country':'US','location_code':2840,'language_code':'en'},'endpoint':ENDPOINT,
          'seed_count':100,'paired_families':18,'paired_seeds':90,'exploration_seeds':10,'single_seed_per_request':True,
          'estimated_cost_usd':str(UNIT*len(seeds)),'budget_ceiling_usd':str(BUDGET),'unit_price_usd':str(UNIT),
          'pricing_source':'https://dataforseo.com/pricing/keywords-data/google-ads','pricing_checked_date':'2026-10-12',
          'no_automatic_paid_retry':True,'min_interval_seconds':6,'execution_order':ids,
          'selection_type':'Purposive stratified pilot, not random population sample; allocation is experimental only.',
          'evaluation':['Exact word overlap is descriptive only, not semantic demand overlap.','Compare each paired seed with its family anchor and with the existing input corpus.','Exploration candidates remain separate from paired comparisons.','Zero suggestions do not establish absent or early-stage demand.','No causal or population-wide accuracy claim from this pilot.'],
          'input_hashes':{name:sha((SOURCE/name).read_bytes()) for name in ['seed-clusters/clusters.json','seed-clusters/needs-review.json','ai-relevance/keywords-ai-strong.txt']},'seeds':seeds}
    OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'plan.json').exists():
        assert load(OUT/'plan.json')==plan,'immutable_plan_changed'
    else: save(OUT/'plan.json',plan)
    (OUT/'seeds.txt').write_text('\n'.join(s['keyword'] for s in seeds)+'\n',encoding='utf-8')
    lines=['# 美国英文 100 词扩词验证样本','', '18 个功能方向各 5 个词用于配对比较，另有 10 个小组或未决候选单独探索。该配额仅为实验设计，不作为正式选种规则。','', '100 个独立请求，预算上限 $9；结果或扣费不确定时停止，不自动重试。','', '| 编号 | 功能方向 | 关键词 | 比较角色 | 原分组状态 |','|---|---|---|---|---|']
    for s in seeds: lines.append(f"| {s['id']} | {s['family_title']} | `{s['keyword']}` | {s['role']} | {'待复核' if s['previous_review'] else s['previous_group']} |")
    (OUT/'sample-plan.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    log('prepared',count=100,families=18,roles=dict(collections.Counter(s['role'] for s in seeds)),review_seeds=sum(bool(s['previous_review']) for s in seeds),out=str(OUT))
    return plan

def credential():
    path=ROOT/'promotion-intelligence-runner/.env'
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key,value=line.split('=',1)
        if key.strip()=='DFS':
            value=value.strip()
            if len(value)>=2 and value[0]==value[-1] and value[0] in "\"'": value=value[1:-1]
            if value: return value
    raise RuntimeError('missing_DFS_credential')

def database():
    db=sqlite3.connect(OUT/'checkpoint.sqlite3')
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,keyword TEXT,status TEXT,started_at TEXT,finished_at TEXT,cost TEXT,receipt_hash TEXT,error TEXT);
    CREATE TABLE IF NOT EXISTS results(seed_id TEXT,keyword TEXT,rank INTEGER,search_volume INTEGER,provider_json TEXT,PRIMARY KEY(seed_id,keyword));
    CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);''')
    return db

def ingest(db,seed,reply,path):
    cost=reply.get('cost')
    if isinstance(cost,bool) or cost is None: raise ValueError('unknown_response_cost')
    cost=Decimal(str(cost))
    if not cost.is_finite() or cost<0: raise ValueError('invalid_response_cost')
    tasks=reply.get('tasks')
    if reply.get('status_code')!=20000 or not isinstance(tasks,list) or len(tasks)!=1: raise ValueError('invalid_provider_envelope')
    task=tasks[0]
    if task.get('status_code')!=20000:
        with db: db.execute('UPDATE requests SET status=?,cost=?,error=?,finished_at=? WHERE id=?',('failed',str(cost),'provider_status_'+str(task.get('status_code')),now(),seed['id']))
        raise ValueError('provider_task_failed')
    data=task.get('data',{})
    for key in ['keywords','location_code','language_code','search_partners','include_adult_keywords','sort_by']:
        if data.get(key)!=seed['payload'][0][key]: raise ValueError('response_parameters_mismatch_'+key)
    results=task.get('result') or []
    if not isinstance(results,list) or len(results)>20000: raise ValueError('invalid_result_size')
    records=[]
    for rank,r in enumerate(results,1):
        word=normal(r.get('keyword',''))
        if not word: raise ValueError('missing_keyword')
        records.append((seed['id'],word,rank,r.get('search_volume'),json.dumps(r,ensure_ascii=False)))
    with db:
        db.executemany('INSERT OR IGNORE INTO results VALUES(?,?,?,?,?)',records)
        db.execute('UPDATE requests SET status=?,cost=?,finished_at=?,receipt_hash=?,error=NULL WHERE id=?',('done',str(cost),now(),sha(path.read_bytes()),seed['id']))
    return len(results),cost

def snapshot(db):
    statuses=dict(db.execute('SELECT status,count(*) FROM requests GROUP BY status').fetchall())
    spent=sum((Decimal(r[0]) for r in db.execute('SELECT cost FROM requests WHERE cost IS NOT NULL')),Decimal('0'))
    result={'at':now(),'version':VERSION,'statuses':statuses,'actual_recorded_cost_usd':str(spent),'result_rows':db.execute('SELECT count(*) FROM results').fetchone()[0],'distinct_result_keywords':db.execute('SELECT count(DISTINCT keyword) FROM results').fetchone()[0]}
    save(OUT/'progress.json',result)
    return result

def execute(plan):
    key=credential()
    db=database()
    plan_hash=sha((OUT/'plan.json').read_bytes())
    old=db.execute("SELECT value FROM config WHERE key='plan_hash'").fetchone()
    if old and old[0]!=plan_hash: raise ValueError('plan_changed')
    with db:
        db.execute('INSERT OR IGNORE INTO config VALUES(?,?)',('plan_hash',plan_hash))
        db.executemany('INSERT OR IGNORE INTO requests(id,keyword,status) VALUES(?,?,?)',[(s['id'],s['keyword'],'pending') for s in plan['seeds']])
    by_id={s['id']:s for s in plan['seeds']}
    last_start=time.monotonic()-6
    for sid in plan['execution_order']:
        seed=by_id[sid]
        row=db.execute('SELECT * FROM requests WHERE id=?',(sid,)).fetchone()
        path=OUT/'receipts'/f'{sid}.json'
        if row['status']=='done':continue
        if path.exists():
            receipt=load(path)
            ingest(db,seed,receipt['response'],path)
            continue
        if row['status']!='pending':raise RuntimeError('uncertain_or_failed_request_no_automatic_retry')
        progress=snapshot(db)
        if Decimal(progress['actual_recorded_cost_usd'])+UNIT>BUDGET:raise RuntimeError('budget_ceiling')
        delay=max(0,6-(time.monotonic()-last_start))
        if delay:time.sleep(delay)
        with db:db.execute('UPDATE requests SET status=?,started_at=? WHERE id=?',('started',now(),sid))
        last_start=time.monotonic()
        request=urllib.request.Request(ENDPOINT,data=json.dumps(seed['payload']).encode('utf-8'),headers={'Authorization':'Basic '+key,'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=150) as response:
                raw=response.read(32*1024*1024+1)
                if len(raw)>32*1024*1024:raise ValueError('response_too_large')
                reply=json.loads(raw)
            save(path,{'seed_id':sid,'keyword':seed['keyword'],'requested_payload':seed['payload'],'received_at':now(),'response':reply})
            rows,cost=ingest(db,seed,reply,path)
            progress=snapshot(db)
            log('request_done',seed_id=sid,keyword=seed['keyword'],rows=rows,cost_usd=str(cost),**{k:v for k,v in progress.items() if k not in ['at','version']})
            if cost>UNIT:raise RuntimeError('unit_price_exceeded_stop')
        except Exception as exc:
            status=db.execute('SELECT status FROM requests WHERE id=?',(sid,)).fetchone()[0]
            if status not in ['done','failed']:
                with db:db.execute('UPDATE requests SET status=?,error=?,finished_at=? WHERE id=?',('uncertain',type(exc).__name__,now(),sid))
            snapshot(db)
            log('stopped',seed_id=sid,error_type=type(exc).__name__)
            raise RuntimeError('provider_execution_stopped_inspect_saved_receipt_and_ledger') from None
    progress=snapshot(db)
    assert progress['statuses']=={'done':100}
    save(OUT/'provider-summary.json',progress)
    log('provider_complete',**{k:v for k,v in progress.items() if k not in ['at','version']})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    plan=prepare()
    if args.execute:execute(plan)
if __name__=='__main__':main()
