from dataclasses import replace
import unittest

from sitemap_monitor.cloudflare import CloudflareD1MetadataStore
from sitemap_monitor.models import CheckOutcome, ResourceState
from test_sitemap_monitor import SqliteD1ClientDouble


class CompactResourceWritesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = SqliteD1ClientDouble()
        self.store = CloudflareD1MetadataStore(self.client, compact_resource_runs=True)
        await self.store.ensure_site('site', 'https://example.com/', 1000)
        self.state = ResourceState(
            content_hash='a' * 64, current_state_key='state/site/resource.jsonl.gz',
            etag='v1', last_modified=None, metadata_hash='b' * 64,
            resource_id='resource', sitemap_kind='urlset',
            url='https://example.com/sitemap.xml', url_count=2, urlset_hash='c' * 64,
        )
        await self.save(self.state, 1000)
        self.statements = []
        self.client.connection.set_trace_callback(self.statements.append)

    async def asyncTearDown(self):
        self.client.connection.close()

    async def save(self, state, checked_at, parent=None, changed=False):
        await self.store.save_resource('site', state, canonical_fetch_url=state.url,
                                       parent_id=parent, checked_at_ms=checked_at, changed=changed)

    async def test_unchanged_resource_keeps_validators_without_rewriting_indexes_or_site(self):
        state = replace(self.state, etag='v2', metadata_hash='d' * 64)
        await self.save(state, 2000)
        self.assertEqual(await self.store.get_resource('site', state.url), state)
        writes = [q for q in self.statements if q.lstrip().upper().startswith(('UPDATE', 'INSERT'))]
        self.assertEqual(len(writes), 1)
        self.assertIn('UPDATE sitemap_resources', writes[0])
        self.assertNotIn('SET parent_id', writes[0])
        self.assertNotIn('INSERT', writes[0])

    async def test_parent_changes_and_real_changes_are_preserved(self):
        parent = replace(self.state, resource_id='parent', url='https://example.com/index.xml')
        await self.save(parent, 1500)
        await self.save(self.state, 2000, parent='parent')
        row = self.client.connection.execute('SELECT parent_id FROM sitemap_resources WHERE id=?', ('resource',)).fetchone()
        self.assertEqual(row['parent_id'], 'parent')
        await self.save(self.state, 3000, changed=True)
        row = self.client.connection.execute('SELECT last_changed_at FROM sitemap_sites WHERE id=?', ('site',)).fetchone()
        self.assertEqual(row['last_changed_at'], 3000)

    async def test_304_resets_failure_streaks_without_per_resource_site_writes(self):
        for missing in (True, False):
            await self.store.mark_failure('site', 'resource', self.state.url,
                canonical_fetch_url=self.state.url, parent_id=None, checked_at_ms=1500, missing=missing)
        failed = self.client.connection.execute('SELECT missing_streak,error_streak FROM sitemap_resources').fetchone()
        self.assertEqual(tuple(failed), (1, 1))
        await self.store.mark_not_modified('site', 'resource', canonical_fetch_url=self.state.url,
            checked_at_ms=2000, etag='v2', last_modified=None)
        resource = self.client.connection.execute('SELECT missing_streak,error_streak,last_checked_at,etag FROM sitemap_resources').fetchone()
        self.assertEqual(tuple(resource), (0, 0, 2000, 'v2'))
        self.assertFalse(any('UPDATE sitemap_sites' in statement for statement in self.statements))

    async def test_scan_completion_updates_site_once_and_stale_completion_is_fenced(self):
        due = (await self.store.claim_due_sites(lease_duration_ms=1000, lease_owner='scheduler', limit=1, now_ms=1000))[0]
        await self.store.ensure_job(due, max_attempts=3, now_ms=1000)
        job = (await self.store.claim_jobs(lease_duration_ms=1000, lease_owner='worker', limit=1, now_ms=1001))[0]
        self.assertTrue(await self.store.finish_job_success(job, finished_at_ms=1500, next_check_at_ms=5000))
        self.assertFalse(await self.store.finish_job_success(job, finished_at_ms=1600, next_check_at_ms=6000))
        row = self.client.connection.execute('SELECT last_checked_at,next_check_at FROM sitemap_sites').fetchone()
        self.assertEqual(tuple(row), (1500, 5000))

    async def test_compaction_keeps_baselines_changes_errors_and_standalone_runs(self):
        outcome = CheckOutcome(added_count=0, bytes_downloaded=100, diff_key=None,
            error_code=None, final_url=self.state.url, http_status=200, modified_count=0,
            resource_id='resource', result='baseline', sitemap_kind='urlset',
            state_key=self.state.current_state_key, url=self.state.url, url_count=2, removed_count=0)
        for result in ('baseline', 'changed', 'failed', 'not_modified', 'semantic_unchanged'):
            await self.store.record_run('site', replace(outcome, result=result),
                finished_at_ms=2000, run_key=result, started_at_ms=1000, site_scan_id='scan')
        await self.store.record_run('site', replace(outcome, result='not_modified'),
            finished_at_ms=2000, run_key='standalone', started_at_ms=1000)
        rows = self.client.connection.execute('SELECT result FROM sitemap_runs ORDER BY result').fetchall()
        self.assertEqual([row['result'] for row in rows], ['baseline', 'changed', 'failed', 'not_modified'])


if __name__ == '__main__':
    unittest.main()
