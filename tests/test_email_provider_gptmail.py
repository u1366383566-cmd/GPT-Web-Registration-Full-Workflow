# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider


class GPTMailProviderTests(unittest.TestCase):
    def test_parse_sources_keeps_gptmail_in_order(self):
        self.assertEqual(
            email_provider.parse_email_sources("outlook,gptmail,generic_api,mailnest,cloudmail"),
            ["outlook", "gptmail", "generic_api", "mailnest", "cloudmail"],
        )

    @patch("core.gptmail_client.pick_account")
    def test_acquire_email_uses_gptmail_client(self, pick_account):
        pick_account.return_value.email = "fresh@gptmail.test"

        with patch("core.email_provider.parse_email_sources", return_value=["gptmail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@gptmail.test")

    @patch("core.gptmail_client.get_account_context", return_value=object())
    def test_resolve_email_source_recognizes_cached_gptmail_address(self, get_context):
        self.assertEqual(email_provider.resolve_email_source("fresh@gptmail.test"), "gptmail")
        get_context.assert_called_once_with("fresh@gptmail.test")

    @patch("core.gptmail_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="gptmail")
    def test_release_email_clears_gptmail_context(self, resolve, release):
        self.assertEqual(email_provider.release_email("fresh@gptmail.test", status="failed"), "gptmail")
        release.assert_called_once_with("fresh@gptmail.test", status="failed", note=None)

    @patch("core.gptmail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="gptmail")
    def test_wait_for_otp_uses_gptmail_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@gptmail.test", after_ts=123.0), "654321")
        fetch_latest_otp.assert_called_once_with("fresh@gptmail.test", after_ts=123.0)

    @patch("core.mailnest_client.pick_account")
    def test_acquire_email_uses_mailnest_client(self, pick_account):
        pick_account.return_value.email = "fresh@mailnest.test"

        with patch("core.email_provider.parse_email_sources", return_value=["mailnest"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@mailnest.test")

    @patch("core.mailnest_client.fetch_latest_otp", return_value="112233")
    @patch("core.email_provider.resolve_email_source", return_value="mailnest")
    def test_wait_for_otp_uses_mailnest_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@mailnest.test", after_ts=123.0), "112233")
        fetch_latest_otp.assert_called_once_with("fresh@mailnest.test", after_ts=123.0)

    @patch("core.cloudmail_client.pick_account")
    def test_acquire_email_uses_cloudmail_client(self, pick_account):
        pick_account.return_value.email = "fresh@cloudmail.test"

        with patch("core.email_provider.parse_email_sources", return_value=["cloudmail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@cloudmail.test")

    @patch("core.cloudmail_client.fetch_latest_otp", return_value="445566")
    @patch("core.email_provider.resolve_email_source", return_value="cloudmail")
    def test_wait_for_otp_uses_cloudmail_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@cloudmail.test", after_ts=123.0), "445566")
        fetch_latest_otp.assert_called_once_with("fresh@cloudmail.test", after_ts=123.0)

    @patch("core.mailcx_client.pick_account")
    def test_acquire_email_uses_mailcx_client(self, pick_account):
        pick_account.return_value.email = "fresh@mailcx.test"
        with patch("core.email_provider.parse_email_sources", return_value=["mailcx"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@mailcx.test")

    @patch("core.mailcx_client.fetch_latest_otp", return_value="778899")
    @patch("core.email_provider.resolve_email_source", return_value="mailcx")
    def test_wait_for_otp_uses_mailcx_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@mailcx.test", after_ts=123.0), "778899")
        fetch_latest_otp.assert_called_once_with("fresh@mailcx.test", after_ts=123.0)
