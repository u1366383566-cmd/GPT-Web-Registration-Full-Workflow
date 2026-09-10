# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountPlanFilterTests(unittest.TestCase):
    def test_plus_trial_and_no_trial_filters_use_sql(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text(json.dumps([
                {"id": 1, "email": "eligible@example.com", "current_plan_type": "free", "plus_trial_eligible": True},
                {"id": 2, "email": "not-eligible@example.com", "current_plan_type": "free", "plus_trial_eligible": False},
                {"id": 3, "email": "paid@example.com", "current_plan_type": "plus", "plus_trial_eligible": True},
                {"id": 4, "email": "missing-plan@example.com", "plus_trial_eligible": True},
                {"id": 5, "email": "unknown-eligibility@example.com", "current_plan_type": "free"},
            ], ensure_ascii=False), encoding="utf-8")

            missing = root / "missing.json"
            with patch.multiple(
                db,
                _ACCOUNTS_JSON=accounts_path,
                _LEGACY_ACCOUNTS_JSON=missing,
                _OUTLOOK_JSON=missing,
                _GENERIC_API_EMAIL_JSON=missing,
                _JOBS_JSON=missing,
                _DOMAIN_EMAIL_JSON=missing,
            ), patch.object(db, "_SQLITE_READY", False), patch.object(db, "_SQLITE_READY_PATH", None):
                for filter_name in ("plus_trial", "plus_trial_eligible", "trial"):
                    result = db.list_accounts_page(limit=20, plan_filter=filter_name)
                    self.assertEqual([item["id"] for item in result["items"]], [1])

                free_result = db.list_accounts_page(limit=20, plan_filter="free")
                self.assertEqual([item["id"] for item in free_result["items"]], [5, 2, 1])

                no_trial_result = db.list_accounts_page(limit=20, plan_filter="free_no_trial")
                self.assertEqual([item["id"] for item in no_trial_result["items"]], [2])

                snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="plus_trial")
                self.assertEqual([item["id"] for item in snapshot["items"]], [1])

                free_snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="free")
                self.assertEqual([item["id"] for item in free_snapshot["items"]], [5, 2, 1])

                no_trial_snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="free_no_trial")
                self.assertEqual([item["id"] for item in no_trial_snapshot["items"]], [2])


if __name__ == "__main__":
    unittest.main()
