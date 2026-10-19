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

def catalog_entry(catalog,key):
    if catalog.get('format')!=2:return catalog.get(key)
    entry=catalog['entries'].get(hashlib.sha256(key.encode()).hexdigest())
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
