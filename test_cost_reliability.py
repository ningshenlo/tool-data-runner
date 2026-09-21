import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import d1_costguard as guard
from sitemap_monitor.cli import run_with_guard_backoff

class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard._pauses.clear()
        guard._paused_until = 0

    def test_minute_throttle_is_scoped_and_honors_retry_after(self):
        response = SimpleNamespace(status_code=429, headers={"Retry-After":"2"}, json=lambda:{"errors":[{"code":"minute_read_budget_exhausted"}]})
        with patch.object(guard.time, "monotonic", return_value=100):
            guard.observe_response(guard.GUARD_URL, response, "assets-worker")
            with self.assertRaises(guard.CostGuardPause) as caught:
                guard.before_request(guard.GUARD_URL, "assets-worker")
            self.assertEqual(caught.exception.retry_seconds, 2)
            self.assertEqual(caught.exception.recovery, "automatic")
            guard.before_request(guard.GUARD_URL, "sitemap-worker")
        with patch.object(guard.time, "monotonic", return_value=103):
            guard.before_request(guard.GUARD_URL, "assets-worker")

    def test_transport_auth_and_query_blocks_are_distinct(self):
        for status, payload, recovery, delay in [(503, {}, "automatic", 15),(401, {}, "operator", 300),(429, {"error":"query_circuit_open"}, "operator", 300)]:
            response=SimpleNamespace(status_code=status, headers={}, json=lambda:payload)
            guard.observe_response(guard.GUARD_URL, response, "sitemap-worker")
            error=guard.pause_error("sitemap-worker")
            self.assertEqual(error.recovery, recovery)
            self.assertLessEqual(error.retry_seconds, delay)

    async def test_sitemap_loop_waits_for_actual_throttle_not_five_minutes(self):
        pause=guard.CostGuardPause("minute_read_budget_exhausted",2,"automatic")
        with patch("sitemap_monitor.cli.run",new=AsyncMock(side_effect=[pause,None])), patch("sitemap_monitor.cli.asyncio.sleep",new=AsyncMock()) as sleep:
            await run_with_guard_backoff(SimpleNamespace(loop=True))
            sleep.assert_awaited_once_with(2)
