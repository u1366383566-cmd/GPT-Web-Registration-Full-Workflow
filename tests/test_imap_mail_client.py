# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db, email_provider
from core import imap_mail_client as imap_client
from webui.app import create_app


class ImapMailClientTests(unittest.TestCase):
    @staticmethod
    def _storage_patches(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json", "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json", "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db", "_CODEX_DIR": root / "codex",
            "_CODEX_AGENT_DIR": root / "codex-agent", "_LEGACY_CODEX_EXPORT_STATE": root / "state.json",
            "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
        }

    def test_pool_import_claim_release_and_source_resolution(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage_patches(Path(td))):
            record = {"email": "user@example.com", "imap_password": "secret", "imap_server": "imap.example.com",
                      "imap_port": 993, "imap_username": "", "imap_ssl": True}
            self.assertEqual(db.import_imap_emails([record]), (1, 0))
            row = db.list_email_pool_page(source="imap", limit=10)["items"][0]
            self.assertEqual(row["source"], "imap")
            self.assertEqual(row["copy_line"], "user@example.com----secret")
            claimed = db.claim_next_imap_email()
            self.assertEqual(claimed["status"], "used")
            self.assertEqual(email_provider.resolve_email_source("user@example.com"), "imap")
            self.assertTrue(db.release_unconsumed_imap_email("user@example.com"))
            self.assertEqual(db.imap_email_pool_summary()["available"], 1)

    def test_connect_uses_email_as_default_username_and_supports_plain_imap(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com", 143, use_ssl=False)
        mail = MagicMock()
        mail.select.return_value = ("OK", [])
        with patch.object(imap_client.imaplib, "IMAP4", return_value=mail) as constructor:
            self.assertIs(imap_client._connect(account), mail)
        constructor.assert_called_once_with("imap.example.com", 143)
        mail.login.assert_called_once_with("user@example.com", "pw")
        mail.select.assert_called_once_with("INBOX")

    def test_webui_can_import_imap_material(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage_patches(Path(td))):
            client = create_app(auth_code="test-auth").test_client()
            response = client.post(
                "/api/outlook/import",
                json={"source": "imap", "text": "user@example.com----pw",
                      "imap_server": "imap.example.com", "imap_port": 993, "imap_ssl": True},
                headers={"X-Auth-Code": "test-auth"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["inserted"], 1)
            row = db.get_imap_email_by_email("user@example.com")
            self.assertEqual(row["imap_username"], "")
            self.assertTrue(row["imap_ssl"])

    def test_webui_accepts_colon_imap_material(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage_patches(Path(td))):
            client = create_app(auth_code="test-auth").test_client()
            response = client.post(
                "/api/outlook/import",
                json={"source": "imap", "text": "user@example.com:pw",
                      "imap_server": "imap.example.com", "imap_port": 143, "imap_ssl": False},
                headers={"X-Auth-Code": "test-auth"},
            )
            self.assertEqual(response.status_code, 200)
            row = db.get_imap_email_by_email("user@example.com")
            self.assertEqual(row["copy_line"], "user@example.com----pw")
            self.assertEqual(row["imap_port"], 143)
            self.assertFalse(row["imap_ssl"])

    def test_fetch_latest_otp_filters_recipient_and_old_mail(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com")
        now = 2_000_000_000.0
        messages = [
            {"to": "other@example.com", "from": "OpenAI <noreply@openai.com>", "subject": "Your code is 111111",
             "text": "Your verification code is 111111", "date": "2033-05-18T03:33:20Z"},
            {"to": "user@example.com", "from": "OpenAI <noreply@openai.com>", "subject": "Your ChatGPT code is 654321",
             "text": "Your verification code is 654321", "date": "2033-05-18T03:33:20Z"},
        ]
        fake_mail = MagicMock()
        with patch.object(imap_client, "get_account_context", return_value=account), \
             patch.object(imap_client, "_connect", return_value=fake_mail), \
             patch.object(imap_client, "_search_messages", return_value=messages), \
             patch.object(imap_client.time, "time", return_value=now):
            self.assertEqual(imap_client.fetch_latest_otp("user@example.com", after_ts=now - 10, max_wait=3,
                                                          poll_interval=1, settle_seconds=0), "654321")


if __name__ == "__main__":
    unittest.main()
