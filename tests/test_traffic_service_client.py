"""SimilarWebClient now delegates fetching to the unified traffic service."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from workloads.traffic import client as client_module
from workloads.traffic.client import SimilarWebClient

TOKEN = "service-token-" + "x" * 32
RAW = {
    "SiteName": "sheeter.ai",
    "Engagments": {"Year": "2026", "Month": "10", "Visits": "8264", "BounceRate": "0.5"},
    "EstimatedMonthlyVisits": {"2026-08-01": 11043, "2026-09-01": 7199, "2026-10-01": 8264},
    "GlobalRank": {"Rank": 1234567},
    "TopCountryShares": [{"CountryCode": 840, "Value": 0.4}],
    "TrafficSources": {"Direct": 0.5, "Search": 0.3},
}


def service_item(status="ok", cached=False, raw=RAW, error=None):
    item = {"input": "sheeter.ai", "domain": "sheeter.ai", "status": status, "cached": cached,
            "fetched_at": "2026-11-11T00:00:00.000Z", "data": None, "error": error}
    if raw is not None or status == "no_data":
        item["raw"] = raw
    return {"request_id": "r", "completed": True, "summary": {}, "results": [item]}


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body)


def run(recorder: Recorder, month: str = "2026-10-01", token: str = TOKEN):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        return real_client(*args, **kwargs)

    config = SimpleNamespace(traffic_service_url="https://gateway.test/", traffic_service_token=token)
    with patch.object(client_module.httpx, "AsyncClient", factory):
        return asyncio.run(SimilarWebClient(config).fetch("https://www.Sheeter.ai/pricing", month))


class TrafficServiceClientTests(unittest.TestCase):
    def test_ok_payload_is_parsed_by_existing_parser(self):
        recorder = Recorder((200, service_item()))
        result = run(recorder)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.raw_payload, RAW)
        self.assertEqual(result.observed_latest_month, "2026-10-01")
        self.assertTrue(any(row.get("traffic_month") == "2026-10-01" for row in result.monthly_rows))
        request = recorder.requests[0]
        self.assertEqual(str(request.url), "https://gateway.test/v1/traffic")
        self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(json.loads(request.content), {"url": "sheeter.ai", "include_raw": True})

    def test_no_data_and_forbidden_and_errors(self):
        self.assertEqual(run(Recorder((200, service_item("no_data", raw=None)))).status, "no_data")
        forbidden = run(Recorder((200, service_item("error", raw=None, error={"code": "upstream_http_403"}))))
        self.assertEqual(forbidden.status, "forbidden")
        limited = run(Recorder((200, service_item("error", raw=None, error={"code": "upstream_http_429"}))))
        self.assertEqual((limited.status, limited.error), ("failed", "traffic_service:upstream_http_429"))
        unauthorized = run(Recorder((401, {"error": "unauthorized"})))
        self.assertEqual((unauthorized.status, unauthorized.error), ("failed", "traffic_service_http_401"))
        self.assertEqual(run(Recorder((200, {"results": []}))).error, "traffic_service_invalid_response")
        self.assertEqual(run(Recorder((200, service_item(raw=None)))).error, "traffic_service_missing_raw")

    def test_missing_token_never_calls_service(self):
        recorder = Recorder()
        result = run(recorder, token="")
        self.assertEqual((result.status, result.error), ("failed", "traffic_service_not_configured"))
        self.assertEqual(recorder.requests, [])

    def test_stale_cache_without_requested_month_bypasses_cache_once(self):
        recorder = Recorder((200, service_item(cached=True)), (200, service_item(cached=False)))
        result = run(recorder, month="2026-11-01")
        self.assertEqual(result.status, "no_data")
        self.assertEqual(len(recorder.requests), 2)
        self.assertTrue(json.loads(recorder.requests[1].content)["bypass_cache"])
        # Fresh (non-cached) results are trusted without a second call.
        fresh = Recorder((200, service_item(cached=False)))
        run(fresh, month="2026-11-01")
        self.assertEqual(len(fresh.requests), 1)


if __name__ == "__main__":
    unittest.main()
