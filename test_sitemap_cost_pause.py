import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sitemap_monitor.cli import run_with_guard_backoff

class PauseLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_waits_in_process_then_checks_operator_recovery(self):
        with patch('sitemap_monitor.cli.run', new=AsyncMock(side_effect=[RuntimeError('Cloudflare D1 request failed: D1 cost guard stopped'), None])) as run, patch('sitemap_monitor.cli.asyncio.sleep', new=AsyncMock()) as sleep:
            await run_with_guard_backoff(SimpleNamespace(loop=True))
            self.assertEqual(run.await_count, 2)
            sleep.assert_awaited_once_with(300)

    async def test_one_shot_and_unrelated_errors_still_fail(self):
        for looping, error in [(False, 'D1 cost guard stopped'), (True, 'invalid configuration')]:
            with patch('sitemap_monitor.cli.run', new=AsyncMock(side_effect=RuntimeError(error))), patch('sitemap_monitor.cli.asyncio.sleep', new=AsyncMock()) as sleep:
                with self.assertRaises(RuntimeError):
                    await run_with_guard_backoff(SimpleNamespace(loop=looping))
                sleep.assert_not_awaited()
