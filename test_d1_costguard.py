import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import d1_costguard as guard

class CostGuardTests(unittest.TestCase):
    def setUp(self):
        guard._paused_until = 0
        guard._pauses.clear()
        self.direct = f"https://api.cloudflare.com/client/v4/accounts/account/d1/database/{guard.PRODUCTION_DATABASE}/query"

    def test_production_always_uses_guard_without_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(guard.guard_endpoint(self.direct), guard.GUARD_URL)

    def test_no_arbitrary_token_destination_or_wrong_database(self):
        with patch.dict(os.environ, {"CLOUDFLARE_D1_GUARD_URL": "https://example.com/query"}, clear=True):
            with self.assertRaises(ValueError): guard.guard_endpoint(self.direct)
        with patch.dict(os.environ, {"CLOUDFLARE_D1_GUARD_REQUIRED": "1"}, clear=True):
            with self.assertRaises(ValueError): guard.guard_endpoint("https://api.cloudflare.com/database/other/query")

    def test_generic_throttle_uses_bounded_default_delay(self):
        with patch.object(guard.time, "monotonic", return_value=100):
            self.assertTrue(guard.observe_response(guard.GUARD_URL, SimpleNamespace(status_code=429)))
            with self.assertRaises(RuntimeError): guard.before_request(guard.GUARD_URL)
        with patch.object(guard.time, "monotonic", return_value=161):
            guard.before_request(guard.GUARD_URL)

    def test_guard_unavailable_never_switches_to_direct_url(self):
        with patch.object(guard.time, "monotonic", return_value=100):
            guard.observe_response(guard.GUARD_URL, SimpleNamespace(status_code=503))
            with self.assertRaises(RuntimeError): guard.before_request(guard.GUARD_URL)
            self.assertEqual(guard.guard_endpoint(self.direct), guard.GUARD_URL)

if __name__ == "__main__": unittest.main()
