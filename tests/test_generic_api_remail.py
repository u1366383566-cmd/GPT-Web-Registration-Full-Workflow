# -*- coding: utf-8 -*-
"""remail /v1/pickup 取件接口的验证码提取测试。"""
import json
import time
import unittest

from core.generic_api_mail_client import (
    _extract_structured_api_code,
    is_remail_pickup_url,
)


def _pickup_payload() -> dict:
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 10)) + ".000Z"
    old_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600)) + ".000Z"
    return {
        "items": [
            {
                "id": 5012,
                "sender": "account-security-noreply@accountprotection.microsoft.com",
                "recipient": "mateo.richards@outlook.com",
                "receivedAt": old_iso,
                "subject": "Microsoft account security code",
                "bodyPreview": "Your security code is 829104.",
                "verificationCode": "829104",
            },
            {
                "id": 5013,
                "sender": "noreply@tm.openai.com",
                "recipient": "mateo.richards@outlook.com",
                "receivedAt": now_iso,
                "subject": "Your ChatGPT code is 654321",
                "bodyPreview": "Verify your email: your code is 654321",
                "verificationCode": "654321",
            },
        ],
        "fetch": {"lastJobId": 0, "lastStatus": "succeeded"},
    }


class TestRemailPickupExtraction(unittest.TestCase):
    def test_is_remail_pickup_url(self):
        self.assertTrue(is_remail_pickup_url("https://remail.aishop6.com/v1/pickup?email=a@b.c&token=st_x"))
        self.assertFalse(is_remail_pickup_url("https://mail.example.com/api/code?email=a@b.c"))

    def test_prefers_openai_mail_over_microsoft(self):
        code, meta = _extract_structured_api_code(json.dumps(_pickup_payload()))
        self.assertEqual(code, "654321")
        self.assertEqual(meta.get("source"), "items_api")
        self.assertIn("openai", str(meta.get("from") or ""))

    def test_skips_stale_code_by_after_ts(self):
        payload = _pickup_payload()
        # 把 OpenAI 邮件改成 1 小时前的旧邮件，after_ts 取当前时间，应整体无新码
        for item in payload["items"]:
            item["receivedAt"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600)
            ) + ".000Z"
        result = _extract_structured_api_code(json.dumps(payload), after_ts=time.time() - 60)
        self.assertIsNone(result)

    def test_falls_back_to_latest_non_openai_mail(self):
        payload = _pickup_payload()
        # 删掉 OpenAI 邮件后，应取到剩余（微软）邮件的验证码
        payload["items"] = [item for item in payload["items"] if "openai" not in str(item.get("sender"))]
        code, meta = _extract_structured_api_code(json.dumps(payload))
        self.assertEqual(code, "829104")
        self.assertIn("microsoft", str(meta.get("from") or "").lower())

    def test_non_pickup_json_still_works(self):
        payload = {"code": "784207", "subject": "Your temporary ChatGPT login code"}
        code, meta = _extract_structured_api_code(json.dumps(payload))
        self.assertEqual(code, "784207")
        self.assertNotEqual(meta.get("source"), "items_api")

    def test_ignores_six_digit_message_ids(self):
        payload = {
            "items": [
                {
                    "id": 501234,
                    "sender": "foo@example.com",
                    "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z",
                    "subject": "Welcome",
                    "bodyPreview": "Hello world",
                }
            ]
        }
        self.assertIsNone(_extract_structured_api_code(json.dumps(payload)))


if __name__ == "__main__":
    unittest.main()
