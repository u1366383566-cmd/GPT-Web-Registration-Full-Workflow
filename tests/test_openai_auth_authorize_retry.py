# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.openai_auth as openai_auth


class _Session:
    def __init__(self):
        self.calls = 0
        self.reset_count = 0
        self.cookie_jar_identity = object()

    def get_auth_navigate_headers(self, **_kwargs):
        return {"sec-fetch-mode": "navigate"}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            error = RuntimeError("HTTP Error 403")
            error.response = SimpleNamespace(status_code=403)
            raise error
        return SimpleNamespace(
            status_code=200,
            url="https://auth.openai.com/email-verification",
            raise_for_status=lambda: None,
        )

    def reset_circuit_breaker(self):
        self.reset_count += 1


class AuthorizeRetryTests(unittest.TestCase):
    def test_403_retries_on_same_session_and_keeps_cookie_jar(self):
        session = _Session()
        cookie_jar_identity = session.cookie_jar_identity

        with patch.object(openai_auth.time, "sleep") as sleep:
            result = openai_auth.follow_authorize(
                session,
                "https://auth.openai.com/api/accounts/authorize?state=test",
            )

        self.assertEqual(result, "https://auth.openai.com/email-verification")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.reset_count, 1)
        self.assertIs(session.cookie_jar_identity, cookie_jar_identity)
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
