# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import core.chatgpt_plan as plan


class _Response:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = {}

    def json(self):
        return self._data


class _HttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _PlanSession:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.proxy = kwargs.get("proxy")
        self.device_id = "device-one"
        self.oai_session_id = "session-one"
        self.session = _HttpSession()
        self.reset_count = 0
        self.requests = []
        self.responses = [
            _Response(403, text="blocked"),
            _Response(200, data={
                "accounts": {
                    "acc-1": {
                        "account": {"account_id": "acc-1", "plan_type": "free"},
                        "entitlement": {},
                    }
                }
            }),
        ]
        self.created.append(self)

    def get_chatgpt_headers(self, **_kwargs):
        return {
            "content-type": "application/json",
            "oai-device-id": self.device_id,
            "oai-session-id": self.oai_session_id,
            "oai-client-build-number": "build",
            "oai-client-version": "version",
        }

    def get(self, url, headers=None, **_kwargs):
        self.requests.append((url, dict(headers or {})))
        return self.responses.pop(0)

    def reset_circuit_breaker(self):
        self.reset_count += 1

    def js_timezone_offset_min(self):
        return -540

    def fingerprint_summary_text(self):
        return "lang=ja-JP tz=Asia/Tokyo"


class ChatgptPlanRetryTests(unittest.TestCase):
    def setUp(self):
        _PlanSession.created = []

    def test_403_retries_in_same_session_with_complete_frontend_headers(self):
        claims = {
            "payload": {}, "email": "one@example.com", "account_id": "acc-1",
            "token_expired": False, "claim_plan_type": "free",
        }
        with patch.object(plan, "BrowserSession", _PlanSession), patch.object(
            plan, "token_claims", return_value=claims
        ), patch.object(plan, "resolve_plan_check_route", return_value={
            "proxy": "socks5://proxy:1080", "proxy_mode": "proxy",
            "network_route": "proxy", "proxy_used": "socks5://***:***@proxy:1080",
            "proxy_fallback_reason": None,
        }), patch.object(plan, "_warm_plan_session"), patch.object(
            plan.time, "sleep"
        ):
            result = plan.check_account_plan(
                "token", max_attempts=2, retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["timezone_offset_min"], "-540")
        self.assertEqual(len(_PlanSession.created), 1)
        session = _PlanSession.created[0]
        self.assertEqual(session.reset_count, 1)
        self.assertTrue(session.session.closed)
        self.assertTrue(session.kwargs["detect_exit_geo"])
        self.assertTrue(session.kwargs["fingerprint_seed"].startswith("plan-check:one@example.com:"))
        first_headers = session.requests[0][1]
        self.assertEqual(first_headers["chatgpt-account-id"], "acc-1")
        self.assertEqual(first_headers["oai-device-id"], "device-one")
        self.assertEqual(first_headers["oai-session-id"], "session-one")
        self.assertNotIn("content-type", first_headers)

    def test_borrowed_session_retries_without_creating_or_closing_session(self):
        session = _PlanSession(proxy="socks5://live-proxy:1080")
        cookies = session.session
        with patch.object(plan, "BrowserSession") as create, \
             patch.object(plan, "resolve_plan_check_route") as route, \
             patch.object(plan, "open_plan_check_proxy") as relay, \
             patch.object(plan, "_warm_plan_session") as warm, \
             patch.object(plan, "close_browser_session") as close, \
             patch.object(plan.time, "sleep"):
            result = plan.check_account_plan("fresh-token", session=session, max_attempts=2, retry_delay=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(session.reset_count, 1)
        self.assertIs(session.session, cookies)
        self.assertFalse(session.session.closed)
        self.assertEqual(session.requests[0][1]["authorization"], "Bearer fresh-token")
        for mock in (create, route, relay, warm, close):
            mock.assert_not_called()

    def test_borrowed_session_failure_does_not_close_owner_resources(self):
        for responses in ([_Response(401)], [_Response(503), _Response(503)]):
            with self.subTest(status=responses[0].status_code):
                session = _PlanSession(proxy="socks5://live-proxy:1080")
                session.responses = responses
                with patch.object(plan.time, "sleep"), patch.object(plan, "close_browser_session") as close:
                    result = plan.check_account_plan("fresh-token", session=session, max_attempts=2, retry_delay=0)
                self.assertFalse(result["ok"])
                close.assert_not_called()
                self.assertFalse(session.session.closed)

    def test_relay_close_failure_does_not_discard_successful_standalone_plan(self):
        relay = Mock()
        relay.close.side_effect = OSError("relay close failed")
        with patch.object(plan, "BrowserSession", _PlanSession), \
             patch.object(plan, "resolve_plan_check_route", return_value={"proxy": "socks5://proxy:1080"}), \
             patch.object(plan, "open_plan_check_proxy", return_value=("socks5://relay:1080", relay)), \
             patch.object(plan, "_warm_plan_session"), patch.object(plan.time, "sleep"):
            result = plan.check_account_plan("token", max_attempts=2, retry_delay=0)
        self.assertTrue(result["ok"])
        self.assertTrue(_PlanSession.created[0].session.closed)
        relay.close.assert_called_once()

    def test_403_is_retryable(self):
        self.assertTrue(plan._retryable_plan_error(403))
        self.assertFalse(plan._retryable_plan_error(401))


if __name__ == "__main__":
    unittest.main()
