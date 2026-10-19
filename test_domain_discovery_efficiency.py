import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import runner


class DomainDiscoveryEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def batch(self, claimed, discover_due=True):
        store = SimpleNamespace(claim_due_tasks=AsyncMock(side_effect=claimed),
            queue_due_tasks=AsyncMock(return_value=1),
            requeue_missing_credential_tasks=AsyncMock(return_value=0))
        config = SimpleNamespace(domain_state_limit=2, concurrency=1, ahref_api_key='',
            domain_state_max_age_days=30, runner_instance_id='test', max_retries=3)
        with patch.object(runner, 'D1DomainStateStore', return_value=store), \
             patch.object(runner, 'DomainStateClient'), \
             patch.object(runner, 'process_domain_state', AsyncMock(return_value='done')), \
             patch.object(runner, 'log_info'), patch.object(runner, 'log_task_result'):
            result = await runner._run_domain_state_once(config, object(), discover_due=discover_due)
        return store, result

    async def test_full_backlog_skips_catalog_discovery(self):
        tasks = [SimpleNamespace(normalized_domain=f'{i}.example') for i in range(2)]
        store, counts = await self.batch([tasks])
        store.queue_due_tasks.assert_not_awaited()
        self.assertEqual(counts['claimed'], 2)
        self.assertEqual(counts['discovery_ran'], 0)

    async def test_partial_batch_discovers_only_remaining_capacity(self):
        tasks = [SimpleNamespace(normalized_domain=f'{i}.example') for i in range(2)]
        store, counts = await self.batch([[tasks[0]], [tasks[1]]])
        store.queue_due_tasks.assert_awaited_once_with(1, 30)
        self.assertEqual(store.claim_due_tasks.await_args_list[1].args, (1, 'test'))
        self.assertEqual(counts['done'], 2)
        self.assertEqual(counts['discovery_ran'], 1)

    async def test_idle_discovery_backoff_still_claims_known_due_work(self):
        task = SimpleNamespace(normalized_domain='retry.example')
        store, counts = await self.batch([[task]], discover_due=False)
        store.queue_due_tasks.assert_not_awaited()
        store.claim_due_tasks.assert_awaited_once_with(2, 'test')
        self.assertEqual(counts['done'], 1)

    def test_empty_discovery_backoff_is_bounded_and_resets_with_work(self):
        policy = runner.DomainDiscoveryBackoff()
        self.assertTrue(policy.due(0))
        now = 0
        for delay in [60, 120, 240, 480, 900, 900]:
            policy.observe({'discovery_ran': 1}, now)
            self.assertEqual(policy.next_discovery_at, now + delay)
            self.assertFalse(policy.due(now + delay - 1))
            now += delay
            self.assertTrue(policy.due(now))
        policy.observe({'claimed': 1}, now)
        self.assertEqual(policy.empty_scans, 0)
        self.assertTrue(policy.due(now))


if __name__ == '__main__':
    unittest.main()
