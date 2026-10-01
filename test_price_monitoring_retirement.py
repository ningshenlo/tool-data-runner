import asyncio
import contextlib
import io
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import runner

class PriceRetirementTests(unittest.IsolatedAsyncioTestCase):
    def test_retired_cli_cannot_start_collection_or_approval(self):
        for flag in ('--pricing', '--approve-pricing'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                runner.parse_args([flag])
            self.assertEqual(result.exception.code, 2)
        self.assertFalse(hasattr(runner, 'D1PricingStore'))
        self.assertFalse(hasattr(runner, 'run_pricing_once'))

    def test_compose_has_no_retired_price_service_or_command(self):
        compose = pathlib.Path(__file__).with_name('docker-compose.dokploy.yml').read_text(encoding='utf-8')
        self.assertNotIn('PRICING_', compose)
        self.assertNotIn('--pricing', compose)
        self.assertNotIn('pricing-monitor-worker', compose)
        for service in ('periodic-facts-worker', 'assets-worker', 'taxonomy-worker', 'sitemap-monitor-worker'):
            self.assertIn('  ' + service + ':', compose)

    def test_legacy_all_profile_excludes_pricing(self):
        args = runner.parse_args(['--all', '--loop'])
        _, workloads = runner.runtime_profile_for_args(args)
        self.assertNotIn('pricing', workloads)
        self.assertTrue({'traffic', 'assets', 'domain_state'} <= set(workloads))

    async def test_all_loop_still_runs_other_collectors(self):
        config = SimpleNamespace(asset_limit=3, limit=4, domain_state_limit=5,
            domain_state_poll_interval_seconds=2, taxonomy_auto_enabled=False,
            taxonomy_interval_seconds=30)
        with patch.object(runner, 'run_assets_loop', new_callable=AsyncMock) as assets, \
             patch.object(runner, 'run_loop', new_callable=AsyncMock) as traffic, \
             patch.object(runner, 'run_domain_state_loop', new_callable=AsyncMock) as domains:
            await runner.run_all_loop(config, None, 300)
            assets.assert_awaited_once_with(config,3,150)
            traffic.assert_awaited_once_with(config,4,300)
            domains.assert_awaited_once_with(config,5,2)

    def test_homepage_recovery_keeps_shared_browser_headers(self):
        headers = runner.browser_request_headers('https://example.com/product')
        self.assertEqual(headers['Referer'], 'https://example.com/')
        self.assertTrue(headers['User-Agent'])

if __name__ == '__main__': unittest.main()
