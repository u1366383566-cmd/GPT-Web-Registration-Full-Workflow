# -*- coding: utf-8 -*-
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

from core import chatgpt2api_client
from webui.app import create_app


class Chatgpt2ApiExportTests(unittest.TestCase):
    def setUp(self):
        self.proxy = "http://registration-user:registration-password@proxy.example:10001"
        self.account = {
            "id": 1,
            "email": "person@example.com",
            "access_token": "test-token",
            "user_id": "user-test",
            "created_at": "2026-10-21T09:58:00",
            "current_plan_type": "free",
            "archived": False,
            "proxy_used": self.proxy,
            "proxy": self.proxy,
            "extra_json": json.dumps({"proxy_used": self.proxy}),
        }

    def assert_account_has_no_proxy(self, item):
        self.assertEqual("test-token", item["access_token"])
        self.assertEqual("person@example.com", item["email"])
        self.assertEqual("user-test", item["user_id"])
        self.assertEqual("free", item["type"])
        self.assertEqual("web", item["source_type"])
        self.assertNotIn("proxy", item)
        self.assertNotIn("proxy_used", item)
        serialized = json.dumps(item)
        self.assertNotIn("registration-user", serialized)
        self.assertNotIn("registration-password", serialized)

    def test_webui_export_omits_registration_proxy(self):
        # Skip startup recovery so this endpoint test cannot touch live storage.
        with patch(
            "webui.app.account_inspection_service.startup_recovery_guard",
            return_value=nullcontext(False),
        ):
            client = create_app(auth_code="test-auth").test_client()
        with patch("webui.app.db.get_account", return_value=self.account):
            response = client.post(
                "/api/accounts/export-chatgpt2api",
                json={"account_ids": [1, 1]},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertEqual(1, body["count"])
        self.assert_account_has_no_proxy(body["items"][0])
        self.assertEqual("2026-10-21 09:58:00", body["items"][0]["created_at"])
        self.assertEqual(self.proxy, self.account["proxy_used"])

    def test_cli_export_omits_registration_proxy(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "accounts.db"
            output = Path(td) / "accounts.json"
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute(
                    "CREATE TABLE accounts "
                    "(id INTEGER, email TEXT, status TEXT, archived INTEGER, payload TEXT)"
                )
                conn.execute(
                    "INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                    (1, self.account["email"], "registered", 0, json.dumps(self.account)),
                )
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [
                    sys.executable, str(root / "tools/export_chatgpt2api_accounts.py"),
                    "--db", str(database), "--output", str(output),
                ],
                cwd=root,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            items = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(1, len(items))
            self.assert_account_has_no_proxy(items[0])
            with closing(sqlite3.connect(database)) as conn:
                source = json.loads(conn.execute("SELECT payload FROM accounts").fetchone()[0])
            self.assertEqual(self.proxy, source["proxy_used"])

    def test_direct_import_omits_registration_proxy(self):
        payloads = chatgpt2api_client.build_payloads([self.account])
        self.assertEqual(1, len(payloads))
        self.assert_account_has_no_proxy(payloads[0])


if __name__ == "__main__":
    unittest.main()
