import json,os,unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
import httpx
from runner import D1Client,D1RequestError
from sitemap_monitor.cloudflare import CloudflareD1Client,CloudflareApiError

class DirectD1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_native_auth_and_batch_are_preserved_despite_stale_guard_env(self):
        for sitemap in [False,True]:
            with patch.dict(os.environ,{'CLOUDFLARE_D1_GUARD_URL':'https://retired.invalid/query','CLOUDFLARE_D1_GUARD_REQUIRED':'1'}):
                config=SimpleNamespace(cloudflare_account_id='account',cloudflare_d1_database_id='3025073a-9e43-4000-b5c9-9e975ed27b7b',cloudflare_api_token='test-token')
                client=CloudflareD1Client(account_id='account',database_id=config.cloudflare_d1_database_id,api_token='test-token') if sitemap else D1Client(config)
            await client.client.aclose();requests=[]
            def handler(request):
                requests.append(request)
                self.assertEqual(str(request.url),f'https://api.cloudflare.com/client/v4/accounts/account/d1/database/{config.cloudflare_d1_database_id}/query')
                self.assertEqual(request.headers['Authorization'],'Bearer test-token')
                self.assertNotIn('X-Sigpik-Service',request.headers)
                return httpx.Response(200,json={'success':True,'result':[{'success':True,'results':[{'value':1}]},{'success':True,'results':[{'value':2}]}]})
            client.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                body={'batch':[{'sql':'SELECT ?','params':[1]},{'sql':'SELECT ?','params':[2]}]}
                result=await client._request(body)
                self.assertEqual(len(result),2);self.assertEqual(len(requests),1);self.assertEqual(json.loads(requests[0].content),body)
            finally:await client.close()

    async def test_transient_d1_errors_recover_with_existing_bounded_retry(self):
        config=SimpleNamespace(cloudflare_account_id='account',cloudflare_d1_database_id='db',cloudflare_api_token='test-token')
        client=D1Client(config);await client.client.aclose();responses=[503,200]
        client.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(responses.pop(0),json={'success':True,'result':[{'success':True,'results':[]}]})))
        try:
            with patch('runner.asyncio.sleep',new=AsyncMock()) as sleep:
                await client._request({'sql':'SELECT 1'},retry_transient=True)
                sleep.assert_awaited_once();self.assertEqual(responses,[])
        finally:await client.close()
