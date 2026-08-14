import unittest

from anti_bot_signatures import detect_anti_bot_page, detect_anti_bot_text


class AntiBotSignatureTests(unittest.TestCase):
    def test_detects_firewall_access_block_that_polluted_janitorai(self):
        detected = detect_anti_bot_text(
            "Access has been blocked by the firewall. The company is working on improving security measures."
        )
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected.state, "access_denied")
        self.assertEqual(detected.provider, "generic_waf")
        self.assertEqual(detected.code, "waf_access_blocked_firewall")

    def test_detects_cloudflare_body_challenge_not_only_title(self):
        detected = detect_anti_bot_page(
            "<html><body><main>Just wait... Checking your browser before accessing</main></body></html>",
            page_title="Example",
            http_status=200,
        )
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected.provider, "cloudflare")
        self.assertIn("cf_checking_browser", detected.matched_codes)

    def test_detects_vendor_html_markers(self):
        fixtures = {
            "cloudflare": '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>',
            "imperva": '<script src="/_Incapsula_Resource?SWJIYLWA=1"></script>',
            "datadome": '<div id="datadome-captcha"></div>',
            "human_perimeterx": '<div id="px-captcha"></div>',
        }
        for provider, body in fixtures.items():
            with self.subTest(provider=provider):
                detected = detect_anti_bot_page(body)
                self.assertIsNotNone(detected)
                assert detected is not None
                self.assertEqual(detected.provider, provider)

    def test_does_not_flag_real_security_product_copy(self):
        detected = detect_anti_bot_text(
            "AI security platform for detecting model attacks, governing access, and monitoring compliance."
        )
        self.assertIsNone(detected)

    def test_http_status_is_a_hard_signal(self):
        detected = detect_anti_bot_page("", http_status=403)
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected.code, "http_403")
        self.assertEqual(detected.state, "access_denied")


if __name__ == "__main__":
    unittest.main()
