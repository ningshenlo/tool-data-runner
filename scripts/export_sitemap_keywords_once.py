"""One-off sitemap -> Jev function classification -> URL keyword text export.
Run with --output DIR --stage all; repeat against the same directory to resume.
Cloudflare access uses cf SELECT/GET only. No scheduled or remote writes.
"""
import argparse, asyncio, gzip, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, unicodedata
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote, parse_qsl, urlencode
import httpx
from dotenv import dotenv_values
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jev import JEV_MODEL, JevError, call_jev, choice

VERSION="all-functions-url-v2"
DATABASE="3025073a-9e43-4000-b5c9-9e975ed27b7b"
LOCALES=set("ar bg bn ca cs da de el en es et fa fi fr he hi hr hu id is it ja ko lt lv mk mn ms nl no pl pt ro ru sk sl sq sr sv ta te th tr uk ur vi zh".split())
ROUTES=set("tools tool features feature products product models model apps app generate create blog blogs news articles article posts post docs documentation guide guides community gallery changelog resources solutions".split())

def now(): return datetime.now(timezone.utc).isoformat()
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def log(event,**kw): print(json.dumps(dict(at=now(),event=event,**kw),ensure_ascii=False),flush=True)
def save(path,value):
 tmp=path.with_suffix(path.suffix+".tmp")
 tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf8");tmp.replace(path)
def cf():
 command=shutil.which("cf.cmd" if os.name=="nt" else "cf")
 if not command: raise RuntimeError("cf_cli_missing")
 return command
def query(sql):
 result=subprocess.run([cf(),"d1","query",DATABASE,"--sql"," ".join(sql.splitlines())],capture_output=True,timeout=120)
 if result.returncode: raise RuntimeError("cf_query_failed")
 data=json.loads(result.stdout.decode("utf-8-sig"))
 if not data or any(not x.get("success") for x in data): raise RuntimeError("cf_query_unsuccessful")
 return [r for item in data for r in item["results"]]
def prepare(out,extra):
 path=out/"cohort.json"
 if path.exists(): return json.loads(path.read_text(encoding="utf8"))
 extra=",".join("'"+d.replace("'","''")+"'" for d in extra) or "''"
 sql=f"""
 WITH RECURSIVE image_terms(id) AS (
 SELECT id FROM taxonomy_terms WHERE dimension='primary_category' AND slug='image-generation' AND status='active'
 UNION ALL SELECT t.id FROM taxonomy_terms t JOIN image_terms p ON t.parent_id=p.id WHERE t.status='active'
 ), cohort AS (
 SELECT t.id AS tool_id,t.normalized_domain,term.slug AS primary_category,
 CASE WHEN p.term_id IN(SELECT id FROM image_terms) THEN 'image-generation'
 WHEN term.slug='video-generation-conversion' THEN 'video-generation-conversion' ELSE 'cross-category-generation' END AS market
 FROM tools t LEFT JOIN current_tool_primary_taxonomy p ON p.tool_id=t.id
 AND (p.decision_status='verified' OR coalesce(p.confidence,0)>=0.5)
 LEFT JOIN taxonomy_terms term ON term.id=p.term_id
 WHERE t.status='published' AND t.content_safety_status='safe' AND t.duplicate_of_tool_id IS NULL
 AND (p.term_id IN(SELECT id FROM image_terms) OR term.slug='video-generation-conversion'
 OR t.normalized_domain IN({extra}) OR EXISTS(
 SELECT 1 FROM product_taxonomy_assignments a JOIN taxonomy_terms tt ON tt.id=a.term_id
 WHERE a.tool_id=t.id AND tt.dimension='capability' AND tt.status='active'
 AND tt.slug IN('text-to-image','image-to-image','text-to-video','image-to-video')
 AND a.decision_status IN('verified','auto_accepted'))))
 SELECT t.*,s.id AS site_id,s.domain,s.status AS monitor_status,s.semantic_baseline_scan_id,
 c.url_count_after,c.finished_at AS baseline_at,c.is_committed,c.traversal_complete,
 c.successful_resource_ratio,c.resource_manifest_json FROM cohort t
 LEFT JOIN sitemap_sites s ON s.domain IN(t.normalized_domain,'www.'||t.normalized_domain)
 LEFT JOIN sitemap_site_scans c ON c.id=s.semantic_baseline_scan_id ORDER BY t.market,t.tool_id
 """
 value=dict(captured_at=now(),extra_domains=extra,rows=query(sql));save(path,value);return value
