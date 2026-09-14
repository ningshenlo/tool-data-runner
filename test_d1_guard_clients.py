import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import d1_costguard as guard
from runner import D1Client, D1RequestError
from sitemap_monitor.cloudflare import CloudflareD1Client, CloudflareApiError


class GuardClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard._paused_until = 0
        self.environment = patch.dict(os.environ, {"RUNNER_SERVICE_NAME": "assets-worker"}, clear=True)
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        guard._paused_until = 0

    async def test_both_clients_use_guard_and_budget_rejections_never_retry(self):
        for sitemap in [False, True]:
            guard._paused_until = 0
            config = SimpleNamespace(cloudflare_account_id="account", cloudflare_d1_database_id=guard.PRODUCTION_DATABASE, cloudflare_api_token="test-token")
            client = CloudflareD1Client(account_id="account", database_id=guard.PRODUCTION_DATABASE, api_token="test-token") if sitemap else D1Client(config)
            await client.client.aclose()
            requests = []

            def handler(request):
                requests.append(request)
                self.assertEqual(str(request.url), guard.GUARD_URL)
                self.assertEqual(request.headers["X-Sigpik-Service"], "sitemap-worker" if sitemap else "assets-worker")
                return httpx.Response(429, json={"success": False, "errors": [{"message": "budget_paused"}]})

            client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(CloudflareApiError if sitemap else D1RequestError):
                    if sitemap:
                        await client._request({"sql": "SELECT 1", "params": []})
                    else:
                        await client._request({"sql": "SELECT 1", "params": []}, retry_transient=True)
                with self.assertRaises(RuntimeError):
                    await client._request({"sql": "SELECT 1", "params": []})
                self.assertEqual(len(requests), 1)
            finally:
                await client.close()
