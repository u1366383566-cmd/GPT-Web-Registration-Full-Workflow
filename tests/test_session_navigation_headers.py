# -*- coding: utf-8 -*-
import unittest

from config.browser import build_browser_environment
from core.session import BrowserSession


class SessionNavigationHeaderTests(unittest.TestCase):
    def test_vietnam_exit_uses_matching_locale_and_timezone(self):
        profile = build_browser_environment({
            "country": "VN",
            "timezone": "Asia/Ho_Chi_Minh",
            "city": "Bien Hoa",
        })

        self.assertEqual(profile["navigator_language"], "vi-VN")
        self.assertTrue(profile["accept_language"].startswith("vi-VN,vi;"))
        self.assertEqual(profile["timezone_iana"], "Asia/Ho_Chi_Minh")
        self.assertEqual(profile["timezone_offset_minutes"], 420)

    def test_proxy_country_without_named_profile_is_derived_from_geo(self):
        profile = build_browser_environment({
            "country": "TH",
            "timezone": "Asia/Bangkok",
            "city": "Bangkok",
        })

        self.assertEqual(profile["locale_profile"], "geo:th")
        self.assertEqual(profile["navigator_language"], "th-TH")
        self.assertTrue(profile["accept_language"].startswith("th-TH,th;"))
        self.assertEqual(profile["timezone_iana"], "Asia/Bangkok")
        self.assertEqual(profile["timezone_offset_minutes"], 420)

    def test_unknown_proxy_country_does_not_leak_fixed_local_locale(self):
        profile = build_browser_environment({
            "country": "XX",
            "timezone": "UTC",
        })

        self.assertEqual(profile["locale_profile"], "geo:xx")
        self.assertEqual(profile["navigator_language"], "en-US")
        self.assertEqual(profile["timezone_iana"], "UTC")

    def test_geo_country_name_is_normalized_to_iso_code(self):
        geo = BrowserSession._normalize_geo_response({
            "country": "Japan",
            "country_code": "JP",
            "timezone": "Asia/Tokyo",
        })
        self.assertEqual(geo["country"], "JP")

        geo_without_code = BrowserSession._normalize_geo_response({"country": "Vietnam"})
        self.assertEqual(geo_without_code["country"], "VN")

    def _session_stub(self):
        session = object.__new__(BrowserSession)
        session.browser_profile = {
            "user_agent": "Mozilla/5.0 Test Chrome/146.0.0.0 Safari/537.36",
            "accept_language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "send_client_hints": True,
            "sec_ch_ua": '"Google Chrome";v="146", "Chromium";v="146"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"',
        }
        return session

    def test_cross_site_auth_navigation_uses_native_document_headers(self):
        headers = self._session_stub().get_auth_navigate_headers(
            referer="https://chatgpt.com/"
        )

        self.assertEqual(headers["sec-fetch-site"], "cross-site")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertNotIn("cache-control", headers)
        self.assertEqual(
            headers["accept-language"],
            "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        )
        self.assertNotIn("origin", headers)
        self.assertFalse(any(key.startswith("x-datadog-") for key in headers))

    def test_external_oauth_navigation_has_no_fake_referer(self):
        headers = self._session_stub().get_auth_navigate_headers(referer="")

        self.assertEqual(headers["sec-fetch-site"], "none")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertNotIn("referer", headers)

    def test_initial_chatgpt_navigation_matches_address_bar_navigation(self):
        headers = self._session_stub().get_chatgpt_navigate_headers(referer="")

        self.assertEqual(headers["sec-fetch-site"], "none")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertNotIn("referer", headers)
        self.assertNotIn("cache-control", headers)


if __name__ == "__main__":
    unittest.main()
