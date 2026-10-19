
"""Local-only English prefilter and cross-site keyword pool; no model calls."""
import importlib.util, pathlib, sqlite3, json, re, collections, unicodedata, time
ROOT=pathlib.Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/sitemap-keywords-once-20261012'
p=ROOT/'tool-data-runner/scripts/export_sitemap_keywords_once.py'
spec=importlib.util.spec_from_file_location('once',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
LANG=m.LOCALES|set("af am as az be bo bs cy eo eu ga gl gu ha hy ka kk km kn ku ky la lb lo mg mi ml mr mt my ne oc or pa ps qu sa sd si so su sw tg tk tl fil tw uz zu nb nn cn ch jp br".split())
LOCALE_CONTAINERS=m.ROUTES|{"ai"}
NEG=set("blog blogs article articles news post posts tutorial tutorials guide guides docs documentation help support faq faqs academy changelog press webinar webinars community gallery galleries feed-detail feed prompts prompt tags tag category categories collection collections users user profiles profile authors author compare comparison comparisons alternatives reviews review glossary how-to-guides tools-comparison press-releases case-studies".split())
LEGAL=set("about about-us contact contact-us pricing privacy privacy-policy terms terms-of-service terms-and-conditions login logout signin sign-in signup sign-up register account careers career affiliate affiliates sitemap search download downloads".split())
DOMAIN_PREFIXES={
 "aiartshop.com":("/products/","/collections/"),
 "easy-peasy.ai":("/ai-image-generator/images/",),
 "deepdreamgenerator.com":("/ddream/",),
 "gentube.app":("/image/","/gen-oc/character/"),
 "photoai.com":("/photos/","/@"),
 "coverdesignai.com":("/book-cover-designs/",),
 "magichour.ai":("/templates/details/",),
 "krea.ai":("/nodes/app/",),
 "illustration.app":("/illustration/",),
 "colorin.ai":("/free-printable-coloring-pages/",),
 "coolcoloringpages.com":("/coloring-pages/",),
 "coloromo.com":("/product-",),
 "aibabygenerator.ai":("/baby-names/name/",),
 "revid.ai":("/tiktok-video-finder/",),
}
def language_of_url(url):
 parts=m.urlsplit(url).path.strip('/').split('/')
 for i in (0,1):
  if i>=len(parts) or (i==1 and parts[0].lower() not in LOCALE_CONTAINERS):continue
  match=re.fullmatch(r'([a-z]{2,3})(?:-[a-z]{2,4}|-\d{3})?',parts[i].lower())
  if match and match[1] in LANG:return match[1]
 return None
def reject(row,url):
 parts=[m.unquote(x) for x in m.urlsplit(url).path.split('/') if x]
 lang=language_of_url(url)
 if lang and lang!='en':return 'non_english_page'
 if parts and re.fullmatch(r'en(?:-[a-z]{2,4})?',parts[0],re.I):parts.pop(0)
 elif len(parts)>1 and parts[0].lower() in LOCALE_CONTAINERS and re.fullmatch(r'en(?:-[a-z]{2,4})?',parts[1],re.I):parts.pop(1)
 if not parts:return 'homepage'
 lower=[x.lower() for x in parts]
 if lower[0].startswith('@'):return 'user_profile'
 if any(x in NEG for x in lower[:-1]) or lower[0] in NEG:return 'non_function_directory'
 path='/'+('/'.join(lower))
 if any(path.startswith(prefix) for prefix in DOMAIN_PREFIXES.get(row['domain'],())):return 'observed_content_directory'
 if lower[-1] in LEGAL or (len(parts)==1 and lower[0] in m.ROUTES):return 'navigation'
 leaf=unicodedata.normalize('NFKC',parts[-1])
 if re.search(r'[^\x00-\x7f]',leaf):return 'non_english_keyword'
 if re.search(r'(?:^|[-_])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:$|[-_])',leaf,re.I):return 'uuid_content'
 if re.fullmatch(r'[A-Za-z0-9]{4,}',leaf) and re.search('[a-zA-Z]',leaf) and re.search('[0-9]',leaf):
  # Preserve recognizable model names/versions; random hashes never become keywords.
  if not (re.fullmatch(r'[A-Za-z]{3,}[0-9]{1,2}[a-z]?',leaf,re.I) or re.fullmatch(r'[A-Za-z]{3,}2[A-Za-z]{3,}',leaf,re.I)):return 'opaque_alphanumeric'
 if row['domain']=='a1.art' and len(parts)==1 and re.fullmatch(r'[a-z0-9]{5}',leaf,re.I) and re.search(r'[0-9]',leaf):return 'observed_short_id'
 if lower[-1].startswith(('how-to-','what-is-','what-are-')):return 'editorial_question'
 keyword=row['keyword']
 if any(re.fullmatch(r'[a-f0-9]{8,}',x) for x in keyword.split()):return 'hash_keyword'
 if len(keyword)<3 or (len(keyword.split())>=3 and sum(len(x)==1 for x in keyword.split())>=len(keyword.split())/2):return 'fragmented_keyword'
 return None
def run(output=None):
 global OUT
 if output is not None:OUT=pathlib.Path(output)
 c=sqlite3.connect(OUT/'checkpoint.sqlite3',timeout=60);c.row_factory=sqlite3.Row;c.execute('PRAGMA cache_size=-131072')
 pending=c.execute("SELECT count(*) FROM objects WHERE status!='done'").fetchone()[0]
 c.executescript("""CREATE TABLE IF NOT EXISTS keyword_pool(keyword TEXT PRIMARY KEY,status TEXT,selected_id TEXT,label TEXT,confidence REAL,request_id TEXT);
 CREATE TABLE IF NOT EXISTS keyword_pool_members(keyword TEXT,candidate_id TEXT,english_url TEXT,priority INTEGER,PRIMARY KEY(keyword,candidate_id));
 CREATE INDEX IF NOT EXISTS pool_members_keyword ON keyword_pool_members(keyword,priority);
 CREATE TABLE IF NOT EXISTS prefilter_rejections(candidate_id TEXT PRIMARY KEY,reason TEXT);""")
 # Prefer any genuine English/default-locale source over the first stored locale.
 wanted={}
 for r in c.execute('SELECT candidate_id,url FROM sources'):
  if r['candidate_id'] not in wanted and language_of_url(r['url']) in (None,'en'):wanted[r['candidate_id']]=r['url']
 reasons=collections.Counter();members=[];rejections=[];total=0
 for r in c.execute('SELECT * FROM candidates'):
  total+=1;url=wanted.get(r['id'])
  reason='no_english_source' if not url else reject(r,url)
  if reason:reasons[reason]+=1;rejections.append((r['id'],reason));continue
  keyword=' '.join(unicodedata.normalize('NFKC',r['keyword']).casefold().split())
  parts=m.urlsplit(url).path.split('/')
  priority=0 if any(x in ('tools','tool','features','feature','apps','app','generate','generator','create') for x in parts) else 1
  members.append((keyword,r['id'],url,priority))
 with c:
  c.execute('DELETE FROM keyword_pool_members');c.execute('DELETE FROM prefilter_rejections')
  c.executemany('INSERT INTO keyword_pool_members VALUES(?,?,?,?)',members)
  c.executemany('INSERT INTO prefilter_rejections VALUES(?,?)',rejections)
  c.execute("INSERT OR IGNORE INTO keyword_pool(keyword,status) SELECT DISTINCT keyword,'pending' FROM keyword_pool_members")
  c.execute("DELETE FROM keyword_pool WHERE NOT EXISTS(SELECT 1 FROM keyword_pool_members m WHERE m.keyword=keyword_pool.keyword)")
  # Prior URL-only judgments predate English/meaningful-keyword checks. Recheck those words once, globally deduplicated.
  c.execute("UPDATE keyword_pool SET status='pending',selected_id=NULL,label=NULL,confidence=NULL,request_id=NULL WHERE label='cached_function'")
  # Preserve already completed keyword-level classifications.

 summary={'at':m.now(),'remaining_snapshot_objects':c.execute("SELECT count(*) FROM objects WHERE status!='done'").fetchone()[0],'parsed_url_records':c.execute("SELECT sum(rows) FROM objects WHERE status='done'").fetchone()[0],
  'normalized_candidates':total,'english_meaningful_candidate_urls':len(members),'distinct_keywords':c.execute('SELECT count(*) FROM keyword_pool').fetchone()[0],
  'keyword_status':dict(c.execute('SELECT status,count(*) FROM keyword_pool GROUP BY status').fetchall()),'filtered':dict(reasons)}
 m.save(OUT/'prefilter-summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
 c.close()
if __name__=='__main__':run()

