# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider


class RemailProviderTests(unittest.TestCase):
    def test_parse_sources_keeps_remail(self):
        self.assertEqual(
            email_provider.parse_email_sources("outlook,remail,gptmail,remail"),
            ["outlook", "remail", "gptmail"],
        )

    @patch("core.remail_client.pick_account")
    def test_acquire_email_uses_remail_client(self, pick_account):
        pick_account.return_value.email = "fresh@remail.test"
        with patch("core.email_provider.parse_email_sources", return_value=["remail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@remail.test")

    @patch("core.remail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="remail")
    def test_wait_for_otp_uses_remail_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@remail.test", after_ts=123.0), "654321")
        fetch_latest_otp.assert_called_once_with("fresh@remail.test", after_ts=123.0)

    @patch("core.remail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source")
    def test_wait_for_otp_prefers_saved_source(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.wait_for_otp(
                    "fresh@remail.test",
                    after_ts=123.0,
                    email_source="Remail",
                ),
                "654321",
            )
        resolve.assert_not_called()
        fetch_latest_otp.assert_called_once_with("fresh@remail.test", after_ts=123.0)

    @patch("core.remail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source")
    @patch("core.db.get_account_by_email", return_value={"email_source": "remail"})
    def test_wait_for_otp_uses_registered_source_without_explicit_argument(
        self,
        get_account_by_email,
        resolve,
        fetch_latest_otp,
    ):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.wait_for_otp("registered@outlook.test", after_ts=123.0),
                "654321",
            )
        resolve.assert_not_called()
        fetch_latest_otp.assert_called_once_with("registered@outlook.test", after_ts=123.0)

    @patch("core.email_provider._registered_email_source", return_value="cloudmail")
    @patch("core.gptmail_client.get_account_context", return_value=object())
    def test_resolve_email_source_prefers_registered_source_over_runtime_context(
        self,
        get_gptmail_context,
        registered_source,
    ):
        self.assertEqual(
            email_provider.resolve_email_source("registered@cloudmail.test"),
            "cloudmail",
        )
        registered_source.assert_called_once_with("registered@cloudmail.test")
        get_gptmail_context.assert_not_called()

    @patch("core.remail_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="remail")
    def test_release_email_uses_remail_client(self, resolve, release_account):
        self.assertEqual(email_provider.release_email("fresh@remail.test", status="failed"), "remail")
        release_account.assert_called_once_with("fresh@remail.test", status="failed", note=None)


if __name__ == "__main__":
    unittest.main()
