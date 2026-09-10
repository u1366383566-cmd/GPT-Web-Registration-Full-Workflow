# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import browser_use_registration as browser_use
from core import email_provider
from core import roxy_registration as roxy


class DelayedEmailAllocationTests(unittest.TestCase):
    @patch("core.email_provider.acquire_email")
    def test_acquire_email_after_input_keeps_fixed_email_without_allocating(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            self.assertEqual(
                email_provider.acquire_email_after_input("fixed@example.com"),
                "fixed@example.com",
            )
        acquire.assert_not_called()

    @patch("core.email_provider.acquire_email", return_value="allocated@example.com")
    def test_acquire_email_after_input_allocates_only_for_automatic_mode(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.acquire_email_after_input(None),
                "allocated@example.com",
            )
        acquire.assert_called_once_with()

    def test_roxy_finds_input_before_allocating_email(self):
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "roxy@example.com"

        with patch.object(roxy, "_wait_for_email_input", side_effect=find_input), patch.object(
            roxy,
            "_human_type_text",
            side_effect=lambda *args, **kwargs: events.append("type_email"),
        ), patch.object(
            roxy,
            "_email_input_value_state",
            return_value={"inputs": [{"value": "roxy@example.com"}]},
        ), patch.object(
            roxy,
            "_submit_email_step",
            side_effect=lambda *args, **kwargs: events.append("submit_email"),
        ), patch.object(
            roxy,
            "_wait_email_submit_next_state",
            return_value="otp",
        ), patch.object(roxy, "human_delay"), patch.object(roxy, "_check_manual_stop"):
            result = roxy._submit_email_and_wait_next(
                object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "otp")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )

    def test_browser_use_finds_input_before_allocating_email(self):
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "browser@example.com"

        with patch.object(browser_use, "_wait_for_email_input_pw", side_effect=find_input), patch.object(
            browser_use,
            "_human_fill_locator",
            side_effect=lambda *args, **kwargs: events.append("type_email"),
        ), patch.object(
            browser_use,
            "_submit_email_step_pw",
            side_effect=lambda *args, **kwargs: events.append("submit_email") or True,
        ), patch.object(
            browser_use,
            "_wait_after_email_submit_transition",
            return_value="email_verification",
        ), patch.object(browser_use, "_human_pause"), patch.object(
            browser_use, "_check_manual_stop"
        ), patch.object(browser_use, "_page_url", return_value="https://chatgpt.com/auth/login"):
            result = browser_use._submit_email_until_transition(
                object(), object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "email_verification")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )


if __name__ == "__main__":
    unittest.main()
