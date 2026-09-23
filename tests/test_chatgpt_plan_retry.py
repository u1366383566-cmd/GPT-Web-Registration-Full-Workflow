# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

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

    def test_403_is_retryable(self):
        self.assertTrue(plan._retryable_plan_error(403))
        self.assertFalse(plan._retryable_plan_error(401))


if __name__ == "__main__":
    unittest.main()
