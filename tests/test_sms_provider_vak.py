# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config
from config import env_loader
from webui import config_editor


class _Resp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data, ensure_ascii=False)

    def json(self):
        return self._data


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers or {}, "params": params or {}})
        item = self.responses.pop(0)
        if isinstance(item, _Resp):
            return item
        return _Resp(item)

    def close(self):
        self.closed = True


class VakSmsProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields_include_vak(self):
        self.assertIn("VAK_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("VAK_API_BASE", fields)
        self.assertIn("VAK_API_KEY", fields)
        self.assertIn("VAK_PRODUCT", fields)
        self.assertIn("VAK_COUNTRY", fields)
        self.assertIn("VAK_MAX_PRICE", fields)
        self.assertIn("VAK_OPERATOR", fields)
        self.assertTrue(fields["VAK_API_KEY"].get("secret"))
        self.assertEqual(fields["VAK_PRODUCT"].get("choices", [{}])[0].get("value"), "dr")
        self.assertTrue(fields["VAK_COUNTRY"].get("allow_custom"))
        self.assertTrue(fields["VAK_MAX_PRICE"].get("allow_custom"))

    def test_acquire_number_uses_vak_api_getnumber(self):
        http = _Http([
            {
                "dr": {
                    "code": "dr",
                    "name": "OpenAI",
                    "totalCount": 12,
                    "minPrice": 10,
                    "apiPrice": 12,
                    "priceMap": {"10": 3, "12": 9},
                }
            },
            {
                "tel": 71234567890,
                "idNum": "123",
                "price": 10,
            }
        ])
        with (
            patch.object(codex_config, "SMS_PROVIDER", "vak"),
            patch.object(codex_config, "VAK_API_BASE", "https://vak-sms.com/api"),
            patch.object(codex_config, "VAK_API_KEY", "vak-key"),
            patch.object(codex_config, "VAK_COUNTRY", "gb"),
            patch.object(codex_config, "VAK_OPERATOR", "any"),
            patch.object(codex_config, "VAK_PRODUCT", "dr"),
            patch.object(codex_config, "VAK_MAX_PRICE", 12.0),
        ):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "123")
        self.assertEqual(phone, "71234567890")
        self.assertTrue(http.calls[0]["url"].endswith("/getOfferNumberList"))
        self.assertEqual(http.calls[0]["params"]["apiKey"], "vak-key")
        self.assertEqual(http.calls[0]["params"]["country"], "gb")
        self.assertEqual(http.calls[0]["params"]["services"], "dr")
        self.assertTrue(http.calls[1]["url"].endswith("/getNumber"))
        self.assertEqual(http.calls[1]["params"]["apiKey"], "vak-key")
        self.assertEqual(http.calls[1]["params"]["service"], "dr")
        self.assertEqual(http.calls[1]["params"]["country"], "gb")
        self.assertEqual(http.calls[1]["params"]["price"], "12")
        self.assertEqual(http.calls[1]["params"]["fixedPrice"], "true")

    def test_wait_for_sms_code_reads_order_sms(self):
        http = _Http([
            {"smsCode": ""},
            {"smsCode": "654321"},
        ])
        with (
            patch.object(codex_config, "SMS_PROVIDER", "vak"),
            patch.object(codex_config, "VAK_API_BASE", "https://vak-sms.com/api"),
            patch.object(codex_config, "VAK_API_KEY", "vak-key"),
        ):
            code = sms_provider.wait_for_sms_code("123", http=http, max_wait=2, poll_interval=0)

        self.assertEqual(code, "654321")
        self.assertTrue(http.calls[0]["url"].endswith("/getSmsCode"))
        self.assertEqual(http.calls[0]["params"]["idNum"], "123")
        self.assertEqual(http.calls[0]["params"]["apiKey"], "vak-key")
        self.assertTrue(http.calls[1]["url"].endswith("/getSmsCode"))

    def test_wait_for_sms_code_treats_bad_data_as_waiting(self):
        http = _Http([
            {"error": "badData"},
            {"smsCode": "123456"},
        ])
        with (
            patch.object(codex_config, "SMS_PROVIDER", "vak"),
            patch.object(codex_config, "VAK_API_BASE", "https://vak-sms.com/api"),
            patch.object(codex_config, "VAK_API_KEY", "vak-key"),
        ):
            code = sms_provider.wait_for_sms_code("123", http=http, max_wait=2, poll_interval=0)

        self.assertEqual(code, "123456")
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0]["url"].endswith("/getSmsCode"))

    def test_cancel_and_complete_use_vak_setstatus(self):
        with (
            patch.object(codex_config, "SMS_PROVIDER", "vak"),
            patch.object(codex_config, "VAK_API_BASE", "https://vak-sms.com/api"),
            patch.object(codex_config, "VAK_API_KEY", "vak-key"),
        ):
            http_cancel = _Http([{"status": "bad"}])
            sms_provider.cancel("123", http=http_cancel)
            http_finish = _Http([{"status": "end"}])
            sms_provider.complete("123", http=http_finish)

        self.assertTrue(http_cancel.calls[0]["url"].endswith("/setStatus"))
        self.assertEqual(http_cancel.calls[0]["params"]["idNum"], "123")
        self.assertEqual(http_cancel.calls[0]["params"]["status"], "bad")
        self.assertTrue(http_finish.calls[0]["url"].endswith("/setStatus"))
        self.assertEqual(http_finish.calls[0]["params"]["status"], "end")

    def test_vak_no_free_phones_maps_to_no_numbers(self):
        http = _Http([
            {"dr": {"code": "dr", "name": "OpenAI", "totalCount": 12, "minPrice": 10, "apiPrice": 12, "priceMap": {"10": 3}}},
            _Resp({"error": "no free phones"}, status_code=400),
        ])
        with (
            patch.object(codex_config, "SMS_PROVIDER", "vak"),
            patch.object(codex_config, "VAK_API_BASE", "https://vak-sms.com/api"),
            patch.object(codex_config, "VAK_API_KEY", "vak-key"),
            patch.object(codex_config, "VAK_COUNTRY", "gb"),
            patch.object(codex_config, "VAK_OPERATOR", "any"),
            patch.object(codex_config, "VAK_PRODUCT", "dr"),
            patch.object(codex_config, "VAK_MAX_PRICE", 0.0),
        ):
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                sms_provider.acquire_number(http=http)

    def test_vak_price_over_limit_is_rejected_before_order(self):
        http = _Http([
            {
                "dr": {
                    "code": "dr",
                    "name": "OpenAI",
                    "totalCount": 12,
                    "minPrice": 10,
                    "apiPrice": 12,
                    "priceMap": {"12": 1, "15": 3},
                }
            }
        ])
        with (
            patch.object(codex_config, "SMS_PROVIDER", "vak"),
            patch.object(codex_config, "VAK_API_BASE", "https://vak-sms.com/api"),
            patch.object(codex_config, "VAK_API_KEY", "vak-key"),
            patch.object(codex_config, "VAK_COUNTRY", "gb"),
            patch.object(codex_config, "VAK_PRODUCT", "dr"),
            patch.object(codex_config, "VAK_OPERATOR", "any"),
            patch.object(codex_config, "VAK_MAX_PRICE", 10.0),
        ):
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                sms_provider.acquire_number(http=http)

        self.assertTrue(http.calls[0]["url"].endswith("/getOfferNumberList"))
        self.assertEqual(len(http.calls), 1)


if __name__ == "__main__":
    unittest.main()
