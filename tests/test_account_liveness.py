# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import core.account_liveness as liveness
import core.live_check_service as live_service


class _DummyHttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DummyBrowserSession:
    created = []

    def __init__(self, proxy=None, **kwargs):
        self.proxy = proxy
        self.received_proxy = proxy
        self.device_id = "device-test"
        self.session = _DummyHttpSession()
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.created.append(self)
        self.kwargs = kwargs

    def fingerprint_summary(self):
        return {
            "proxy": self.proxy or "",
            "user_agent": "test-agent",
            "accept_language": "en-US",
            "timezone_iana": "UTC",
            "timezone_offset_minutes": 0,
            "screen_width": 1440,
            "screen_height": 900,
            "device_pixel_ratio": 2,
            "hardware_concurrency": 8,
            "device_memory": 8,
            "geo_country": "US",
            "geo_city": "Test",
        }

    def fingerprint_summary_text(self):
        return "test-fingerprint"

    def reset_circuit_breaker(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""


class _DummyQueueSlot:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class AccountLivenessTests(unittest.TestCase):
    def setUp(self):
        _DummyBrowserSession.created = []

    def test_preflight_preserves_explicit_direct_route_and_skips_providers(self):
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", return_value="csrf"), \
             patch.object(liveness, "signin_openai", return_value="https://auth.example/authorize"):
            session, authorize_url = liveness._network_preflight_with_retry(
                "user@example.com", "", max_attempts=1
            )

        self.assertEqual(session.proxy, "")
        self.assertEqual(authorize_url, "https://auth.example/authorize")
        self.assertTrue(
            session.kwargs["fingerprint_seed"].startswith("live-check:user@example.com:")
        )

    def test_preflight_retries_with_same_session_when_csrf_is_blocked(self):
        csrf_errors = [RuntimeError("HTTP Error 403"), "csrf"]
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", side_effect=csrf_errors), \
             patch.object(liveness, "signin_openai", return_value="authorize"), \
             patch.object(liveness.time, "sleep"):
            session, _ = liveness._network_preflight_with_retry(
                "user@example.com", None, max_attempts=2
            )

        self.assertIs(_DummyBrowserSession.created[-1], session)
        # 403 下发的 CF Cookie 必须留在同一 Cookie Jar 中，不能每轮新建会话。
        self.assertEqual(len(_DummyBrowserSession.created), 1)
        self.assertFalse(_DummyBrowserSession.created[0].session.closed)
        self.assertIsNone(_DummyBrowserSession.created[0].received_proxy)

    def test_callback_403_retries_in_same_session_before_fetching_token(self):
        session = _DummyBrowserSession(proxy="proxy")
        with patch.object(
            liveness,
            "follow_oauth_callback",
            side_effect=[RuntimeError("HTTP 403 from callback/openai"), "https://chatgpt.com/"],
        ) as callback, patch.object(
            liveness,
            "fetch_session",
            return_value={"accessToken": "token"},
        ) as fetch, patch.object(liveness.time, "sleep"):
            result = liveness._follow_continue_and_fetch(
                session,
                "https://auth.openai.com/authorize/continue?state=test",
                referer="https://auth.openai.com/email-verification",
            )

        self.assertEqual(result["accessToken"], "token")
        self.assertEqual(callback.call_count, 2)
        fetch.assert_called_once_with(session)

    def test_fingerprint_identity_is_pinned_when_session_is_recreated_in_one_attempt(self):
        state = {}
        first = MagicMock()
        first.browser_profile = {
            "navigator_language": "ja-JP",
            "timezone_iana": "Asia/Tokyo",
            "screen_width": 1512,
        }
        second = MagicMock()
        second.browser_profile = dict(first.browser_profile)
        with patch.object(liveness, "BrowserSession", side_effect=[first, second]) as factory:
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "socks5://proxy:1080", state,
                ),
                first,
            )
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "", state,
                ),
                second,
            )

        first_kwargs = factory.call_args_list[0].kwargs
        second_kwargs = factory.call_args_list[1].kwargs
        self.assertTrue(first_kwargs["detect_exit_geo"])
        self.assertIsNone(first_kwargs["browser_profile"])
        self.assertFalse(second_kwargs["detect_exit_geo"])
        self.assertEqual(second_kwargs["browser_profile"]["navigator_language"], "ja-JP")
        self.assertEqual(second_kwargs["browser_profile"]["timezone_iana"], "Asia/Tokyo")
        self.assertEqual(first_kwargs["fingerprint_seed"], second_kwargs["fingerprint_seed"])
        self.assertTrue(str(first_kwargs["fingerprint_seed"]).startswith("live-check:user@example.com:"))

    def test_reauth_otp_dead_account_error_is_not_retried(self):
        response = SimpleNamespace(
            status_code=403,
            text='{"error":{"code":"account_deactivated"}}',
        )
        error = RuntimeError("HTTP Error 403")
        error.response = response
        session = _DummyBrowserSession(proxy="proxy")

        with patch.object(liveness, "_validate_reauth_otp", side_effect=error), \
             patch.object(liveness, "wait_for_otp", return_value="123456"):
            with self.assertRaises(liveness.AccountUnusableError) as ctx:
                liveness._validate_reauth_with_retry(session, "user@example.com", 1.0)

        self.assertEqual(ctx.exception.error_code, "account_deactivated")

    def test_existing_access_token_uses_reauth_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "BrowserSession", return_value=session), \
                 patch.object(liveness, "_warm_authenticated_session") as warm, \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", return_value={
                     "accessToken": "new-token",
                     "user": {"id": "user-1"},
                     "account": {"planType": "free"},
                 }) as reauth:
                result = liveness.check_account_liveness("user@example.com", proxy="")

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        warm.assert_called_once_with(session, "old-token")
        reauth.assert_called_once()
        self.assertTrue(session.session.closed)

    def test_reauth_403_falls_back_to_clean_full_web_login_on_same_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            reauth_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            full_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            fresh_info = {
                "accessToken": "new-token",
                "user": {"id": "user-1"},
                "account": {"planType": "free"},
            }
            state = {}
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_new_fingerprint_pinned_session", return_value=reauth_session), \
                 patch.object(liveness, "_warm_authenticated_session"), \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", side_effect=RuntimeError("HTTP 403 callback")), \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(
                     full_session, fresh_info,
                 )) as full_login:
                result = liveness.check_account_liveness(
                    "user@example.com",
                    proxy=None,
                    fingerprint_state=state,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        full_login.assert_called_once_with(
            "user@example.com",
            "socks5://proxy.example:1080",
            email_source=None,
            fingerprint_state=state,
        )
        self.assertTrue(reauth_session.session.closed)
        self.assertTrue(full_session.session.closed)

    def test_service_403_fallback_really_uses_direct_connection(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "socks5://proxy.example:1080",
                 "network_route": "proxy",
                 "proxy_mode": "auto",
             }), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "socks5://proxy.example:1080")
        self.assertEqual(check.call_args_list[0].kwargs["email_source"], "remail")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        self.assertEqual(check.call_args_list[1].kwargs["email_source"], "remail")
        self.assertIsNot(
            check.call_args_list[0].kwargs["fingerprint_state"],
            check.call_args_list[1].kwargs["fingerprint_state"],
        )
        self.assertTrue(slot.released)


if __name__ == "__main__":
    unittest.main()
