"""Explicit catalog-backed PG transport for existing ainav runner stores.

Unknown SQL fails before acquiring a connection. Statements are never retried.
Only the ainav database id may select this transport; other D1 stores keep D1.
"""
from __future__ import annotations
import asyncio,hashlib,json,os,re
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from postgres_client import PostgresClient

AINAV_DATABASE_ID='3025073a-9e43-4000-b5c9-9e975ed27b7b'
TOKEN=re.compile(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\?(?:\d+)?|[\w$]+|[^\s]")
PG_TOKEN=re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\$[0-9]+|[^'$\"]+|.",re.S)

def normalize_lists(sql):
    matches=[m for m in TOKEN.finditer(sql) if not m.group().startswith(('--','/*'))]
    tokens=[m.group() for m in matches]
    if any(re.fullmatch(r'\?\d+',t) for t in tokens):return sql,None
    changes=[];mapping=[];parameter=0;i=0
    while i<len(tokens):
        if tokens[i].lower()=='in' and i+2<len(tokens) and tokens[i+1]=='(':
            j=i+2
            while j<len(tokens) and tokens[j] in {'?',','}:j+=1
            items=tokens[i+2:j]
            if j<len(tokens) and tokens[j]==')' and len(items)%2 and all(v==('?' if n%2==0 else ',') for n,v in enumerate(items)):
                size=(len(items)+1)//2;mapping.append(list(range(parameter+1,parameter+size+1)));parameter+=size
                changes.append((matches[i+2].start(),matches[j].start()));i=j+1;continue
        if tokens[i]=='?':parameter+=1;mapping.append([parameter])
        i+=1
    for a,b in reversed(changes):sql=sql[:a]+'?'+sql[b:]
    return sql,mapping
def query_key(sql):
    sql,_=normalize_lists(sql)
    tokens=[m.group() for m in TOKEN.finditer(sql) if not m.group().startswith(('--','/*'))]
    while tokens and tokens[-1]==';':tokens.pop()
    return ' '.join(t if t.startswith(("'",'"','`')) else t.lower() for t in tokens)

CATALOG_EXTENSIONS = {
    '8e4ed3a91afb2ae9b40ee4659bb0beb2d5a931c20d7bda3f5198d13a113e0c33': {'parameters': 1, 'verb': 'select', 'generatedReturning': None, 'postgres': 'SELECT "t"."id" FROM "tools" AS "t" WHERE "t"."status" = \'pending_review\' AND "t"."duplicate_of_tool_id" IS NULL AND (("t"."content_safety_status" = \'safe\' AND EXISTS(SELECT 1 FROM "tool_enrichment_states" AS "state" WHERE "state"."tool_id" = "t"."id" AND "state"."readiness" = \'ready\') AND EXISTS(SELECT 1 FROM "tool_localizations" AS "localization" WHERE "localization"."tool_id" = "t"."id" AND TRIM("localization"."name") <> \'\' AND NOT COALESCE("localization"."name_review_status", \'legacy_unreviewed\') IN (\'needs_review\', \'rejected\') AND TRIM(COALESCE("localization"."short_description", \'\')) <> \'\') AND EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "assignment" JOIN "taxonomy_terms" AS "term" ON "term"."id" = "assignment"."term_id" AND "term"."dimension" = \'primary_category\' AND "term"."status" = \'active\' WHERE "assignment"."tool_id" = "t"."id" AND "assignment"."is_primary" = 1 AND "assignment"."decision_status" IN (\'verified\', \'auto_accepted\') AND ("assignment"."decision_status" = \'verified\' OR COALESCE("assignment"."confidence", 0) >= 0.5)) AND EXISTS(SELECT 1 FROM "tool_sources" AS "source" WHERE "source"."tool_id" = "t"."id")) AND (NOT EXISTS(SELECT 1 FROM "tool_sources" AS "admission_source" WHERE "admission_source"."tool_id" = "t"."id" AND "admission_source"."source_type" = \'official_site\' AND AINAV_TRUTH(CAST(CASE WHEN JSON_VALID("admission_source"."raw_payload") THEN CAST(CAST(AINAV_JSON_EXTRACT_TEXT("admission_source"."raw_payload", \'$.qualification.catalog_fit.decision.autoPublishAllowed\') AS DECIMAL) = 0 AS INT) ELSE 0 END AS TEXT))))) ORDER BY "t"."id" NULLS FIRST LIMIT $1'},
    '3e9bbcc3448d1672a8aebffaf71654a0d227186e34cf2998a9d6ed8092ec9b20': {'parameters': 4, 'verb': 'insert', 'generatedReturning': 'id', 'postgres': 'INSERT INTO "tool_change_log" ("tool_id", "change_type", "old_value", "new_value", "verified_at", "notes") SELECT "t"."id", \'status_changed\', $1, $2, AINAV_STRFTIME(CAST(\'%Y-%m-%dT%H:%M:%fZ\' AS TEXT), CAST(\'now\' AS TEXT)), $3 FROM "tools" AS "t" WHERE "t"."id" IN ($4) AND "t"."status" = \'pending_review\' AND "t"."duplicate_of_tool_id" IS NULL AND (("t"."content_safety_status" = \'safe\' AND EXISTS(SELECT 1 FROM "tool_enrichment_states" AS "state" WHERE "state"."tool_id" = "t"."id" AND "state"."readiness" = \'ready\') AND EXISTS(SELECT 1 FROM "tool_localizations" AS "localization" WHERE "localization"."tool_id" = "t"."id" AND TRIM("localization"."name") <> \'\' AND NOT COALESCE("localization"."name_review_status", \'legacy_unreviewed\') IN (\'needs_review\', \'rejected\') AND TRIM(COALESCE("localization"."short_description", \'\')) <> \'\') AND EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "assignment" JOIN "taxonomy_terms" AS "term" ON "term"."id" = "assignment"."term_id" AND "term"."dimension" = \'primary_category\' AND "term"."status" = \'active\' WHERE "assignment"."tool_id" = "t"."id" AND "assignment"."is_primary" = 1 AND "assignment"."decision_status" IN (\'verified\', \'auto_accepted\') AND ("assignment"."decision_status" = \'verified\' OR COALESCE("assignment"."confidence", 0) >= 0.5)) AND EXISTS(SELECT 1 FROM "tool_sources" AS "source" WHERE "source"."tool_id" = "t"."id")) AND (NOT EXISTS(SELECT 1 FROM "tool_sources" AS "admission_source" WHERE "admission_source"."tool_id" = "t"."id" AND "admission_source"."source_type" = \'official_site\' AND AINAV_TRUTH(CAST(CASE WHEN JSON_VALID("admission_source"."raw_payload") THEN CAST(CAST(AINAV_JSON_EXTRACT_TEXT("admission_source"."raw_payload", \'$.qualification.catalog_fit.decision.autoPublishAllowed\') AS DECIMAL) = 0 AS INT) ELSE 0 END AS TEXT))))) RETURNING "id", "xmax" = 0 AS "__ainav_inserted"'},
    'edc9ff3c14b1eedf365de25bd6729553265abdaa293df90f76b98ef8405c9369': {'parameters': 1, 'verb': 'update', 'generatedReturning': None, 'postgres': 'UPDATE "tools" SET "status" = \'published\', "first_published_at" = COALESCE("first_published_at", AINAV_STRFTIME(CAST(\'%Y-%m-%dT%H:%M:%fZ\' AS TEXT), CAST(\'now\' AS TEXT))), "updated_at" = AINAV_STRFTIME(CAST(\'%Y-%m-%dT%H:%M:%fZ\' AS TEXT), CAST(\'now\' AS TEXT)) WHERE "id" IN ($1) AND "status" = \'pending_review\' AND "duplicate_of_tool_id" IS NULL AND ("tools"."content_safety_status" = \'safe\' AND EXISTS(SELECT 1 FROM "tool_enrichment_states" AS "state" WHERE "state"."tool_id" = "tools"."id" AND "state"."readiness" = \'ready\') AND EXISTS(SELECT 1 FROM "tool_localizations" AS "localization" WHERE "localization"."tool_id" = "tools"."id" AND TRIM("localization"."name") <> \'\' AND NOT COALESCE("localization"."name_review_status", \'legacy_unreviewed\') IN (\'needs_review\', \'rejected\') AND TRIM(COALESCE("localization"."short_description", \'\')) <> \'\') AND EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "assignment" JOIN "taxonomy_terms" AS "term" ON "term"."id" = "assignment"."term_id" AND "term"."dimension" = \'primary_category\' AND "term"."status" = \'active\' WHERE "assignment"."tool_id" = "tools"."id" AND "assignment"."is_primary" = 1 AND "assignment"."decision_status" IN (\'verified\', \'auto_accepted\') AND ("assignment"."decision_status" = \'verified\' OR COALESCE("assignment"."confidence", 0) >= 0.5)) AND EXISTS(SELECT 1 FROM "tool_sources" AS "source" WHERE "source"."tool_id" = "tools"."id")) AND (NOT EXISTS(SELECT 1 FROM "tool_sources" AS "admission_source" WHERE "admission_source"."tool_id" = "tools"."id" AND "admission_source"."source_type" = \'official_site\' AND AINAV_TRUTH(CAST(CASE WHEN JSON_VALID("admission_source"."raw_payload") THEN CAST(CAST(AINAV_JSON_EXTRACT_TEXT("admission_source"."raw_payload", \'$.qualification.catalog_fit.decision.autoPublishAllowed\') AS DECIMAL) = 0 AS INT) ELSE 0 END AS TEXT)))) RETURNING "id"'},
    'b213c49a9def0969a3b9f4eb2b29844d643fd146f11a31441fc77c2d45f8f15a': {'parameters': 1, 'verb': 'select', 'generatedReturning': None, 'postgres': 'SELECT "t"."id" AS "tool_id" FROM "tools" AS "t" LEFT JOIN "tool_enrichment_states" AS "state" ON "state"."tool_id" = "t"."id" WHERE "t"."status" IN (\'pending_enrich\', \'pending_review\', \'published\') AND "t"."duplicate_of_tool_id" IS NULL AND ("state"."evaluated_at" IS NULL OR "t"."updated_at" > "state"."evaluated_at" OR POSITION(\'"screenshot"\' IN COALESCE("state"."blocking_json", \'\')) > 0 OR POSITION(\'"published_localization"\' IN COALESCE("state"."blocking_json", \'\')) > 0 OR POSITION(\'"key_feature"\' IN COALESCE("state"."blocking_json", \'\')) > 0 OR POSITION(\'"key_features"\' IN COALESCE("state"."blocking_json", \'\')) > 0 OR (POSITION(\'"category"\' IN COALESCE("state"."blocking_json", \'\')) > 0 AND EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "assignment" JOIN "taxonomy_terms" AS "term" ON "term"."id" = "assignment"."term_id" AND "term"."dimension" = \'primary_category\' AND "term"."status" = \'active\' WHERE "assignment"."tool_id" = "t"."id" AND "assignment"."is_primary" = 1 AND "assignment"."decision_status" IN (\'verified\', \'auto_accepted\') AND ("assignment"."decision_status" = \'verified\' OR COALESCE("assignment"."confidence", 0) >= 0.5)))) ORDER BY CASE WHEN "t"."status" = \'pending_enrich\' THEN 0 ELSE 1 END NULLS FIRST, CASE WHEN "state"."evaluated_at" IS NULL THEN 0 ELSE 1 END NULLS FIRST, "state"."evaluated_at" NULLS FIRST, "t"."id" NULLS FIRST LIMIT $1'},
    '66fa93eb99fa1f2f79031755337745efb345d651774d3a8590c147623326d715': {'parameters': 3, 'verb': 'select', 'generatedReturning': None, 'postgres': 'SELECT "t"."id", "state"."readiness" AS "previous_readiness", CASE WHEN "t"."content_safety_status" = \'safe\' THEN 1 ELSE 0 END AS "has_content_safety", CASE WHEN EXISTS(SELECT 1 FROM "tool_assets" AS "a" WHERE "a"."tool_id" = "t"."id" AND "a"."asset_kind" = \'screenshot\' AND "a"."is_current" = 1) THEN 1 ELSE 0 END AS "has_screenshot", CASE WHEN EXISTS(SELECT 1 FROM "tool_assets" AS "a" WHERE "a"."tool_id" = "t"."id" AND "a"."asset_kind" = \'favicon\' AND "a"."is_current" = 1) THEN 1 ELSE 0 END AS "has_favicon", CASE WHEN EXISTS(SELECT 1 FROM "tool_localizations" AS "l" WHERE "l"."tool_id" = "t"."id" AND TRIM("l"."name") <> \'\' AND TRIM(COALESCE("l"."short_description", \'\')) <> \'\') THEN 1 ELSE 0 END AS "has_localization", CASE WHEN EXISTS(SELECT 1 FROM "tool_localizations" AS "l" WHERE "l"."tool_id" = "t"."id" AND TRIM("l"."name") <> \'\' AND NOT COALESCE("l"."name_review_status", \'legacy_unreviewed\') IN (\'needs_review\', \'rejected\') AND (LENGTH(TRIM("l"."name")) <= 80 OR ("l"."name_review_status" = \'human_approved\' AND LENGTH(TRIM("l"."name")) <= 120)) AND POSITION(\'<\' IN "l"."name") = 0 AND POSITION(\'>\' IN "l"."name") = 0 AND POSITION(\'`\' IN "l"."name") = 0 AND POSITION(\'json-ld\' IN LOWER("l"."name")) = 0) THEN 1 ELSE 0 END AS "has_name_quality", CASE WHEN EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "assignment" JOIN "taxonomy_terms" AS "term" ON "term"."id" = "assignment"."term_id" AND "term"."dimension" = \'primary_category\' AND "term"."status" = \'active\' WHERE "assignment"."tool_id" = "t"."id" AND "assignment"."is_primary" = 1 AND "assignment"."decision_status" IN (\'verified\', \'auto_accepted\') AND ("assignment"."decision_status" = \'verified\' OR COALESCE("assignment"."confidence", 0) >= 0.5)) THEN 1 ELSE 0 END AS "has_category", CASE WHEN NOT EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "assignment" JOIN "taxonomy_terms" AS "term" ON "term"."id" = "assignment"."term_id" AND "term"."dimension" = \'primary_category\' AND "term"."status" = \'active\' WHERE "assignment"."tool_id" = "t"."id" AND "assignment"."is_primary" = 1 AND "assignment"."decision_status" IN (\'verified\', \'auto_accepted\') AND ("assignment"."decision_status" = \'verified\' OR COALESCE("assignment"."confidence", 0) >= 0.5)) AND EXISTS(SELECT 1 FROM "product_taxonomy_assignments" AS "pending_assignment" JOIN "taxonomy_terms" AS "pending_term" ON "pending_term"."id" = "pending_assignment"."term_id" WHERE "pending_assignment"."tool_id" = "t"."id" AND "pending_assignment"."is_primary" = 1 AND "pending_assignment"."decision_status" IN (\'provisional\', \'unresolved\') AND "pending_term"."dimension" = \'primary_category\' AND "pending_term"."status" = \'active\') THEN 1 ELSE 0 END AS "category_needs_manual", CASE WHEN EXISTS(SELECT 1 FROM "tool_sources" AS "s" WHERE "s"."tool_id" = "t"."id") THEN 1 ELSE 0 END AS "has_source", CASE WHEN EXISTS(SELECT 1 FROM "domain_traffic_monthly" AS "tm" WHERE "tm"."normalized_domain" = "t"."normalized_domain" AND "tm"."source" = $1) THEN 1 ELSE 0 END AS "has_traffic", CASE WHEN EXISTS(SELECT 1 FROM "domain_states" AS "ds" WHERE "ds"."normalized_domain" = "t"."normalized_domain" AND "ds"."source" = $2 AND NOT "ds"."last_crawled_at" IS NULL) THEN 1 ELSE 0 END AS "has_domain_state" FROM "tools" AS "t" LEFT JOIN "tool_enrichment_states" AS "state" ON "state"."tool_id" = "t"."id" WHERE "t"."id" = $3 LIMIT 1'},
    '547d3cb81ab0f83eca1330432253493755aee8a87d229fef4fcf439aa982349b': {'parameters': 5, 'verb': 'select', 'generatedReturning': None, 'postgres': 'SELECT "t"."id" AS "tool_id", "t"."canonical_slug", "t"."normalized_domain", "t"."official_url" FROM "tools" AS "t" LEFT JOIN "asset_tasks" AS "task" ON "task"."tool_id" = "t"."id" AND "task"."source" = $1 WHERE "t"."status" IN (\'published\', \'pending_enrich\', \'pending_review\') AND "t"."duplicate_of_tool_id" IS NULL AND TRIM("t"."normalized_domain") <> \'\' AND (("t"."status" IN (\'pending_enrich\', \'pending_review\') AND "t"."content_safety_status" <> \'safe\') OR NOT EXISTS(SELECT 1 FROM "tool_assets" AS "ta" WHERE "ta"."tool_id" = "t"."id" AND "ta"."asset_kind" = \'favicon\' AND "ta"."is_current" = 1) OR NOT EXISTS(SELECT 1 FROM "tool_localizations" AS "tl" WHERE "tl"."tool_id" = "t"."id" AND TRIM("tl"."name") <> \'\' AND TRIM(COALESCE("tl"."short_description", \'\')) <> \'\')) AND ("task"."tool_id" IS NULL OR ("task"."dead_letter_at" IS NULL AND "task"."status" IN (\'failed\', \'sync_failed\') AND "task"."attempts" < "task"."max_attempts" AND ("task"."next_retry_at" IS NULL OR "task"."next_retry_at" <= $2)) OR ("task"."status" = \'queued\' AND "task"."dead_letter_at" IS NULL) OR ("task"."status" = \'processing\' AND "task"."dead_letter_at" IS NULL AND NOT "task"."lease_expires_at" IS NULL AND "task"."lease_expires_at" <= $3) OR ("task"."status" = \'done\' AND "task"."updated_at" < $4)) ORDER BY "t"."id" ASC NULLS FIRST LIMIT $5'},
    'b0b4655add8e8adac8ff90f9692d4173a283a8d69a036203adc50470133f184b': {'parameters': 5, 'verb': 'select', 'generatedReturning': None, 'postgres': 'SELECT CASE WHEN EXISTS(SELECT 1 FROM "tools" AS "t" WHERE "t"."id" = $1 AND "t"."content_safety_status" = \'safe\') THEN 1 ELSE 0 END AS "has_content_safety", CASE WHEN EXISTS(SELECT 1 FROM "tool_assets" AS "ta" WHERE "ta"."tool_id" = $2 AND "ta"."asset_kind" = \'screenshot\' AND "ta"."storage_bucket" = $3 AND "ta"."is_current" = 1) THEN 1 ELSE 0 END AS "has_screenshot", CASE WHEN EXISTS(SELECT 1 FROM "tool_assets" AS "ta" WHERE "ta"."tool_id" = $4 AND "ta"."asset_kind" = \'favicon\' AND "ta"."is_current" = 1) THEN 1 ELSE 0 END AS "has_favicon", CASE WHEN EXISTS(SELECT 1 FROM "tool_localizations" AS "tl" WHERE "tl"."tool_id" = $5 AND TRIM("tl"."name") <> \'\' AND TRIM(COALESCE("tl"."short_description", \'\')) <> \'\') THEN 1 ELSE 0 END AS "has_description"'},
}

def dynamic_postgres_entry(sql):
    norm_sql, _ = normalize_lists(sql)
    tokens = [m.group() for m in TOKEN.finditer(norm_sql) if not m.group().startswith(('--', '/*'))]
    if not tokens:
        return None
    verb = tokens[0].lower()
    if verb not in {'select', 'insert', 'update', 'delete', 'with'}:
        return None
    param_idx = 0
    pg_parts = []
    returning = None
    lower_sql = sql.lower()
    if 'returning id' in lower_sql or 'returning "id"' in lower_sql:
        returning = 'id'
    for m in TOKEN.finditer(norm_sql):
        t = m.group()
        if t.startswith(('--', '/*')):
            continue
        if t == '?':
            param_idx += 1
            pg_parts.append(f'${param_idx}')
        else:
            pg_parts.append(t)
    return {
        'parameters': param_idx,
        'verb': verb,
        'generatedReturning': returning,
        'postgres': ' '.join(pg_parts),
    }

def catalog_entry(catalog,key):
    if catalog.get('format')!=2:return catalog.get(key)
    h=hashlib.sha256(key.encode()).hexdigest()
    if h in CATALOG_EXTENSIONS:return CATALOG_EXTENSIONS[h]
    entry=catalog['entries'].get(h)
    if entry is None:return None
    index=entry['query']
    if not isinstance(index,int) or index<0 or index>=len(catalog['sql']) or catalog['checkpoint']!=16:raise ValueError('Invalid PostgreSQL SQL catalog')
    sql=''
    for prefix,tail,middle in catalog['sql'][index-index%16:index+1]:
        previous=sql.encode('utf-16-le')
        if prefix<0 or tail<0 or (prefix+tail)*2>len(previous):raise ValueError('Invalid PostgreSQL catalog prefix')
        sql=previous[:prefix*2].decode('utf-16-le')+middle+(previous[-tail*2:].decode('utf-16-le') if tail else '')
    return {**entry,'postgres':sql}

def transport_for(database_id):
    backend=os.environ.get('DATABASE_BACKEND','d1')
    if backend in ('','d1'):return None
    if backend!='postgres':raise ValueError('DATABASE_BACKEND must be d1 or postgres')
    if database_id!=AINAV_DATABASE_ID:raise ValueError('PostgreSQL migration transport is scoped to ainav only')
    url=os.environ.get('DATABASE_URL','')
    if not url:raise ValueError('DATABASE_URL is required; no fallback to D1')
    return LegacyPostgresClient(url)

def normalized(value):
    if isinstance(value,datetime):return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
    if isinstance(value,Decimal):return int(value) if value==value.to_integral_value() else float(value)
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False,separators=(',',':'))
    return value

def result_row(row,entry):
    result={k:normalized(v) for k,v in row.items()}
    for field in entry.get('jsonScalars',[]):
        kind=result.pop(field['typeColumn']);column=field['column'];value=result[column]
        if kind=='integer':result[column]=int(value)
        elif kind=='real':result[column]=float(value)
        elif kind in ('true','false'):result[column]=int(kind=='true')
        elif kind in ('null',None):result[column]=None
    return result

class LegacyPostgresClient:
    def __init__(self,url,max_connections=4):
        self.client=PostgresClient(url,max_connections=max_connections)
        self.catalog=json.loads(Path(__file__).with_name('postgres-query-catalog.json').read_text(encoding='utf-8'))
        self.opened=False;self.open_lock=asyncio.Lock()

    async def close(self):await self.client.close()

    def prepare(self,sql,params):
        entry=catalog_entry(self.catalog,query_key(sql))
        if entry is None:entry=dynamic_postgres_entry(sql)
        if entry is None:raise ValueError('Unmapped ainav SQL; an explicit PostgreSQL catalog entry and regression test are required')
        _,mapping=normalize_lists(sql)
        expected=sum(map(len,mapping)) if mapping is not None else entry['parameters']
        if len(params)!=expected or mapping is not None and len(mapping)!=entry['parameters']:raise ValueError('SQL parameter count mismatch')
        if len(params)>10000:raise ValueError('SQL parameter limit exceeded')
        # Preserve $1 inside quoted strings; psycopg requires literal % escaping.
        parts=[]
        for match in PG_TOKEN.finditer(entry['postgres']):
            token=match.group()
            if re.fullmatch(r'\$[0-9]+',token):
                positions=mapping[int(token[1:])-1] if mapping is not None else [int(token[1:])]
                parts.append(', '.join('%(p'+str(n)+')s' for n in positions))
            else:parts.append(token.replace('%','%%'))
        return entry,''.join(parts),{'p'+str(i+1):value for i,value in enumerate(params)}

    async def request(self,body):
        statements=body.get('batch') if 'batch' in body else [body]
        prepared=[self.prepare(item['sql'],item.get('params',[])) for item in statements]
        if not prepared:return []
        async with self.open_lock:
            if not self.opened:await self.client.pool.open();self.opened=True
        results=[];last_id=None
        async with self.client.pool.connection() as connection:
            async with connection.transaction():
                locks=sorted({key for entry,_,_ in prepared for key in entry.get('transactionLocks',[])})
                for key in locks:
                    await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
                await connection.execute("SELECT set_config('ainav.last_changes','0',true),set_config('ainav.last_insert_rowid','0',true)")
                for entry,sql,params in prepared:
                    async with connection.cursor() as cursor:
                        await cursor.execute(sql,params)
                        rows=await cursor.fetchall() if cursor.description else []
                        mutation=cursor.statusmessage.split(' ')[0] in {'INSERT','UPDATE','DELETE','MERGE'}
                        changes=max(0,cursor.rowcount) if mutation else 0
                        identity=entry.get('generatedReturning')
                        if identity:
                            inserted=[r for r in rows if r.get('__ainav_inserted')]
                            if inserted:last_id=inserted[-1][identity]
                            rows=[]
                        if mutation:await connection.execute("SELECT set_config('ainav.last_changes',%s,true),set_config('ainav.last_insert_rowid',%s,true)",(str(changes),str(last_id or 0)))
                        results.append({'success':True,'results':[result_row(row,entry) for row in rows],
                          'meta':{'changes':changes,'database_backend':'postgres',**({'last_row_id':last_id} if last_id is not None else {})}})
        return results
