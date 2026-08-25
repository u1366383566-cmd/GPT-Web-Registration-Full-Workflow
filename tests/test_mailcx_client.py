# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import mailcx_client
from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


class MailCXClientTests(unittest.TestCase):
    def setUp(self):
        mailcx_client._CONTEXT_CACHE.clear()
        mailcx_client._DOMAIN_CACHE = None

    @patch("core.mailcx_client.requests.get")
    @patch("core.mailcx_client.secrets.choice", side_effect=list("bcdefghijkl"))
    @patch("core.mailcx_client.random.choice", return_value="a")
    def test_pick_account_uses_default_system_domain(self, _random, _choice, get):
        response = Mock(status_code=200)
        response.json.return_value = {"system_domains": [{"domain": "one.test", "default": False}, {"domain": "two.test", "default": True}]}
        get.return_value = response
        with patch.object(mailcx_client._email_cfg, "MAILCX_API_TOKEN", "token-123", create=True), patch.object(mailcx_client._email_cfg, "MAILCX_DOMAIN", "", create=True), patch.object(mailcx_client._email_cfg, "MAILCX_RANDOM_LOCAL_LENGTH", 12, create=True):
            account = mailcx_client.pick_account()
        self.assertEqual(account.email, "abcdefghijkl@two.test")
        self.assertIs(mailcx_client.get_account_context(account.email), account)
        self.assertEqual(get.call_args.kwargs["headers"]["x-api-token"], "token-123")

    def test_pick_account_requires_api_token(self):
        with patch.object(mailcx_client._email_cfg, "MAILCX_API_TOKEN", "", create=True):
            with self.assertRaisesRegex(mailcx_client.MailCXError, "API Token 未配置"):
                mailcx_client.pick_account()

    def test_webui_marks_mailcx_token_as_secret_env_field(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "MAILCX_API_TOKEN")
        self.assertEqual(SECRET_ENV_KEYS["MAILCX_API_TOKEN"], "Mail.cx API Token")
        self.assertEqual(field["group"], "邮箱 / OTP")
        self.assertTrue(field["secret"])
        self.assertEqual(field["storage"], "env")

    @patch("core.mailcx_client.requests.get")
    def test_fetch_latest_otp_reads_detail_from_long_poll_result(self, get):
        inbox = Mock(status_code=200)
        inbox.json.return_value = {"emails": [{"id": "mail-1", "from_email": "noreply@openai.com", "subject": "Your verification code", "created_at": "2026-08-25T12:00:00Z"}], "next_since": "cursor-1"}
        detail = Mock(status_code=200)
        detail.json.return_value = {"id": "mail-1", "from_email": "noreply@openai.com", "subject": "Your verification code", "text": "Your code is 654321", "created_at": "2026-08-25T12:00:00Z"}
        get.side_effect = [inbox, detail]
        with patch.object(mailcx_client._email_cfg, "MAILCX_API_TOKEN", "token-123", create=True):
            code = mailcx_client.fetch_latest_otp("new@two.test", after_ts=0, max_wait=1)
        self.assertEqual(code, "654321")
        self.assertIn("/inbox/new@two.test", get.call_args_list[0].args[0])
        self.assertIn("/email/mail-1", get.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
