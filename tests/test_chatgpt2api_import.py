# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

from core import chatgpt2api_client
from webui.app import create_app


class _JsonResponse:
    def __init__(self, payload=None, *, ok=True):
        self.payload = payload or {}
        self.ok = ok
        self.status_code = 200 if ok else 502

    def json(self):
        return self.payload


class Chatgpt2ApiImportTests(unittest.TestCase):
    def setUp(self):
        keys = (
            "CHATGPT2API_BASE_URL",
            "CHATGPT2API_AUTH_KEY",
            "CHATGPT2API_IMPORT_TIMEOUT",
            "CHATGPT2API_NEW_BASE_URL",
            "CHATGPT2API_NEW_AUTH_KEY",
            "CHATGPT2API_NEW_IMPORT_TIMEOUT",
            "CHATGPT2API_IMPORT_TARGET",
        )
        self.previous = {key: os.environ.get(key) for key in keys}
        os.environ["CHATGPT2API_BASE_URL"] = "http://127.0.0.1:8021/"
        os.environ["CHATGPT2API_AUTH_KEY"] = "legacy-key"
        os.environ["CHATGPT2API_IMPORT_TIMEOUT"] = "9"
        os.environ["CHATGPT2API_NEW_BASE_URL"] = "http://127.0.0.1:18765/"
        os.environ["CHATGPT2API_NEW_AUTH_KEY"] = "new-key"
        os.environ["CHATGPT2API_NEW_IMPORT_TIMEOUT"] = "11"
        os.environ["CHATGPT2API_IMPORT_TARGET"] = "legacy"

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def account():
        return {
            "access_token": "test-token",
            "email": "person@example.com",
            "user_id": "user-test",
        }

    def test_build_payload_marks_regular_web_source(self):
        payload = chatgpt2api_client.build_account_payload({
            **self.account(), "plan_type": "free",
        })
        self.assertEqual("web", payload["source_type"])
        self.assertEqual("free", payload["type"])

    @patch("core.chatgpt2api_client.requests.post")
    def test_legacy_import_sends_top_level_counts(self, post):
        post.return_value = _JsonResponse({"added": 2, "skipped": 1})
        result = chatgpt2api_client.import_accounts([self.account()])
        self.assertEqual({
            "submitted": 1,
            "added": 2,
            "skipped": 1,
            "errors": 0,
            "targets": [{
                "target": "legacy", "label": "旧版",
                "submitted": 1, "added": 2, "skipped": 1, "errors": 0,
            }],
            "target_errors": [],
            "partial_success": False,
        }, result)
        self.assertEqual("http://127.0.0.1:8021/api/accounts", post.call_args.args[0])
        self.assertEqual("Bearer legacy-key", post.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual("web", post.call_args.kwargs["json"]["accounts"][0]["source_type"])

    @patch("core.chatgpt2api_client.requests.post")
    def test_new_import_reads_nested_counts(self, post):
        post.return_value = _JsonResponse({
            "result": {"added": 4, "skipped": 2, "errors": [{"id": "x"}]},
        })
        result = chatgpt2api_client.import_accounts([self.account()], target="new")
        self.assertEqual(4, result["added"])
        self.assertEqual(2, result["skipped"])
        self.assertEqual(1, result["errors"])
        self.assertFalse(result["partial_success"])
        self.assertEqual("http://127.0.0.1:18765/api/accounts", post.call_args.args[0])
        self.assertEqual("Bearer new-key", post.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual(11, post.call_args.kwargs["timeout"])

    @patch("core.chatgpt2api_client.requests.post")
    def test_both_posts_separately_and_aggregates(self, post):
        post.side_effect = [
            _JsonResponse({"added": 1, "skipped": 1}),
            _JsonResponse({"result": {"added": 4, "skipped": 2}}),
        ]
        result = chatgpt2api_client.import_accounts([self.account()], target="both")
        self.assertEqual(2, result["submitted"])
        self.assertEqual(5, result["added"])
        self.assertEqual(3, result["skipped"])
        self.assertEqual(["legacy", "new"], [item["target"] for item in result["targets"]])
        self.assertEqual(
            ["http://127.0.0.1:8021/api/accounts", "http://127.0.0.1:18765/api/accounts"],
            [call.args[0] for call in post.call_args_list],
        )
        self.assertFalse(result["partial_success"])

    @patch("core.chatgpt2api_client.requests.post")
    def test_both_reports_partial_failure(self, post):
        post.side_effect = [
            _JsonResponse({"added": 1, "skipped": 0}),
            chatgpt2api_client.requests.RequestException("boom"),
        ]
        result = chatgpt2api_client.import_accounts([self.account()], target="both")
        self.assertTrue(result["partial_success"])
        self.assertEqual(1, result["added"])
        self.assertEqual(1, len(result["target_errors"]))
        self.assertIn("新版", result["target_errors"][0]["error"])

    def test_rejects_unknown_target_before_posting(self):
        with patch("core.chatgpt2api_client.requests.post") as post:
            with self.assertRaisesRegex(chatgpt2api_client.ChatGPT2ApiImportError, "导入目标"):
                chatgpt2api_client.import_accounts([self.account()], target="v9")
        post.assert_not_called()

    def test_import_requires_each_target_admin_key(self):
        os.environ.pop("CHATGPT2API_AUTH_KEY", None)
        with self.assertRaisesRegex(chatgpt2api_client.ChatGPT2ApiImportError, "管理员密钥"):
            chatgpt2api_client.import_accounts([{"access_token": "test-token"}])

        os.environ["CHATGPT2API_AUTH_KEY"] = "legacy-key"
        os.environ.pop("CHATGPT2API_NEW_AUTH_KEY", None)
        with self.assertRaisesRegex(chatgpt2api_client.ChatGPT2ApiImportError, "新版"):
            chatgpt2api_client.import_accounts([{"access_token": "test-token"}], target="new")

    @patch("webui.app.chatgpt2api_client.probe_targets")
    def test_probe_endpoint_uses_selected_target(self, probe_targets):
        probe_targets.return_value = [{
            "target": "new", "label": "新版", "ok": True, "version": "3.2.2", "error": "",
        }]
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.post("/api/chatgpt2api/probe", json={"target": "new"})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["ok"])
        probe_targets.assert_called_once_with("new")

    @patch("webui.app.chatgpt2api_client.import_accounts")
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.get_account")
    @patch("webui.app.db.get_job")
    def test_import_successful_job_passes_request_target(self, get_job, get_account, get_by_email, import_accounts):
        get_job.return_value = {"id": 7, "status": "success", "account_id": 12, "email": "person@example.com"}
        get_account.return_value = {"id": 12, "email": "person@example.com", "access_token": "test-token", "archived": False}
        import_accounts.return_value = {
            "submitted": 1, "added": 1, "skipped": 0, "errors": 0,
            "targets": [], "target_errors": [], "partial_success": False,
        }
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        response = client.post("/api/jobs/import-chatgpt2api", json={"job_ids": [7], "target": "both"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.get_json()["added"])
        import_accounts.assert_called_once_with([get_account.return_value], target="both")
        get_by_email.assert_not_called()

    @patch("webui.app.chatgpt2api_client.import_accounts")
    def test_import_job_rejects_invalid_request_target(self, import_accounts):
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.post("/api/jobs/import-chatgpt2api", json={"job_ids": [7], "target": "beta"})
        self.assertEqual(400, response.status_code)
        import_accounts.assert_not_called()

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
