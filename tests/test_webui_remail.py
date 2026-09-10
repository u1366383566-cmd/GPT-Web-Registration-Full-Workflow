# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class RemailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_remail_without_project_id(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "remail"
        ), patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 0, create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Remail 项目 ID", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_remail_config_does_not_check_local_pool(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "remail"
        ), patch.object(email_config, "REMAIL_API_BASE", "https://remail.aishop6.com", create=True), patch.object(
            email_config, "REMAIL_API_KEY", "rk-test-key", create=True
        ), patch.object(email_config, "REMAIL_PROJECT_ID", 1001, create=True), patch.object(
            email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)


if __name__ == "__main__":
    unittest.main()