def connect(out):
 db=sqlite3.connect(out/"checkpoint.sqlite3",timeout=60);db.row_factory=sqlite3.Row
 db.executescript("""
 PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA cache_size=-131072; PRAGMA temp_store=MEMORY;
 CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);
 CREATE TABLE IF NOT EXISTS objects(object_key TEXT PRIMARY KEY,domain TEXT,market TEXT,scan_id TEXT,
 expected INTEGER,status TEXT DEFAULT 'pending',rows INTEGER,skipped TEXT,error TEXT);
 CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,domain TEXT,path_key TEXT,keyword TEXT,
 example_url TEXT,market TEXT,status TEXT DEFAULT 'pending',label TEXT,confidence REAL,request_id TEXT);
 CREATE INDEX IF NOT EXISTS candidates_pending ON candidates(status,id);
 CREATE TABLE IF NOT EXISTS sources(url_hash TEXT PRIMARY KEY,candidate_id TEXT,url TEXT,raw_url TEXT,locale TEXT,object_key TEXT);
 CREATE INDEX IF NOT EXISTS sources_candidate ON sources(candidate_id);
 CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,status TEXT,input_tokens INTEGER,output_tokens INTEGER,error TEXT);
 """)
 columns={r[1] for r in db.execute("PRAGMA table_info(candidates)")}
 if "classification_basis" not in columns:
  db.execute("ALTER TABLE candidates ADD COLUMN classification_basis TEXT")
 old=db.execute("SELECT value FROM config WHERE key='version'").fetchone()
 if old and old[0]!=VERSION: raise RuntimeError("new_prompt_requires_new_output_directory")
 with db: db.execute("INSERT OR IGNORE INTO config VALUES('version',?)",(VERSION,))
 return db
def usable(r):
 return bool(r["semantic_baseline_scan_id"] and r["is_committed"]==1 and r["traversal_complete"]==1 and r["successful_resource_ratio"]==1)
def register(db,cohort):
 with db:
  for r in cohort["rows"]:
   if not usable(r): continue
   for item in json.loads(r["resource_manifest_json"]):
    if item["kind"] in ("urlset","text") and item.get("state_key"):
     db.execute("INSERT OR IGNORE INTO objects(object_key,domain,market,scan_id,expected) VALUES(?,?,?,?,?)",
      (item["state_key"],r["domain"],r["market"],r["semantic_baseline_scan_id"],item["url_count"]))
def is_locale(s):
 return bool(re.fullmatch(r"[a-z]{2}(?:-[a-z]{2,4}|-\d{3})?",s.lower()) and s.split("-")[0].lower() in LOCALES)
def candidate(url,domain):
 """Only mechanical cleanup; Jev, not path rules, decides page purpose."""
 try:
  u=urlsplit(url);host=(u.hostname or "").lower().removeprefix("www.")
  if u.scheme not in ("http","https") or u.username or u.password or u.port: return None,"invalid_url"
  if host!=domain.lower().removeprefix("www."): return None,"different_host"
  if re.search(r"\.(xml|gz|png|jpe?g|gif|svg|webp|ico|css|js|woff2?|mp4|zip|pdf)$",u.path,re.I): return None,"non_page"
  parts=[unicodedata.normalize("NFKC",unquote(x,errors="strict")) for x in u.path.split("/") if x]
 except (UnicodeError,ValueError): return None,"invalid_url"
 language=None
 if parts and is_locale(parts[0]): language=parts.pop(0).lower()
 elif len(parts)>1 and parts[0].lower() in ROUTES and is_locale(parts[1]): language=parts.pop(1).lower()
 if not parts: return None,"homepage"
 leaf=re.sub(r"\.html?$","",parts[-1],flags=re.I)
 if re.fullmatch(r"[\d\W_]+",leaf) or re.fullmatch(r"[0-9a-f]{8,}",leaf,re.I) or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",leaf,re.I): return None,"opaque_leaf"
 leaf=re.sub(r"[-_][0-9a-f]{12,}$","",leaf,flags=re.I)
 leaf=re.sub(r"([A-Z]+)([A-Z][a-z])",r"\1 \2",leaf);leaf=re.sub(r"([a-z])([A-Z])",r"\1 \2",leaf);leaf=re.sub(r"[-_]+"," ",leaf)
 phrase=" ".join("".join(c if c.isalnum() or c in " .+" else " " for c in leaf).lower().split())
 if not phrase or not any(c.isalpha() for c in phrase) or len(phrase)>200: return None,"unextractable_leaf"
 query=[(k,v) for k,v in parse_qsl(u.query,keep_blank_values=True) if not re.match(r"^(utm_.+|gclid|fbclid|msclkid|ref|referrer)$",k,re.I)]
 path="/"+"/".join(parts)+("/" if u.path.endswith("/") else "")
 if query: path+="?"+urlencode(query)
 return dict(id=sha(host+"\n"+path),domain=host,path_key=path,keyword=phrase,locale=language),None
def object_path(out,key): return out/"objects"/(sha(key)+".jsonl.gz")
def download(out,key):
 path=object_path(out,key)
 if path.exists(): return path
 tmp=path.with_suffix(".part")
 with tmp.open("wb") as f:
  result=subprocess.run([cf(),"r2","objects","get",key,"--bucket-name","sitemap-monitor"],stdout=f,stderr=subprocess.PIPE,timeout=120)
 if result.returncode: raise RuntimeError("cf_object_get_failed")
 if tmp.stat().st_size>100*1024*1024: raise RuntimeError("object_too_large")
 with tmp.open("rb") as f:
  if f.read(2)!=b"\x1f\x8b": raise RuntimeError("object_not_gzip")
 tmp.replace(path);return path
