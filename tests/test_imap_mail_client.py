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
        constructor.assert_called_once_with("imap.example.com", 143, timeout=10.0)
        mail.login.assert_called_once_with("user@example.com", "pw")
        mail.select.assert_called_once_with("INBOX", readonly=True)

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

    def test_failed_login_closes_socket(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com")
        mail = MagicMock()
        mail.login.side_effect = imap_client.imaplib.IMAP4.error("bad credentials")
        with patch.object(imap_client.imaplib, "IMAP4_SSL", return_value=mail):
            with self.assertRaisesRegex(imap_client.ImapMailError, "登录失败"):
                imap_client._connect(account)
        mail.shutdown.assert_called_once()

    def test_uid_batch_fetch_does_not_mark_seen(self):
        from datetime import datetime, timezone
        mail = MagicMock()
        raw = b"From: noreply@openai.com\r\nTo: user@example.com\r\nSubject: Your code is 654321\r\n\r\n654321"
        mail.uid.side_effect = [("OK", [b"1 2"]), ("OK", [(b"2 (BODY[]", raw), b")"])]
        result = imap_client._search_messages(mail, datetime(2026, 10, 13, tzinfo=timezone.utc))
        self.assertEqual(result[0]["subject"], "Your code is 654321")
        self.assertEqual(mail.uid.call_args_list[0].args, ("search", None, "(SINCE 13-Oct-2026)"))
        self.assertEqual(mail.uid.call_args_list[1].args, ("fetch", b"1,2", "(BODY.PEEK[])"))

    def test_zero_timestamp_accepts_old_mail(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com")
        mail = MagicMock()
        message = {"to": account.email, "from": "noreply@openai.com", "subject": "Code 654321",
                   "date": "2020-01-01T00:00:00Z"}
        with patch.object(imap_client, "get_account_context", return_value=account), \
             patch.object(imap_client, "_connect", return_value=mail), \
             patch.object(imap_client, "_search_messages", return_value=[message]) as search:
            self.assertEqual(imap_client.fetch_latest_otp(account.email, after_ts=0, max_wait=5,
                                                          settle_seconds=0), "654321")
        self.assertIsNone(search.call_args.args[1])
        mail.shutdown.assert_called_once()

    def test_polling_reuses_connection_and_reconnects_after_socket_failure(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com")
        first, second = MagicMock(), MagicMock()
        clock = [0.0]
        message = {"to": account.email, "from": "noreply@openai.com", "subject": "Code 654321"}
        with patch.object(imap_client, "get_account_context", return_value=account), \
             patch.object(imap_client, "_connect", side_effect=[first, second]) as connect, \
             patch.object(imap_client, "_search_messages", side_effect=[[], OSError("reset"), [message]]) as search, \
             patch.object(imap_client.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(imap_client.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)):
            self.assertEqual(imap_client.fetch_latest_otp(account.email, after_ts=0, max_wait=10,
                                                          poll_interval=1, settle_seconds=0), "654321")
        self.assertEqual(connect.call_count, 2)
        self.assertEqual([c.args[0] for c in search.call_args_list], [first, first, second])
        first.shutdown.assert_called_once()
        second.shutdown.assert_called_once()

    def test_deadline_caps_socket_timeout_and_retry_sleep(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com")
        clock = [100.0]
        with patch.object(imap_client.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(imap_client._operation_timeout(102.0), 2.0)
            with self.assertRaises(TimeoutError):
                imap_client._operation_timeout(100.0)
        with patch.object(imap_client, "get_account_context", return_value=account), \
             patch.object(imap_client, "_connect", side_effect=imap_client.ImapMailError("连接失败")), \
             patch.object(imap_client.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(imap_client.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)):
            with self.assertRaisesRegex(imap_client.ImapMailError, "最后取件错误.*连接失败"):
                imap_client.fetch_latest_otp(account.email, max_wait=2, poll_interval=10)
        self.assertEqual(clock[0], 102.0)

    def test_settle_picks_newer_code_without_reconnecting(self):
        account = imap_client.ImapEmailAccount("user@example.com", "pw", "imap.example.com")
        mail = MagicMock()
        clock = [0.0]
        older = {"to": account.email, "from": "noreply@openai.com", "subject": "Code 111111",
                 "date": "2026-10-13T10:44:00Z"}
        newer = dict(older, subject="Code 222222", date="2026-10-13T10:45:00Z")
        with patch.object(imap_client, "get_account_context", return_value=account), \
             patch.object(imap_client, "_connect", return_value=mail) as connect, \
             patch.object(imap_client, "_search_messages", side_effect=[[older], [newer], [newer]]), \
             patch.object(imap_client.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(imap_client.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)):
            self.assertEqual(imap_client.fetch_latest_otp(account.email, after_ts=0, max_wait=10,
                                                          poll_interval=1, settle_seconds=2), "222222")
        connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
