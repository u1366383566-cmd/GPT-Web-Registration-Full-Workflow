# -*- coding: utf-8 -*-
"""邮箱池 source 列在 claim/release 重写集合后必须保留的回归测试。

背景：_save_collection 曾在 INSERT 时漏掉 source 列，导致 generic_api 邮箱
经过一次领取/释放后 source 被抹成空，注册时按来源领取永远找不到邮箱。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class EmailPoolSourceTests(unittest.TestCase):
    def test_generic_api_source_survives_claim_release_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_JOBS_JSON", root / "jobs.json"), \
                 patch.object(db, "_GENERIC_API_EMAIL_JSON", root / "generic_api.json"):
                inserted, skipped = db.import_generic_api_emails([
                    {"email": "a@outlook.com", "code_url": "https://x/v1/pickup?email=a@outlook.com&token=t1"},
                    {"email": "b@outlook.com", "code_url": "https://x/v1/pickup?email=b@outlook.com&token=t2"},
                ])
                self.assertEqual((inserted, skipped), (2, 0))

                # 第一次领取 + 保存循环
                first = db.claim_next_generic_api_email()
                self.assertEqual(first["email"], "a@outlook.com")
                # 第二次领取：修复前 source 已被第一次保存抹掉，这里会直接失败
                second = db.claim_next_generic_api_email()
                self.assertEqual(second["email"], "b@outlook.com")

                # 释放后可再次领取
                self.assertTrue(db.release_unconsumed_generic_api_email("a@outlook.com"))
                again = db.claim_next_generic_api_email()
                self.assertEqual(again["email"], "a@outlook.com")

                # 落库校验：所有行的 source 列保持 generic_api
                conn = sqlite3.connect(str(root / "turb.sqlite3"))
                rows = conn.execute(
                    "SELECT email, source, status FROM email_pool ORDER BY id"
                ).fetchall()
                conn.close()
                by_email = {email: (source, status) for email, source, status in rows}
                self.assertEqual(by_email["a@outlook.com"], ("generic_api", "used"))
                self.assertEqual(by_email["b@outlook.com"], ("generic_api", "used"))


if __name__ == "__main__":
    unittest.main()
