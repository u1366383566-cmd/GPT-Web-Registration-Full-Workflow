# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import core.account_export as account_export


class _CircuitSession:
    def __init__(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.reset_count = 0

    def reset_circuit_breaker(self):
        self.reset_count += 1
        self.blocked_until = 0.0
        self.blocked_reason = ""


class AccountExportTwofaTests(unittest.TestCase):
    def test_authorize_403_reuses_cf_cookie_and_retries(self):
        session = _CircuitSession()
        calls = []

        def follow(_session, _auth_url):
            calls.append(1)
            if len(calls) == 1:
                session.blocked_until = 9999999999.0
                session.blocked_reason = "HTTP 403 from authorize"
                raise RuntimeError("HTTP 403 from authorize")
            return "https://auth.openai.com/email-verification"

        with patch.object(account_export, "_follow_reauth", side_effect=follow), patch(
            "config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3
        ), patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), patch.object(
            account_export.time, "sleep"
        ) as sleep:
            result = account_export._follow_reauth_with_retry(
                session, "https://auth.openai.com/api/accounts/authorize?state=test"
            )

        self.assertEqual(result, "https://auth.openai.com/email-verification")
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.reset_count, 1)
        sleep.assert_not_called()

    def test_reauth_403_is_retried_after_circuit_reset(self):
        session = _CircuitSession()
        calls = []

        def trigger(_session, _email):
            calls.append(1)
            if len(calls) < 3:
                session.blocked_until = 9999999999.0
                session.blocked_reason = "HTTP 403 from csrf"
                raise RuntimeError("HTTP 403 from csrf")
            return "https://auth.example/authorize"

        with patch.object(account_export, "_trigger_reauth", side_effect=trigger), patch(
            "config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3
        ), patch("config.twofa.TWOFA_REAUTH_RETRY_DELAY", 0), patch.object(
            account_export.time, "sleep"
        ) as sleep:
            result = account_export._trigger_reauth_with_retry(
                session, "user@example.com"
            )

        self.assertEqual(result, "https://auth.example/authorize")
        self.assertEqual(len(calls), 3)
        self.assertEqual(session.reset_count, 2)
        sleep.assert_not_called()

    def test_reauth_business_400_is_not_retried(self):
        session = _CircuitSession()
        with patch.object(
            account_export, "_trigger_reauth", side_effect=RuntimeError("HTTP 400 invalid request")
        ) as trigger, patch("config.twofa.TWOFA_REAUTH_MAX_ATTEMPTS", 3):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                account_export._trigger_reauth_with_retry(
                    session, "user@example.com"
                )

        trigger.assert_called_once()
        self.assertEqual(session.reset_count, 0)

    def test_optional_bootstrap_403_does_not_block_reauth(self):
        session = _CircuitSession()

        def bootstrap_with_403(*_args, **_kwargs):
            session.blocked_until = 9999999999.0
            session.blocked_reason = (
                "HTTP 403 from https://chatgpt.com/backend-api/accounts/optimized/check"
            )

        def assert_reauth_can_start(_session, _email):
            self.assertEqual(session.blocked_until, 0.0)
            self.assertEqual(session.blocked_reason, "")
            raise RuntimeError("stop-after-assert")

        with patch(
            "core.chatgpt_bootstrap.authenticated_bootstrap",
            side_effect=bootstrap_with_403,
        ), patch.object(account_export, "human_delay"), patch.object(
            account_export, "_trigger_reauth", side_effect=assert_reauth_can_start
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-assert"):
                account_export.setup_2fa(
                    session,
                    "user@example.com",
                    access_token="access-token",
                )

        self.assertEqual(session.reset_count, 1)

    def test_bootstrap_exception_also_clears_circuit(self):
        session = _CircuitSession()

        def bootstrap_failure(*_args, **_kwargs):
            session.blocked_until = 9999999999.0
            session.blocked_reason = "HTTP 403 from optional bootstrap"
            raise RuntimeError("bootstrap failed")

        def assert_reauth_can_start(_session, _email):
            self.assertEqual(session.blocked_until, 0.0)
            raise RuntimeError("stop-after-assert")

        with patch(
            "core.chatgpt_bootstrap.authenticated_bootstrap",
            side_effect=bootstrap_failure,
        ), patch.object(account_export, "human_delay"), patch.object(
            account_export, "_trigger_reauth", side_effect=assert_reauth_can_start
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-assert"):
                account_export.setup_2fa(
                    session,
                    "user@example.com",
                    access_token="access-token",
                )

        self.assertEqual(session.reset_count, 1)


if __name__ == "__main__":
    unittest.main()
