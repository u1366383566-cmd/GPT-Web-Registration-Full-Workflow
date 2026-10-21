import copy
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from core import db
from core.chatgpt_plan import parse_accounts_check
from webui.app import _compact_account_for_list


class PlanPromoDetailsTests(unittest.TestCase):
    def parse(self, campaigns, plan="free"):
        return parse_accounts_check({"accounts": {"default": {
            "account": {"plan_type": plan},
            "eligible_promo_campaigns": campaigns,
        }}})

    def test_all_campaigns_preserved_without_changing_plus_logic(self):
        campaigns = {
            "plus": {"id": "plus-1-month-free", "metadata": {
                "discount": {"percentage": 100},
                "duration": {"num_periods": 1, "period": "month"},
                "no_auto_renewal_at_discount_end": False,
            }},
            "go": {"id": "go-discount", "metadata": {"custom_field": "保留未知字段"}},
        }
        result = self.parse(campaigns)
        self.assertTrue(result["plus_trial_eligible"])
        self.assertEqual(result["eligible_promo_campaigns"], campaigns)
        self.assertEqual(result["plus_trial_discount_percentage"], 100)
        other = self.parse({"go": campaigns["go"]})
        self.assertFalse(other["plus_trial_eligible"])
        self.assertIn("go", other["eligible_promo_campaigns"])
        self.assertEqual(self.parse(campaigns, "plus")["eligible_promo_campaigns"], {})
        self.assertEqual(self.parse({})["eligible_promo_campaigns"], {})

    def test_persistence_list_polling_and_failure_preservation(self):
        rows = [{"id": 1, "email": "test@example.test"}]
        result = self.parse({"go": {"id": "go-offer"}})
        @contextmanager
        def account_for_update(*args, **kwargs):
            yield rows[0]
        with patch.object(db, "_account_for_update", side_effect=account_for_update):
            db.update_account_plan_check(acc_id=1, result=result)
            self.assertEqual(rows[0]["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
            self.assertEqual(_compact_account_for_list(rows[0])["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
            with patch.object(db, "_query_collection_page", side_effect=lambda *a, **kw: (copy.deepcopy(rows), 1, "")):
                snapshot = db.list_account_plan_check_statuses()
                self.assertEqual(snapshot["items"][0]["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
                rows[0]["eligible_promo_campaigns"] = {"go": {"id": "changed"}}
                self.assertNotEqual(snapshot["revision"], db.list_account_plan_check_statuses()["revision"])
            db.update_account_plan_check(acc_id=1, result={"ok": False, "error": "timeout"})
            self.assertEqual(rows[0]["eligible_promo_campaigns"], {"go": {"id": "changed"}})
            db.update_account_plan_check(acc_id=1, result=self.parse({}))
            self.assertEqual(rows[0]["eligible_promo_campaigns"], {})


if __name__ == "__main__":
    unittest.main()
