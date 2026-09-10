# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class WebUiEmailPoolDeleteTests(unittest.TestCase):
    @staticmethod
    def _storage_patches(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex_accounts",
            "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
        }

    def test_single_delete_all_does_not_fall_back_to_outlook(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "generic@example.com", "code_url": "https://mail.example/code"},
                ])
                app = create_app(auth_code="test-auth")
                client = app.test_client()

                response = client.post(
                    "/api/outlook/delete",
                    json={"email": "generic@example.com", "source": "all"},
                    headers={"X-Auth-Code": "test-auth"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {"ok": True, "deleted": True})
                self.assertEqual(db.list_email_pool_page(source="all", limit=10)["total"], 0)

    def test_bulk_delete_uses_each_item_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "generic@example.com", "code_url": "https://mail.example/code"},
                ])
                db.import_outlook_accounts([
                    {
                        "email": "outlook@example.com",
                        "password": "password",
                        "client_id": "client",
                        "refresh_token": "refresh",
                    },
                ])
                db.claim_next_domain_email("domain@example.com")
                app = create_app(auth_code="test-auth")
                client = app.test_client()

                response = client.post(
                    "/api/outlook/delete-bulk",
                    json={
                        "source": "all",
                        "items": [
                            {"email": "generic@example.com", "source": "generic_api"},
                            {"email": "outlook@example.com", "source": "outlook"},
                            {"email": "domain@example.com", "source": "cloudflare_domain"},
                        ],
                    },
                    headers={"X-Auth-Code": "test-auth"},
                )

                body = response.get_json()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(body["deleted_count"], 3)
                self.assertEqual(body["skipped"], [])
                self.assertEqual(db.list_email_pool_page(source="all", limit=10)["total"], 0)

    def test_delete_reports_not_found_instead_of_fake_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                app = create_app(auth_code="test-auth")
                client = app.test_client()

                response = client.post(
                    "/api/outlook/delete",
                    json={"email": "missing@example.com", "source": "generic_api"},
                    headers={"X-Auth-Code": "test-auth"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {"ok": True, "deleted": False})


if __name__ == "__main__":
    unittest.main()
