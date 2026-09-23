# -*- coding: utf-8 -*-
import json
import unittest
from unittest import mock

import core.db as db


class AccountFullExportTests(unittest.TestCase):
    def _row(self, **overrides) -> dict:
        row = {
            "email": "a@b.com",
            "email_source": "gptmail",
            "extra_json": json.dumps({"registration_password": "Pass123"}),
            "totp_secret": "ABCDEF123",
        }
        row.update(overrides)
        return row

    def test_basic_format(self):
        line = db._account_full_export_line(self._row())
        self.assertEqual(
            line,
            "a@b.com---gptmail---Pass123---https://2fa.run/----2FA:ABCDEF123",
        )

    def test_separators_match_spec(self):
        # 前四段为 “---”，2FA 段前为 “----”
        line = db._account_full_export_line(self._row())
        self.assertTrue(line.startswith("a@b.com---gptmail---Pass123---https://2fa.run/----2FA:ABCDEF123"))
        self.assertIn("---https://2fa.run/----2FA:", line)

    def test_missing_registration_password_keeps_empty_field(self):
        row = self._row(extra_json=json.dumps({}))
        line = db._account_full_export_line(row)
        # 空密码是“真实空字段”，由相邻 “---” 收拢为 “------”，保持 5 段结构可读
        self.assertEqual(
            line,
            "a@b.com---gptmail------https://2fa.run/----2FA:ABCDEF123",
        )

    def test_missing_totp_keeps_prefix(self):
        row = self._row(totp_secret="")
        line = db._account_full_export_line(row)
        self.assertEqual(
            line,
            "a@b.com---gptmail---Pass123---https://2fa.run/----2FA:",
        )

    def test_password_from_top_level_registration_password(self):
        # 兼容 registration_password 直接写在顶层（无 extra_json）的情况
        row = self._row(extra_json=None, registration_password="TopPw")
        line = db._account_full_export_line(row)
        self.assertIn("---TopPw---", line)

    def test_email_source_fallback_empty(self):
        row = self._row(email_source="")
        line = db._account_full_export_line(row)
        # 来源为空时保留空字段（------），不破坏整体结构
        self.assertTrue(line.startswith("a@b.com------Pass123---https://2fa.run/----2FA:ABCDEF123"))

    def test_generic_api_resolves_to_code_url_link(self):
        # generic_api 账号应解析出邮箱池里的完整取码链接，而非裸字符串 generic_api
        with mock.patch.object(
            db, "get_generic_api_email_by_email",
            return_value={"code_url": "http://127.0.0.1:5055/code?email=a@b.com"},
        ):
            row = self._row(email_source="generic_api")
            line = db._account_full_export_line(row)
        self.assertEqual(
            line,
            "a@b.com---http://127.0.0.1:5055/code?email=a@b.com---Pass123---https://2fa.run/----2FA:ABCDEF123",
        )

    def test_generic_api_constructs_link_when_pool_missing(self):
        # 邮箱池查不到该邮箱时，按 OmniMail 约定拼出完整取码链接
        import config.email as ce
        with mock.patch.object(
            db, "get_generic_api_email_by_email", return_value=None
        ), mock.patch.object(ce, "OMNIMAIL_BASE", "https://omnimail.example/api", create=True):
            row = self._row(email_source="generic_api")
            line = db._account_full_export_line(row)
        self.assertEqual(
            line,
            "a@b.com---https://omnimail.example/api/messages?mailbox=a@b.com---Pass123---https://2fa.run/----2FA:ABCDEF123",
        )

    def test_build_code_url_with_base(self):
        import config.email as ce
        with mock.patch.object(ce, "OMNIMAIL_BASE", "https://example.com/api/", create=True):
            self.assertEqual(
                db._build_generic_api_code_url("a@b.com"),
                "https://example.com/api/messages?mailbox=a@b.com",
            )


if __name__ == "__main__":
    unittest.main()
