# -*- coding: utf-8 -*-
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from config import email as cfg
from core import mailcode_client as client
from core import email_provider


def response(data=None, status=200, error=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = {"request_id": "req-test", "error": error} if error else {"request_id": "req-test", "data": data}
    return result


def codes(records=None, cursor="0", more=False, channel="READY"):
    return {"records": records or [], "next_sequence": cursor, "has_more": more, "channel_status": channel}


def record(sequence="1", code="012345", received=None, expired=False):
    now = time.time()
    return {"sequence": sequence, "code": code, "received_at": int((received or now) * 1000),
            "expires_at": int((now + (-60 if expired else 600)) * 1000)}


class MailCodeClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {"MAILCODE_API_BASE": "http://mail.test:18080/api/open/v1/", "MAILCODE_API_KEY": "test-key",
                            "MAILCODE_PRODUCT_ID": "product-1", "MAILCODE_REQUEST_TIMEOUT": 2,
                            "MAILCODE_CHANNEL_WAIT_SECONDS": 5, "OTP_SETTLE_SECONDS": 0}.items():
            p = patch.object(cfg, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(client, "_STATE_DIR", Path(self.temp.name))
        p.start()
        self.addCleanup(p.stop)
        client._CONTEXT_CACHE.clear()
        self.addCleanup(client._CONTEXT_CACHE.clear)

    def test_service_base_ignores_legacy_custom_configuration(self):
        with patch.object(cfg, "MAILCODE_API_BASE", "https://custom.example.test"):
            self.assertEqual(client._base(), "https://mailcode.bzb.qzz.io")

    def order(self):
        return {"order_id": "order-1", "status": "COMPLETED", "count": 1, "points_charged": "100",
                "items": [{"item_id": "item-1", "email": "a@mail.test", "mailbox_id": "mailbox-1", "status": "ACTIVE"}]}

    @patch.object(client.requests, "request")
    def test_purchase_headers_fixed_base_and_ready_baseline(self, request):
        request.side_effect = [response(self.order(), 201), response(codes(cursor="9"))]
        account = client.pick_account()
        self.assertEqual(account.email, "a@mail.test")
        self.assertEqual(account.next_sequence, "9")
        args, kwargs = request.call_args_list[0]
        self.assertEqual(args, ("POST", "https://mailcode.bzb.qzz.io/api/open/v1/orders"))
        self.assertEqual(kwargs["json"], {"product_id": "product-1", "count": 1})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertTrue(kwargs["headers"]["Idempotency-Key"].startswith("turb-mailcode-"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("after_sequence", request.call_args_list[1].kwargs["params"])
        self.assertEqual(list(Path(self.temp.name).glob("purchases/*.json")), [])

    @patch.object(client.time, "sleep")
    @patch.object(client.requests, "request")
    def test_network_retry_keeps_identical_purchase_key_and_body(self, request, sleep):
        request.side_effect = [requests.Timeout(), response(self.order(), 201), response(codes())]
        client.pick_account()
        first, second = request.call_args_list[:2]
        self.assertEqual(first.kwargs["headers"]["Idempotency-Key"], second.kwargs["headers"]["Idempotency-Key"])
        self.assertEqual(first.kwargs["json"], second.kwargs["json"])

    @patch.object(client.time, "sleep")
    @patch.object(client.requests, "request")
    def test_uncertain_purchase_persists_key_for_next_attempt(self, request, sleep):
        request.side_effect = requests.Timeout()
        with self.assertRaisesRegex(client.MailCodeError, "已保存幂等键"):
            client.pick_account()
        key = request.call_args_list[0].kwargs["headers"]["Idempotency-Key"]
        self.assertEqual(len(list(Path(self.temp.name).glob("purchases/*.json"))), 1)
        request.reset_mock()
        request.side_effect = [response(self.order(), 201), response(codes())]
        client.pick_account()
        self.assertEqual(request.call_args_list[0].kwargs["headers"]["Idempotency-Key"], key)

    @patch.object(client.requests, "request")
    def test_no_repurchase_when_preparation_failed(self, request):
        request.side_effect = [response(self.order(), 201), response(status=401, error={"code": "UNAUTHORIZED"})]
        with self.assertRaises(client.MailCodeError):
            client.pick_account()
        request.reset_mock()
        request.side_effect = [response(codes(cursor="2"))]
        self.assertEqual(client.pick_account().next_sequence, "2")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")

    @patch.object(client.time, "sleep")
    @patch.object(client.requests, "request")
    def test_waits_for_ready_and_handles_temporary_unavailable(self, request, sleep):
        request.side_effect = [response(self.order(), 201), response(status=503, error={"code": "CODE_UNAVAILABLE"}, headers={"Retry-After": "1"}),
                               response(codes(channel="INITIALIZING")), response(codes(cursor="4"))]
        self.assertEqual(client.pick_account().next_sequence, "4")
        self.assertEqual(sleep.call_count, 2)

    @patch.object(client.requests, "request")
    def test_business_failure_is_not_retried(self, request):
        request.return_value = response(status=409, error={"code": "OUT_OF_STOCK", "message": "库存不足"})
        with self.assertRaisesRegex(client.MailCodeError, "OUT_OF_STOCK"):
            client.pick_account()
        request.assert_called_once()
        self.assertEqual(list(Path(self.temp.name).glob("purchases/*.json")), [])

    @patch.object(client.requests, "request")
    def test_non_json_error_preserves_http_status(self, request):
        request.return_value = Mock(status_code=503, headers={"Retry-After": "7"})
        request.return_value.json.side_effect = ValueError()
        with self.assertRaises(client.MailCodeError) as error:
            client.get_wallet()
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(error.exception.retry_after, 7)

    @patch.object(client.requests, "request")
    def test_products_and_wallet_pagination_preserve_points_string(self, request):
        request.side_effect = [response([{"id": "product"}] * 100), response([]), response({"balance": "1000"})]
        self.assertEqual(len(client.list_products()), 100)
        self.assertEqual(request.call_args_list[1].kwargs["params"], {"offset": 100, "limit": 100})
        self.assertEqual(client.get_wallet()["balance"], "1000")

    @patch.object(client, "get_codes")
    def test_pagination_keeps_exact_cursor_and_leading_zero(self, get_codes):
        account = client._remember(client.MailCodeAccount("a@mail.test", "m", next_sequence="7"))
        get_codes.side_effect = [codes([record("8", "111111")], "8", True), codes([record("12")], "12")]
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=1, settle_seconds=0), "012345")
        self.assertEqual(get_codes.call_args_list[0].args, ("m", "7"))
        self.assertEqual(get_codes.call_args_list[1].args, ("m", "8"))
        self.assertEqual(account.next_sequence, "12")
        client._CONTEXT_CACHE.clear()
        self.assertEqual(client._restore(account.email).next_sequence, "12")

    @patch.object(client, "get_codes")
    def test_identical_code_in_different_records_is_not_deduplicated(self, get_codes):
        account = client._remember(client.MailCodeAccount("a@mail.test", "m"))
        get_codes.side_effect = [codes([record("1")], "1"), codes([record("2")], "2")]
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=0, settle_seconds=0), "012345")
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=0, settle_seconds=0), "012345")
        self.assertEqual(get_codes.call_args_list[1].args, ("m", "1"))

    @patch.object(client, "get_codes")
    def test_ignores_old_or_expired_codes(self, get_codes):
        client._remember(client.MailCodeAccount("a@mail.test", "m"))
        now = time.time()
        get_codes.return_value = codes([record("1", received=now - 600), record("2", expired=True)], "2")
        with self.assertRaisesRegex(client.MailCodeError, "超时"):
            client.fetch_latest_otp("a@mail.test", after_ts=now - 10, max_wait=0)

    @patch.object(client, "_request")
    @patch.object(client, "get_codes")
    def test_restore_by_email_from_paginated_mailboxes_without_buying(self, get_codes, request):
        request.side_effect = [[{"id": "other", "email": "other@mail.test"}] * 100, [{"id": "m", "email": "a@mail.test"}]]
        get_codes.return_value = codes([record()], "1")
        self.assertEqual(client.fetch_latest_otp("a@mail.test", max_wait=0, settle_seconds=0), "012345")
        self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))

    @patch.object(client, "_request")
    def test_release_never_refunds_or_revokes(self, request):
        client._remember(client.MailCodeAccount("a@mail.test", "m"))
        client.release_account("a@mail.test", status="failed")
        self.assertIsNone(client.get_account_context("a@mail.test"))
        request.assert_not_called()

    def test_provider_accepts_mailcode_and_routes_acquire_and_otp(self):
        self.assertEqual(email_provider.parse_email_sources("mailcode,remail,mailcode"), ["mailcode", "remail"])
        with patch.object(client, "pick_account", return_value=client.MailCodeAccount("a@mail.test", "m")):
            self.assertEqual(email_provider.acquire_email_from_source("mailcode"), "a@mail.test")
        with patch.object(cfg, "USE_EMAIL_SERVICE", True), patch.object(client, "fetch_latest_otp", return_value="012345") as fetch:
            self.assertEqual(email_provider.wait_for_otp("a@mail.test", after_ts=123, email_source="mailcode"), "012345")
            fetch.assert_called_once_with("a@mail.test", after_ts=123)


if __name__ == "__main__":
    unittest.main()
