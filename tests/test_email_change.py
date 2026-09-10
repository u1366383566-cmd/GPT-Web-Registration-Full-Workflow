# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db
from core import email_change_service
from webui.app import create_app


class EmailChangeTests(unittest.TestCase):
    @staticmethod
    def storage(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json", "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json", "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db", "_CODEX_DIR": root / "codex",
            "_CODEX_AGENT_DIR": root / "agent", "_LEGACY_CODEX_EXPORT_STATE": root / "state.json",
            "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
        }

    def test_finish_keeps_original_and_switches_current_email(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            account_id = db.insert_account(email="old@example.com", access_token="old-token", email_source="outlook")
            self.assertTrue(db.claim_account_email_change(account_id, "imap"))
            self.assertTrue(db.mark_account_email_change_running(account_id, "new@example.com"))
            self.assertTrue(db.finish_account_email_change(
                account_id, ok=True, new_email="new@example.com", source="imap",
                material_line="new@example.com----imap-pass",
            ))
            row = db.get_account(account_id)
            self.assertEqual(row["original_email"], "old@example.com")
            self.assertEqual(row["email"], "new@example.com")
            self.assertEqual(row["email_source"], "imap")
            self.assertEqual(row["access_token"], "")
            self.assertEqual(row["email_change_status"], "success")

    def test_protocol_uses_captured_begin_and_verify_paths(self):
        session = MagicMock()
        session.device_id = "device"
        session.navigator_language.return_value = "en-US"
        session.get_chatgpt_headers.return_value = {}
        response = MagicMock(status_code=200)
        response.json.return_value = {"success": True}
        session.post.return_value = response
        email_change_service._post(session, "/backend-api/accounts/change_email/begin", "token", {"email": "new@example.com"})
        email_change_service._post(session, "/backend-api/accounts/change_email/verify", "token", {"email": "new@example.com", "code": "123456"})
        self.assertEqual(session.post.call_args_list[0].args[0], "https://chatgpt.com/backend-api/accounts/change_email/begin")
        self.assertEqual(session.post.call_args_list[1].args[0], "https://chatgpt.com/backend-api/accounts/change_email/verify")

    def test_success_triggers_live_check_with_new_email(self):
        queued = {"accepted": True, "status": "queued"}
        with patch("core.live_check_service.enqueue_account_live_check", return_value=queued) as enqueue:
            result = email_change_service._enqueue_live_check_after_change(12, "new@example.com")
        self.assertTrue(result["accepted"])
        enqueue.assert_called_once_with(
            account_id=12, email="new@example.com", trigger="email_change_auto", proxy=None,
        )

    def test_transport_error_switches_session_and_retries(self):
        first, second = MagicMock(), MagicMock()
        with patch.object(email_change_service, "_post", side_effect=[OSError("curl 35 reset"), {"success": True}]) as post, \
             patch.object(email_change_service, "_new_session", return_value=second) as new_session, \
             patch.object(email_change_service, "_append_log"), \
             patch.object(email_change_service.time, "sleep"):
            result, used_session = email_change_service._post_with_network_retry(
                first, account_id=9, path="/backend-api/accounts/change_email/begin",
                token="token", payload={"email": "new@example.com"},
            )
        self.assertTrue(result["success"])
        self.assertIs(used_session, second)
        self.assertEqual(post.call_count, 2)
        new_session.assert_called_once_with(9, None, email="")

    def test_recent_login_reuses_twofa_warmup_and_reauth_flow(self):
        session = MagicMock()
        with patch("core.account_liveness._warm_authenticated_session") as warm, \
             patch("core.account_liveness._login_via_reauth", return_value={
                 "accessToken": "fresh-token",
             }) as reauth, \
             patch.object(email_change_service, "_append_log"):
            token = email_change_service._refresh_recent_login(
                session,
                account_id=19,
                email="Old@Example.com",
                email_source="imap",
                access_token="stale-token",
            )
        self.assertEqual(token, "fresh-token")
        warm.assert_called_once_with(session, "stale-token")
        self.assertIs(reauth.call_args.args[0], session)
        self.assertEqual(reauth.call_args.args[1], "Old@Example.com")
        self.assertEqual(reauth.call_args.kwargs["email_source"], "imap")

    def test_post_change_live_check_reuses_current_session(self):
        session = MagicMock()
        session.proxy = "socks5://127.0.0.1:7897"
        session_info = {
            "accessToken": "new-token",
            "user": {"id": "user-1", "email": "new@example.com"},
            "account": {"planType": "free"},
        }
        with patch("core.chatgpt_auth.get_csrf_token", return_value="csrf") as csrf, \
             patch("core.chatgpt_auth.signin_openai", return_value="https://auth/authorize") as signin, \
             patch("core.openai_auth.follow_authorize") as follow, \
             patch("core.account_liveness._login_via_password_or_otp", return_value=session_info) as login, \
             patch("core.account_liveness._safe_fingerprint_for_account", return_value={"geo_country": "JP"}), \
             patch("core.account_liveness._safe_fingerprint_text_for_account", return_value="geo=JP"), \
             patch.object(email_change_service.db, "update_account_liveness", return_value=True) as update, \
             patch.object(email_change_service, "_append_log"):
            result = email_change_service._check_live_in_current_session(
                session,
                account_id=67,
                email="new@example.com",
                email_source="imap",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        csrf.assert_called_once_with(session)
        signin.assert_called_once_with(session, "csrf", "new@example.com")
        follow.assert_called_once_with(session, "https://auth/authorize")
        self.assertIs(login.call_args.args[0], session)
        self.assertEqual(login.call_args.kwargs["email_source"], "imap")
        update.assert_called_once_with(67, result)

    def test_begin_skips_reauth_when_existing_token_is_recent(self):
        session = MagicMock()
        with patch.object(
            email_change_service,
            "_post_with_network_retry",
            return_value=({"success": True}, session),
        ) as post, patch.object(email_change_service, "_refresh_recent_login") as refresh, \
             patch.object(email_change_service, "_append_log"):
            used_session, token, _, reauthenticated = (
                email_change_service._begin_change_with_optional_reauth(
                    session,
                    account_id=3,
                    current_email="old@example.com",
                    current_source="imap",
                    new_email="new@example.com",
                    access_token="recent-token",
                )
            )
        self.assertIs(used_session, session)
        self.assertEqual(token, "recent-token")
        self.assertFalse(reauthenticated)
        self.assertEqual(post.call_count, 1)
        refresh.assert_not_called()

    def test_begin_reauths_only_after_server_requires_it(self):
        session = MagicMock()
        error = RuntimeError(
            "/backend-api/accounts/change_email/begin 返回 401: "
            "{'code': 'reauth_required', 'message': 'Recent login required'}"
        )
        with patch.object(
            email_change_service,
            "_post_with_network_retry",
            side_effect=[error, ({"success": True}, session)],
        ) as post, patch.object(
            email_change_service, "_refresh_recent_login", return_value="fresh-token",
        ) as refresh, patch.object(email_change_service, "_append_log"):
            _, token, _, reauthenticated = (
                email_change_service._begin_change_with_optional_reauth(
                    session,
                    account_id=4,
                    current_email="old@example.com",
                    current_source="imap",
                    new_email="new@example.com",
                    access_token="old-token",
                )
            )
        self.assertEqual(token, "fresh-token")
        self.assertTrue(reauthenticated)
        self.assertEqual(post.call_count, 2)
        refresh.assert_called_once_with(
            session,
            account_id=4,
            email="old@example.com",
            email_source="imap",
            access_token="old-token",
        )

    def test_bulk_api_accepts_selected_source(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            account_id = db.insert_account(email="old@example.com", access_token="token", email_source="outlook")
            client = create_app(auth_code="test-auth").test_client()
            with patch("core.email_change_service.enqueue", return_value={"accepted": True, "future": MagicMock()}):
                response = client.post("/api/accounts/change-email-bulk", json={"account_ids": [account_id], "source": "imap"}, headers={"X-Auth-Code": "test-auth"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["started_count"], 1)

    def test_change_email_log_api(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            account_id = db.insert_account(email="old@example.com", access_token="token", email_source="outlook")
            log_file = Path(td) / "change.log"
            log_file.write_text("12:00:00 [INFO] 换绑任务已入队\n", encoding="utf-8")
            client = create_app(auth_code="test-auth").test_client()
            with patch("core.email_change_service.log_path", return_value=log_file), \
                 patch("core.email_change_service.is_running", return_value=False):
                response = client.get(
                    f"/api/accounts/{account_id}/change-email-log",
                    headers={"X-Auth-Code": "test-auth"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn("换绑任务已入队", response.get_json()["log"])


if __name__ == "__main__":
    unittest.main()
