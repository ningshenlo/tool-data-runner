import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import search_demand_refresh as refresh
from search_demand_brand import classify

SOURCE='similarweb'
MONTH='2026-08-01'
SNAPSHOT={'id':2,'traffic_month':MONTH}
NOW=1789459200


class Database:
    """Synthetic SQLite fixture, never a local copy of the production D1."""
    def __init__(self,db):
        self.db=db
        self.writes=0
        self.operations=[]

    async def execute(self,sql,params=None,*,operation=None):
        self.operations.append(operation)
        before=self.db.total_changes
        cursor=self.db.execute(sql,params or [])
        result={'results':[dict(row) for row in cursor.fetchall()] if cursor.description else [],
                'meta':{'changes':self.db.total_changes-before}}
        self.db.commit()
        self.writes+=result['meta']['changes']
        return result

    async def query(self,sql,params=None,*,operation=None):
        return (await self.execute(sql,params,operation=operation))['results']


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          PRAGMA foreign_keys=ON;
          CREATE TABLE tools(id INTEGER PRIMARY KEY,status TEXT,content_safety_status TEXT,duplicate_of_tool_id INTEGER);
          CREATE TABLE taxonomy_terms(id INTEGER PRIMARY KEY,parent_id INTEGER,dimension TEXT,status TEXT);
          CREATE TABLE market_snapshot_versions(id INTEGER PRIMARY KEY,traffic_source TEXT,traffic_month TEXT,status TEXT,activated_at TEXT);
          CREATE TABLE tool_market_snapshots(snapshot_id INTEGER,tool_id INTEGER,normalized_domain TEXT,primary_term_id INTEGER,visits INTEGER,PRIMARY KEY(snapshot_id,tool_id));
          CREATE TABLE domain_traffic_monthly(normalized_domain TEXT,source TEXT,traffic_month TEXT,metrics_json TEXT,source_snapshot_id INTEGER,captured_at TEXT,PRIMARY KEY(normalized_domain,source,traffic_month));
          CREATE TABLE search_keywords(id INTEGER PRIMARY KEY AUTOINCREMENT,normalized_keyword TEXT UNIQUE,display_keyword TEXT,first_seen_month TEXT,last_seen_month TEXT,updated_at TEXT);
          CREATE TABLE search_keyword_brand_labels(id INTEGER PRIMARY KEY AUTOINCREMENT,keyword_id INTEGER REFERENCES search_keywords(id),demand_type TEXT,matched_tool_id INTEGER,classification_method TEXT,confidence REAL,effective_from_month TEXT,is_current INTEGER DEFAULT 1);
          CREATE UNIQUE INDEX labels_current ON search_keyword_brand_labels(keyword_id) WHERE is_current=1;
          CREATE TABLE search_keyword_observations(id INTEGER PRIMARY KEY AUTOINCREMENT,keyword_id INTEGER REFERENCES search_keywords(id),product_id INTEGER REFERENCES tools(id),primary_term_id INTEGER,observed_month TEXT,source TEXT,normalized_domain TEXT,keyword_position INTEGER,observed_traffic INTEGER,search_volume INTEGER,cpc REAL,source_snapshot_id INTEGER,captured_at TEXT,UNIQUE(keyword_id,product_id,observed_month,source));
          CREATE TABLE search_keyword_monthly_metrics(keyword_id INTEGER REFERENCES search_keywords(id),primary_term_id INTEGER,observed_month TEXT,source TEXT,observed_traffic INTEGER,search_volume INTEGER,observed_products INTEGER,observed_markets INTEGER,captured_at TEXT,product_change_1m INTEGER,PRIMARY KEY(keyword_id,observed_month,source));
          CREATE TABLE search_keyword_term_snapshots(keyword_id INTEGER REFERENCES search_keywords(id),term_id INTEGER,observed_month TEXT,source TEXT,observed_traffic INTEGER,observed_products INTEGER,captured_at TEXT,PRIMARY KEY(keyword_id,term_id,observed_month,source));
          CREATE TABLE search_demand_coverage_snapshots(observed_month TEXT,source TEXT,observed_terms INTEGER,domain_coverage INTEGER,product_coverage INTEGER,market_coverage INTEGER,source_limit INTEGER,captured_at TEXT,PRIMARY KEY(observed_month,source));
          INSERT INTO taxonomy_terms VALUES(10,NULL,'primary_category','active'),(11,10,'primary_category','active'),(12,10,'primary_category','active');
          INSERT INTO market_snapshot_versions VALUES(2,'similarweb','2026-08-01','active','2026-09-15');
          INSERT INTO search_keywords VALUES(1,'common task','Common task','2026-07-01','2026-07-01',NULL);
          INSERT INTO search_keyword_brand_labels(keyword_id,demand_type,classification_method,confidence,effective_from_month) VALUES(1,'generic','manual_review',1,'2026-07-01');
          INSERT INTO search_keyword_monthly_metrics VALUES(1,11,'2026-07-01','similarweb',80,500,1,1,'2026-08-11',NULL);
          INSERT INTO search_demand_coverage_snapshots VALUES('2026-07-01','similarweb',1,1,1,1,5,'2026-08-11');
        ''')
        self.d1=Database(self.db)
        self.add_product(1,'one.example',11,[{'name':'common task','estimated_traffic':100.5,'volume':500},{'name':'one','estimated_traffic':10,'volume':80},{'name':' common task ','estimated_traffic':999}])
        self.add_product(2,'two.example',12,[{'name':'common task','estimated_traffic':200,'volume':700},{'name':'new task','estimated_traffic':0,'volume':0}])

    def add_product(self,id,domain,term,keywords):
        self.db.execute("INSERT INTO tools VALUES(?,'published','safe',NULL)",[id])
        self.db.execute('INSERT INTO tool_market_snapshots VALUES(2,?,?,?,1000)',[id,domain,term])
        self.db.execute('INSERT INTO domain_traffic_monthly VALUES(?,?,?,?,?,?)',[domain,SOURCE,MONTH,json.dumps({'top_search_keywords':keywords,'traffic_sources':{'direct':.7}}),id,'2026-09-15T00:00:00Z'])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    async def publish(self,**kwargs):
        return await refresh.publish_month(self.d1,SNAPSHOT,SOURCE,**kwargs)

    async def test_dry_run_no_writes_then_complete_publish_and_idempotency(self):
        result=await self.publish(dry_run=True)
        self.assertEqual((result['status'],result['observations'],result['observed_terms'],result['market_coverage']),('ready',4,3,1))
        self.assertEqual(self.d1.writes,0)
        result=await self.publish()
        self.assertEqual(result['status'],'active')
        current=dict(self.db.execute("SELECT * FROM search_keyword_monthly_metrics WHERE keyword_id=1 AND observed_month=?",[MONTH]).fetchone())
        self.assertEqual((current['observed_traffic'],current['search_volume'],current['observed_products'],current['observed_markets'],current['primary_term_id'],current['product_change_1m']),(301,700,2,1,12,1))
        label=dict(self.db.execute('SELECT * FROM search_keyword_brand_labels WHERE keyword_id=1').fetchone())
        self.assertEqual((label['classification_method'],label['confidence']),('manual_review',1))
        self.assertEqual(self.db.execute("SELECT first_seen_month,last_seen_month FROM search_keywords WHERE id=1").fetchone()[:],('2026-07-01',MONTH))
        self.assertEqual(self.db.execute("SELECT product_change_1m FROM search_keyword_monthly_metrics WHERE keyword_id!=1 AND observed_month=?",[MONTH]).fetchone()[0],None)
        writes=self.d1.writes
        self.assertEqual((await self.publish())['status'],'current')
        self.assertEqual(writes,self.d1.writes)
        self.assertEqual(self.db.execute("SELECT observed_traffic FROM search_keyword_monthly_metrics WHERE observed_month='2026-07-01'").fetchone()[0],80)

    async def test_interrupted_write_preserves_old_month_and_retry_rebuilds_partial_rows(self):
        execute=self.d1.execute
        async def interrupted(sql,params=None,**kwargs):
            if kwargs.get('operation')=='search_demand.search_keyword_monthly_metrics.write':
                raise RuntimeError('synthetic interruption')
            return await execute(sql,params,**kwargs)
        with patch.object(self.d1,'execute',interrupted):
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                await self.publish()
        self.assertEqual(await refresh.published_month(self.d1,SOURCE),'2026-07-01')
        self.assertEqual(self.db.execute('SELECT count(*) FROM search_keyword_observations').fetchone()[0],4)
        self.assertEqual((await self.publish())['status'],'active')
        self.assertEqual(self.db.execute('SELECT count(*) FROM search_keyword_observations').fetchone()[0],4)

    async def test_bad_persisted_values_and_changed_source_never_publish(self):
        validate=refresh.validate
        async def corrupt(d1,plan,source):
            self.db.execute('UPDATE search_keyword_monthly_metrics SET search_volume=9999 WHERE observed_month=?',[MONTH])
            await validate(d1,plan,source)
        with patch.object(refresh,'validate',corrupt):
            with self.assertRaisesRegex(RuntimeError,'differs'):
                await self.publish()
        self.assertEqual(await refresh.published_month(self.d1,SOURCE),'2026-07-01')
        make_plan=refresh.make_plan
        calls=0
        async def changed(*args):
            nonlocal calls
            calls+=1
            plan=await make_plan(*args)
            if calls==2:
                plan['fingerprint']='changed'
            return plan
        with patch.object(refresh,'make_plan',changed):
            with self.assertRaisesRegex(RuntimeError,'Source changed'):
                await self.publish()
        self.assertEqual(await refresh.published_month(self.d1,SOURCE),'2026-07-01')

    async def test_empty_month_unpublished_traffic_and_future_month_do_not_write(self):
        self.db.execute("UPDATE market_snapshot_versions SET status='candidate'")
        with self.assertRaisesRegex(RuntimeError,'publication gates'):
            await self.publish()
        self.db.execute("UPDATE market_snapshot_versions SET status='active'")
        self.db.execute("UPDATE domain_traffic_monthly SET metrics_json='{}'")
        with self.assertRaisesRegex(RuntimeError,'No keyword'):
            await self.publish()
        with self.assertRaisesRegex(ValueError,'closed'):
            await refresh.publish_month(self.d1,dict(id=2,traffic_month='2099-01-01'),SOURCE)
        self.assertEqual(self.d1.writes,0)

    async def test_sparse_page_boundaries_and_excluded_products(self):
        for id in range(10,215,2):
            self.add_product(id,'product'+str(id)+'.example',11,[{'name':'task '+str(id),'volume':id,'estimated_traffic':id}])
        self.db.execute("UPDATE tools SET status='rejected' WHERE id=2")
        with patch.object(refresh,'PAGE_SIZE',7):
            result=await self.publish()
        self.assertEqual(result['product_coverage'],104)
        self.assertEqual(self.db.execute('SELECT count(*) FROM search_keyword_observations WHERE product_id=2').fetchone()[0],0)

    async def test_scheduler_lock_throttle_and_catchup_without_new_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            env={'SEARCH_DEMAND_AUTO_PUBLISH_ENABLED':'1','SEARCH_DEMAND_STATE_ROOT':tmp}
            self.assertEqual((await refresh.refresh_completed_search_demand(self.d1,source=SOURCE,environ={},now=NOW))['status'],'disabled')
            with refresh.export_lock(Path(tmp)):
                self.assertEqual((await refresh.refresh_completed_search_demand(self.d1,source=SOURCE,environ=env,now=NOW))['status'],'locked')
            self.assertEqual((await refresh.refresh_completed_search_demand(self.d1,source=SOURCE,environ=env,now=NOW))['status'],'active')
            self.assertEqual((await refresh.refresh_completed_search_demand(self.d1,source=SOURCE,environ=env,now=NOW+1))['status'],'throttled')
            writes=self.d1.writes
            self.assertEqual((await refresh.refresh_completed_search_demand(self.d1,source=SOURCE,environ=env,now=NOW+3601))['status'],'current')
            self.assertEqual(writes,self.d1.writes)


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_hook_and_independent_failure_handling(self):
        import runner
        class Client:
            def __init__(self,*args): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
        for outcome in ({'status':'active'},RuntimeError('cost guard paused')):
            demand=AsyncMock(return_value=outcome) if isinstance(outcome,dict) else AsyncMock(side_effect=outcome)
            exports=AsyncMock(return_value={})
            with patch.object(runner,'D1Client',Client),patch.object(runner,'run_with_telemetry',AsyncMock(return_value={'claimed':0})),patch.object(runner,'refresh_completed_market_snapshot',AsyncMock(return_value={'status':'current'})),patch.object(runner,'refresh_completed_search_demand',demand),patch.object(runner,'export_ready_reports',exports),patch.object(runner,'log_info'),patch.object(runner,'log_error'):
                result=await runner.run_once(object())
            demand.assert_awaited_once()
            exports.assert_awaited_once()
            self.assertEqual(result['claimed'],0)
            self.assertEqual(result['search_demand_active' if isinstance(outcome,dict) else 'search_demand_blocked'],1)


class BrandTests(unittest.TestCase):
    def test_website_classifier_parity(self):
        cases=json.loads((Path(__file__).parent/'tests/fixtures/search-demand-brand-v2.json').read_text('utf-8'))
        for case in cases:
            with self.subTest(case=case['name']):
                self.assertEqual(classify(case['observations']),case['expected'])


if __name__=='__main__':
    unittest.main()
