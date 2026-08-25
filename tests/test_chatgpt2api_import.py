# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

from core import chatgpt2api_client
from webui.app import create_app


class _Response:
    ok = True
    status_code = 200

    def json(self):
        return {"added": 1, "skipped": 0}


class Chatgpt2ApiImportTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            key: os.environ.get(key)
            for key in ("CHATGPT2API_BASE_URL", "CHATGPT2API_AUTH_KEY", "CHATGPT2API_IMPORT_TIMEOUT")
        }
        os.environ["CHATGPT2API_BASE_URL"] = "http://127.0.0.1:8021/"
        os.environ["CHATGPT2API_AUTH_KEY"] = "test-admin-key"
        os.environ["CHATGPT2API_IMPORT_TIMEOUT"] = "9"

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_build_payload_marks_regular_web_source(self):
        payload = chatgpt2api_client.build_account_payload({
            "access_token": "test-token", "email": "person@example.com", "plan_type": "free",
        })
        self.assertEqual("web", payload["source_type"])
        self.assertEqual("free", payload["type"])

    @patch("core.chatgpt2api_client.requests.post")
    def test_import_sends_account_items_to_target_pool(self, post):
        post.return_value = _Response()
        result = chatgpt2api_client.import_accounts([{
            "access_token": "test-token", "email": "person@example.com", "user_id": "user-test",
        }])
        self.assertEqual({"submitted": 1, "added": 1, "skipped": 0}, result)
        self.assertEqual("http://127.0.0.1:8021/api/accounts", post.call_args.args[0])
        self.assertEqual("Bearer test-admin-key", post.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual("web", post.call_args.kwargs["json"]["accounts"][0]["source_type"])

    def test_import_rejects_accounts_without_access_token(self):
        with self.assertRaisesRegex(chatgpt2api_client.ChatGPT2ApiImportError, "access_token"):
            chatgpt2api_client.import_accounts([{"email": "person@example.com"}])

    def test_import_requires_target_admin_key(self):
        os.environ.pop("CHATGPT2API_AUTH_KEY", None)
        with self.assertRaisesRegex(chatgpt2api_client.ChatGPT2ApiImportError, "管理员密钥"):
            chatgpt2api_client.import_accounts([{"access_token": "test-token"}])

    @patch("webui.app.chatgpt2api_client.import_accounts")
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.get_account")
    @patch("webui.app.db.get_job")
    def test_import_successful_job_uses_its_registered_account(self, get_job, get_account, get_by_email, import_accounts):
        get_job.return_value = {"id": 7, "status": "success", "account_id": 12, "email": "person@example.com"}
        get_account.return_value = {"id": 12, "email": "person@example.com", "access_token": "test-token", "archived": False}
        import_accounts.return_value = {"submitted": 1, "added": 1, "skipped": 0}
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        response = client.post("/api/jobs/import-chatgpt2api", json={"job_ids": [7]})

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.get_json()["added"])
        import_accounts.assert_called_once_with([get_account.return_value])
        get_by_email.assert_not_called()

    @patch("webui.app.chatgpt2api_client.import_accounts")
    @patch("webui.app.db.get_job", return_value={"id": 8, "status": "failed"})
    def test_import_job_rejects_non_success_task(self, get_job, import_accounts):
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        response = client.post("/api/jobs/import-chatgpt2api", json={"job_ids": [8]})

        self.assertEqual(400, response.status_code)
        self.assertIn("成功任务", response.get_json()["error"])
        import_accounts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