def ingest_one(db,row,path):
 skipped=Counter();count=size=0
 with db,gzip.open(path,"rb") as f:
  for raw in f:
   size+=len(raw);count+=1
   if size>256*1024*1024 or len(raw)>100000 or count>row["expected"]: raise ValueError("state_limit")
   entry=json.loads(raw)
   if sha(entry["u"])!=entry["h"]: raise ValueError("state_hash_mismatch")
   c,reason=candidate(entry["u"],row["domain"])
   if not c: skipped[reason]+=1;continue
   db.execute("INSERT OR IGNORE INTO candidates(id,domain,path_key,keyword,example_url,market) VALUES(?,?,?,?,?,?)",
    (c["id"],c["domain"],c["path_key"],c["keyword"],entry["u"],row["market"]))
   db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?)",(entry["h"],c["id"],entry["u"],entry.get("r") or entry["u"],c["locale"],row["object_key"]))
  if count!=row["expected"]: raise ValueError("state_count_mismatch")
  db.execute("UPDATE objects SET status='done',rows=?,skipped=?,error=NULL WHERE object_key=?",(count,json.dumps(skipped),row["object_key"]))
async def ingest(db,out,args):
 rows=[dict(r) for r in db.execute("SELECT * FROM objects WHERE status!='done' ORDER BY domain,object_key")]
 rows.sort(key=lambda r:(not object_path(out,r["object_key"]).exists(),r["domain"],r["object_key"]))
 if args.max_objects: rows=rows[:args.max_objects]
 queue=asyncio.Queue()
 for r in rows: queue.put_nowait(r)
 done=errors=0
 async def worker():
  nonlocal done,errors
  while not queue.empty():
   r=queue.get_nowait()
   try: ingest_one(db,r,await asyncio.to_thread(download,out,r["object_key"]))
   except Exception as e:
    errors+=1
    with db: db.execute("UPDATE objects SET status='error',error=? WHERE object_key=?",(type(e).__name__+":"+str(e)[:120],r["object_key"]))
    log("object_error",domain=r["domain"],error_type=type(e).__name__)
   done+=1
   if done%20==0 or done==len(rows): log("ingest",objects_done=done,objects_planned=len(rows),errors=errors)
 await asyncio.gather(*(worker() for _ in range(args.download_concurrency)))

def key_from(path):
 values=dotenv_values(path) if path.exists() else {}
 for name in ("JEV_API_KEY","jev_api_key","TYPESAFE_API_KEY"):
  if value:=os.environ.get(name) or values.get(name): return value
 raise RuntimeError("jev_key_missing")
async def main(args):
 out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
 for name in ("objects","responses"): (out/name).mkdir(exist_ok=True)
 cohort=prepare(out,args.include_domain);db=connect(out)
 try:
  register(db,cohort)
  if args.stage in ("ingest","all"): await ingest(db,out,args)
  if args.stage in ("prefilter","all"):
   import sitemap_keyword_prefilter_once as pre
   pre.run(out)
  if args.stage in ("classify","export","all"):
   import sitemap_keyword_model_once as km
   if not (out/"prefilter-summary.json").exists(): raise RuntimeError("run_prefilter_before_classify")
   if args.stage in ("classify","all"): await km.classify(db,out,args,sys.modules[__name__])
   km.export(db,out,args,sys.modules[__name__])
  elif args.stage in ("prepare","ingest"):
   log("snapshot_stage",stage=args.stage,objects=dict(db.execute("SELECT status,count(*) FROM objects GROUP BY status").fetchall()))
 finally: db.close()

if __name__=="__main__":
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("--output",required=True,type=Path)
 p.add_argument("--stage",choices=["prepare","ingest","prefilter","classify","export","all"],default="all")
 p.add_argument("--env-file",type=Path,default=Path(__file__).resolve().parents[1]/".env")
 p.add_argument("--include-domain",action="append",default=[])
 p.add_argument("--download-concurrency",type=int,default=6);p.add_argument("--concurrency",type=int,default=4)
 p.add_argument("--batch-size",type=int,default=64);p.add_argument("--threshold",type=float,default=.85,help="minimum returned class probability for automatic acceptance")
 p.add_argument("--max-objects",type=int,default=0);p.add_argument("--max-batches",type=int,default=0)
 a=p.parse_args()
 if not 1<=a.download_concurrency<=12 or not 1<=a.concurrency<=12 or not 1<=a.batch_size<=64 or not 0<=a.threshold<=1 or a.max_objects<0 or a.max_batches<0: p.error("invalid limits")
 asyncio.run(main(a))




