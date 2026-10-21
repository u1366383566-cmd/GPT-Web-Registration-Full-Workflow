import unittest
from types import SimpleNamespace

from core.http_diagnostics import response_summary
from core.chatgpt_bootstrap import _safe_request


class HttpDiagnosticsTests(unittest.TestCase):
    def test_summary_uses_only_safe_metadata(self):
        response = SimpleNamespace(status_code=403, headers={
            "Content-Type": "text/html; charset=utf-8",
            "CF-Ray": "abc123-NRT", "CF-Mitigated": "challenge",
            "Set-Cookie": "secret-cookie", "Authorization": "secret-token",
        }, text="private-email@example.test")
        summary = response_summary(response)
        self.assertIn("challenge=True", summary)
        self.assertIn("cf_ray=abc123-NRT", summary)
        self.assertNotIn("secret", summary)
        self.assertNotIn("private-email", summary)

    def test_untrusted_header_values_are_not_logged(self):
        response = SimpleNamespace(status_code=403, headers={
            "cf-ray": "bad\nsecret", "content-type": "secret-token",
        })
        self.assertEqual(response_summary(response),
                         "status=403 content_type=unknown cf_ray=unknown challenge=False")

    def test_bootstrap_failure_logs_metadata_not_body(self):
        response = SimpleNamespace(status_code=403, headers={}, text="secret-body")
        with self.assertLogs("core.chatgpt_bootstrap", level="WARNING") as logs:
            self.assertIsNone(_safe_request("test", lambda: response))
        self.assertIn("status=403", str(logs.output))
        self.assertNotIn("secret-body", str(logs.output))
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            _safe_request("test", lambda: response, strict=True)


if __name__ == "__main__":
    unittest.main()
