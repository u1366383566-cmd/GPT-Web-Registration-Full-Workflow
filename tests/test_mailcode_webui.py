import unittest
from unittest.mock import patch

from config import email as cfg
from webui.app import create_app
from webui.config_editor import EDITABLE_FIELDS
from core.mailcode_client import MailCodeError


class MailCodeWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_config_schema_hides_base_and_links_official_site(self):
        fields = {f["key"]: f for f in EDITABLE_FIELDS}
        self.assertNotIn("MAILCODE_API_BASE", fields)
        self.assertEqual(fields["MAILCODE_API_KEY"]["external_url"], "https://mailcode.bzb.qzz.io")
        self.assertEqual(fields["MAILCODE_API_KEY"]["external_label"], "打开官网")
        self.assertEqual(fields["MAILCODE_API_KEY"]["storage"], "env")
        self.assertTrue(fields["MAILCODE_API_KEY"]["secret"])
        self.assertIn("mailcode", fields["EMAIL_SOURCE"]["help"])

    @patch("core.mailcode_client.get_wallet", return_value={"balance": "1000"})
    @patch("core.mailcode_client.list_products", return_value=[{"id": "product-1", "price": "100", "stock": 20}])
    @patch("core.mailcode_client.pick_account")
    def test_catalog_does_not_purchase(self, pick, products, wallet):
        result = self.client.get("/api/mailcode/catalog")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["products"][0]["id"], "product-1")
        self.assertEqual(result.json["wallet"]["balance"], "1000")
        pick.assert_not_called()

    @patch("core.mailcode_client.list_products", side_effect=MailCodeError("密钥不可用", status=401))
    def test_catalog_reports_api_error(self, products):
        result = self.client.get("/api/mailcode/catalog")
        self.assertEqual(result.status_code, 400)
        self.assertIn("密钥不可用", result.json["error"])

    def test_job_requires_mailcode_key_before_submitting(self):
        with patch.object(cfg, "USE_EMAIL_SERVICE", True), patch.object(cfg, "EMAIL_SOURCE", "mailcode"), patch.object(cfg, "MAILCODE_API_KEY", ""):
            result = self.client.post("/api/jobs", json={"count": 1})
        self.assertEqual(result.status_code, 400)
        self.assertIn("MailCode API Key", result.json["error"])

    @patch("core.registration_service.submit_registration", return_value=[{"id": 1}])
    def test_jobs_accept_dynamic_mailcode_provider(self, submit):
        with patch.object(cfg, "USE_EMAIL_SERVICE", True), patch.object(cfg, "EMAIL_SOURCE", "mailcode"), patch.object(cfg, "MAILCODE_API_KEY", "test-key"), patch.object(cfg, "MAILCODE_PRODUCT_ID", "product-1"):
            result = self.client.post("/api/jobs", json={"count": 1})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["warning"], "")
        submit.assert_called_once()
