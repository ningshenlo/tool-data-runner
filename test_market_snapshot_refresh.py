import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import market_snapshot_refresh as refresh
import runner
from report_exports import export_lock
import test_runner_stores as stores

NOW = 1789459200  # September 2026
SOURCE = runner.TRAFFIC_SOURCE
CTES = 'visible_tools AS (SELECT id, normalized_domain, primary_term_id FROM candidates)'


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE candidates(id INTEGER, normalized_domain TEXT, primary_term_id INTEGER);
          CREATE TABLE domain_traffic_monthly(normalized_domain TEXT, source TEXT, traffic_month TEXT, visits INTEGER, metrics_schema_version INTEGER, updated_at TEXT);
          CREATE TABLE traffic_tasks(normalized_domain TEXT, source TEXT, traffic_month TEXT, status TEXT, generation INTEGER, updated_at TEXT);
          CREATE TABLE market_catalog_eligibility_revision(id INTEGER, revision INTEGER);
          INSERT INTO market_catalog_eligibility_revision VALUES(1,0);
          INSERT INTO candidates VALUES(1,'one.example',NULL);
        ''')
        self.d1 = stores.FakeD1(self.db)

    def tearDown(self):
        self.db.close()

    async def check(self):
        return await refresh.month_readiness(self.d1, '2026-08-01', source=SOURCE, visible_tools_ctes=CTES)

    def traffic(self, domain='one.example', visits=0, schema=2, month='2026-08-01'):
        self.db.execute('INSERT INTO domain_traffic_monthly VALUES(?,?,?,?,?,?)', [domain,SOURCE,month,visits,schema,'time'])

    def task(self, status, domain='one.example'):
        self.db.execute('DELETE FROM traffic_tasks WHERE normalized_domain=?',[domain])
        self.db.execute('INSERT INTO traffic_tasks VALUES(?,?,?,?,?,?)',[domain,SOURCE,'2026-08-01',status,1,'time'])

    async def test_unqueued_products_block_and_zero_is_real_data(self):
        self.assertEqual((await self.check())['status'], 'waiting')
        self.traffic(visits=0)
        self.assertEqual((await self.check())['measured'], 1)
        self.assertEqual((await self.check())['status'], 'ready')
        self.db.execute("INSERT INTO candidates VALUES(2,'two.example',NULL)")
        self.assertEqual((await self.check())['status'], 'waiting')
        self.task('no_data', 'two.example')
        result = await self.check()
        self.assertEqual((result['status'],result['terminal_absence']),('ready',1))

    async def test_failed_and_running_are_not_completion_even_with_data(self):
        self.traffic()
        for status, expected in [('queued','waiting'),('processing','waiting'),('failed','blocked'),('sync_failed','blocked'),('done','ready'),('no_data','blocked'),('forbidden','blocked')]:
            self.task(status)
            self.assertEqual((await self.check())['status'],expected,status)

    async def test_missing_materialization_and_wrong_schema_block(self):
        self.task('done')
        self.assertEqual((await self.check())['status'],'blocked')
        self.traffic(schema=1)
        self.assertEqual((await self.check())['status'],'blocked')

    async def test_absent_previous_month_is_not_zero_or_cross_month_comparison(self):
        self.traffic(visits=100)
        self.traffic(visits=50,month='2026-06-01')
        result = await self.check()
        self.assertEqual(result['status'],'ready')
        self.db.execute("UPDATE domain_traffic_monthly SET visits=60 WHERE traffic_month='2026-06-01'")
        self.assertEqual((await self.check())['fingerprint'],result['fingerprint'])
        self.traffic(visits=70,month='2026-07-01')
        self.assertNotEqual((await self.check())['fingerprint'],result['fingerprint'])


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, root, *, d1=None, now=NOW, enabled=True, build=None, activate=None):
        return await refresh.refresh_completed_market_snapshot(
          d1 or self.d1, source=SOURCE,visible_tools_ctes=CTES,
          build=build or self.build,activate=activate or self.activate,now=now,
          environ={'MARKET_SNAPSHOT_AUTO_PUBLISH_ENABLED':'1' if enabled else '0','MARKET_SNAPSHOT_STATE_ROOT':str(root)})

    def setUp(self):
        self.d1=type('D1',(),{'query':AsyncMock(return_value=[{'traffic_month':'2026-08-01'}])})()
        self.build=AsyncMock(return_value={'snapshot_id':9})
        self.activate=AsyncMock(return_value={'snapshot_id':9,'coverage':{'traffic_tools':4000}})
        self.ready={'status':'ready','fingerprint':'fixed','eligible':4200,'measured':4000,'terminal_absence':200,'waiting':0,'blocked':0}

    async def test_disabled_has_no_io_and_success_rechecks_then_activates(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refresh,'month_readiness',AsyncMock(return_value=self.ready)) as readiness:
            root=Path(tmp)/'state'
            self.assertEqual((await self.call(root,enabled=False))['status'],'disabled')
            self.assertFalse(root.exists())
            result=await self.call(root)
            self.assertEqual(result['status'],'active')
            self.build.assert_awaited_once_with(self.d1,'2026-08-01',activate=False)
            self.activate.assert_awaited_once_with(self.d1,9)
            self.assertEqual(readiness.await_count,2)
            self.assertEqual((await self.call(root,now=NOW+1))['status'],'throttled')
            self.d1.query.return_value=[] # D1, not local state, proves publication after restart.
            self.assertEqual((await self.call(root,now=NOW+3601))['status'],'current')
            self.assertEqual(self.build.await_count,1)

    async def test_incomplete_month_waits_and_older_ready_month_can_publish(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refresh,'month_readiness',AsyncMock(return_value={**self.ready,'status':'waiting'})) as readiness:
            self.assertEqual((await self.call(tmp))['status'],'waiting')
            self.build.assert_not_awaited()
            self.d1.query.return_value=[{'traffic_month':'2026-08-01'},{'traffic_month':'2026-07-01'}]
            readiness.side_effect=[{**self.ready,'status':'waiting'},self.ready,self.ready]
            self.assertEqual((await self.call(tmp,now=NOW+3601))['status'],'active')
            self.build.assert_awaited_once_with(self.d1,'2026-07-01',activate=False)

    async def test_changed_collection_and_failed_build_never_activate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refresh,'month_readiness',AsyncMock(side_effect=[self.ready,{**self.ready,'fingerprint':'new'}])):
            self.assertEqual((await self.call(tmp))['status'],'waiting')
            self.activate.assert_not_awaited()
        with tempfile.TemporaryDirectory() as tmp, patch.object(refresh,'month_readiness',AsyncMock(return_value=self.ready)):
            self.build.side_effect=RuntimeError('Build failed')
            self.assertEqual((await self.call(tmp))['status'],'blocked')
            self.activate.assert_not_awaited()
            self.assertIn('Build failed',json.loads((Path(tmp)/'publication-status.json').read_text())['reason'])

    async def test_activation_error_retries_and_shared_lock_prevents_duplicate_build(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refresh,'month_readiness',AsyncMock(return_value=self.ready)):
            self.activate.side_effect=RuntimeError('Coverage gate failed')
            self.assertEqual((await self.call(tmp))['status'],'blocked')
            self.assertEqual((await self.call(tmp,now=NOW+1))['status'],'throttled')
            with export_lock(Path(tmp)):
                self.assertEqual((await self.call(tmp,now=NOW+3601))['status'],'locked')
            self.activate.side_effect=None
            self.assertEqual((await self.call(tmp,now=NOW+3601))['status'],'active')

    async def test_cancellation_propagates(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refresh,'month_readiness',AsyncMock(return_value=self.ready)):
            self.build.side_effect=asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await self.call(tmp)


class ActivationSqlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Synthetic schema for the publication boundary. This suite also runs
        # in collector-only CI, without the sibling website or production data.
        self.connection=sqlite3.connect(':memory:')
        self.connection.row_factory=sqlite3.Row
        self.connection.executescript('''
          CREATE TABLE market_catalog_eligibility_revision(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
          CREATE TABLE market_snapshot_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
            traffic_source TEXT NOT NULL DEFAULT 'similarweb', traffic_month TEXT NOT NULL,
            catalog_eligibility_revision INTEGER NOT NULL DEFAULT 0,
            activated_at TEXT, retired_at TEXT, updated_at TEXT
          );
          CREATE UNIQUE INDEX active_source ON market_snapshot_versions(traffic_source) WHERE status='active';
          CREATE TABLE traffic_month_release_checks(source TEXT, traffic_month TEXT, status TEXT, probe_domain TEXT);
          CREATE TABLE domain_traffic_monthly(normalized_domain TEXT, source TEXT, traffic_month TEXT, visits INTEGER);
        ''')
        self.d1=stores.FakeD1(self.connection)

    def tearDown(self):
        self.connection.close()

    async def test_selects_only_released_closed_months_newer_than_active(self):
        self.connection.execute("INSERT INTO market_snapshot_versions(status,traffic_month) VALUES('active','2026-07-01')")
        for month,status in [('2026-06-01','available'),('2026-07-01','available'),('2026-08-01','available'),('2026-09-01','available'),('2026-10-01','unavailable')]:
            self.connection.execute('INSERT INTO traffic_month_release_checks(source,traffic_month,status,probe_domain) VALUES(?,?,?,?)',[SOURCE,month,status,'one.example'])
        readiness=AsyncMock(return_value={'status':'waiting'})
        build=AsyncMock()
        with patch.object(refresh,'month_readiness',readiness):
            result=await refresh.refresh_month(self.d1,source=SOURCE,visible_tools_ctes=CTES,build=build,activate=AsyncMock(),state={},save=lambda _:None,now=NOW)
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(readiness.await_count,1)
        self.assertEqual(readiness.call_args.args[1],'2026-08-01')
        build.assert_not_awaited()

    async def test_delayed_builder_cannot_roll_back_a_newer_active_month(self):
        await runner.ensure_market_catalog_eligibility_revision(self.d1)
        for status,month in [('active','2026-08-01'),('candidate','2026-07-01')]:
            self.connection.execute('INSERT INTO market_snapshot_versions(status,traffic_month) VALUES(?,?)',[status,month])
        coverage={key:100 for key in ['total_tools','traffic_tools','search_tools','paid_tools','ai_tools','dr_tools','dr_comparable_tools','country_rows','country_tools','country_ai_rows','country_ai_tools']}
        with patch.object(runner,'get_market_snapshot_coverage',AsyncMock(return_value=coverage)),patch.object(runner,'validate_market_snapshot_facet_rollups'):
            with self.assertRaisesRegex(RuntimeError,'activation did not commit'):
                await runner.activate_market_snapshot_from_d1(self.d1,2)
        self.assertEqual([tuple(r) for r in self.connection.execute('SELECT id,status FROM market_snapshot_versions ORDER BY id')],[(1,'active'),(2,'candidate')])

    async def test_baseline_is_previous_calendar_month_not_last_available(self):
        self.connection.execute("INSERT INTO traffic_month_release_checks(source,traffic_month,status,probe_domain) VALUES(?,'2026-06-01','available','one.example')",[SOURCE])
        self.connection.execute("INSERT INTO domain_traffic_monthly(normalized_domain,source,traffic_month,visits) VALUES('one.example',?,'2026-06-01',100)",[SOURCE])
        self.assertEqual(await runner.resolve_market_snapshot_months(self.d1,'2026-08-01'),('2026-08-01',None))


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_collection_publishes_and_publication_failure_does_not_break_exports(self):
        class Client:
            def __init__(self,*args): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
        for outcome in [{'status':'active'},RuntimeError('State volume unavailable')]:
            publisher=AsyncMock(return_value=outcome) if isinstance(outcome,dict) else AsyncMock(side_effect=outcome)
            exports=AsyncMock(return_value={})
            with patch.object(runner,'D1Client',Client),patch.object(runner,'run_with_telemetry',AsyncMock(return_value={'claimed':0})),patch.object(runner,'refresh_completed_market_snapshot',publisher),patch.object(runner,'export_ready_reports',exports),patch.object(runner,'log_info'),patch.object(runner,'log_error'):
                result=await runner.run_once(object())
            self.assertEqual(result['claimed'],0)
            publisher.assert_awaited_once()
            exports.assert_awaited_once()
            self.assertEqual(result['market_snapshot_active' if isinstance(outcome,dict) else 'market_snapshot_blocked'],1)


if __name__=='__main__':
    unittest.main()
